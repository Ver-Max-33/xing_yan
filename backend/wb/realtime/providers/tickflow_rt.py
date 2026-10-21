"""TickFlow as the prod profile's CN realtime source (spec §4.8), when its key is set: today's 1-minute bars of the
stocks you follow and the indexes, ≤100 symbols a call (GET /v1/klines/batch, period 1m — a plan without 批量日内分时
may still ask the K-line batch for today's minutes). Measured 2026-10-09 with a Pro key: the bars are labelled by
their end and the day starts with a 09:30 bar that is the opening auction alone (241 a day); here a minute is labelled
by its start, as the session's and Yahoo's are (09:30 is 09:30–09:31), so the auction is part of the first minute (its
open is the day's) and the closing auction is in 14:59's. Volume comes in lots (科创板 too): ×100 is shares, the daily
bars' unit; amount is yuan, so the day's average price is exact. Prices carry float noise (9.739999999999998): rounded.
A day without an auction match still has its 09:30 bar, at the close before with no volume — like any bar before the
first trade it is a placeholder, left out. Prices are asked as traded (`adjust=none`: its K-lines are 比例前复权 unless
asked), as the daily bars and the close before are.
Its 1-minute history (about a year) fills the multi-day 分时. The K-line batch allows 30 calls a minute
("请求频率超限 (30/min)"): every TickFlow source here shares one budget of 25."""
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from wb.data.http import RateLimiter
from wb.realtime.providers.tushare_rt import ts_code
from wb.registry import REALTIME
from wb.secrets import get_secret

BASE = "https://api.tickflow.org"
BATCH = 100        # symbols a call (more: TOO_MANY_SYMBOLS)
TZ = ZoneInfo("Asia/Shanghai")
UNIVERSE = "CN_Equity_A"   # every A share (沪深京) in one quote call: 5,572 rows, 0.34 s, 584 KB gzipped (2026-10-12)
QUOTES = {"listing_id": pl.Utf8, "at": pl.Datetime, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "last": pl.Float64,
          "prev_close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64}
MINUTES = {"listing_id": pl.Utf8, "at": pl.Datetime, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
           "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64}
_client: httpx.Client | None = None
BUDGET = RateLimiter(25)   # the K-line batch's 30 a minute, shared by a poll, the days before and every instance


class TickFlowError(RuntimeError):
    pass


def symbol(listing_id: str) -> str:
    """CN:600000 → 600000.SH; an index CN:IDX:000001 → 000001.SH (上证指数), 399xxx → .SZ, 899xxx → .BJ (北证50)."""
    if ":IDX:" in listing_id:
        code = listing_id.rsplit(":", 1)[1]
        return f"{code}.{'SZ' if code.startswith('399') else 'BJ' if code.startswith('899') else 'SH'}"
    return ts_code(listing_id)


def listing_of(sym: str) -> str | None:
    """An A share's symbol back to its listing: 600000.SH → CN:600000; anything else (a future, a Hong Kong share) None."""
    code, _, ex = sym.partition(".")
    return f"CN:{code}" if ex in ("SH", "SZ", "BJ") and len(code) == 6 and code.isdigit() else None


def quote_frame(answer: list[dict], back: dict[str, str] | None = None) -> pl.DataFrame:
    """Quote rows as QUOTES: `back` maps the symbols asked for to their listings (indexes too); without it only A shares
    are kept. Prices rounded to 4 places (they carry float noise: 9.739999999999998); a price of 0 is one not traded
    yet (before the auction's match, a suspended stock): null; volume in lots × 100 is shares; the exchange's clock."""
    rows = []
    for q in answer:
        sym = str(q.get("symbol") or "")
        lid = back.get(sym) if back is not None else listing_of(sym)
        if lid is None or not q.get("timestamp"):
            continue
        at = datetime.fromtimestamp(q["timestamp"] / 1000, TZ).replace(tzinfo=None)
        prices = [round(float(v), 4) if v else None for v in (q.get(k) for k in ("open", "high", "low", "last_price", "prev_close"))]
        rows.append((lid, at, *prices, float(q.get("volume") or 0) * 100, float(q.get("amount") or 0)))
    return pl.DataFrame(rows, schema=QUOTES, orient="row")


def _shared() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=BASE, timeout=20)
    return _client


@REALTIME.register("tickflow_rt")
class TickFlowRealtime:
    name = "tickflow_rt"
    kind = "minutes"
    delay = 0
    poll_seconds = 30     # three calls a poll (≤297 stocks and the 3 indexes): six a minute, the days before three more
    max_listings = 297
    history_days = 19     # with today, the 20 days the multi-day 分时 shows
    approx_volume = False

    def __init__(self, market: str = "CN", transport: httpx.BaseTransport | None = None,
                 key: Callable[[], str | None] = lambda: get_secret("tickflow_api_key"), limiter: RateLimiter = BUDGET):
        self.market, self._key, self._limiter = market, key, limiter
        self._http = httpx.Client(base_url=BASE, timeout=20, transport=transport) if transport else None

    def minutes(self, listing_ids: Iterable[str], day: date, indexes: Iterable[str] = ()) -> pl.DataFrame:
        """listing_id (a stock or an index id), at (exchange time, the minute's start), open, high, low, close, volume
        (shares) and amount (yuan) of each minute of `day` so far; empty when the source has not reached the day."""
        return self.minutes_between([*listing_ids, *indexes], day, day)

    def minutes_between(self, listing_ids: Iterable[str], first: date, last: date) -> pl.DataFrame:
        """The minutes of each trading day from `first` to `last`, as minutes() gives one day's — in one call for up to
        100 symbols (one call brought a stock's 15 days, measured 10-12; ≤ 5,000 bars a call, about 20 days)."""
        ids = {symbol(x): x for x in listing_ids}
        window = [int(datetime.combine(d, t, TZ).timestamp() * 1000) for d, t in ((first, time(9, 0)), (last, time(15, 30)))]
        count = min(5000, 300 * ((last - first).days + 1))
        rows = []
        syms = list(ids)
        for k in range(0, len(syms), BATCH):
            data = self._get("/v1/klines/batch", {"symbols": ",".join(syms[k:k + BATCH]), "period": "1m",
                                                  "start_time": window[0], "end_time": window[1], "count": count,
                                                  "adjust": "none"})   # as traded: its default is 比例前复权
            for sym, c in (data or {}).items():
                if sym in ids:
                    rows += [(ids[sym], datetime.fromtimestamp(t / 1000, TZ).replace(tzinfo=None), *v)
                             for t, *v in zip(c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"],
                                              c["amount"], strict=True)]
        if not rows:
            return pl.DataFrame(schema=MINUTES)
        end = pl.col("at")
        return (pl.DataFrame(rows, schema=MINUTES, orient="row")
                .filter(end.dt.date().is_between(first, last))
                .sort("listing_id", "at")
                # before the day's first trade a bar is a placeholder at the close before (a 09:30 bar without an auction
                # match, a first minute without trades): it is not the open and not the day's range (000819, 2026-09-18)
                .filter((pl.col("volume") > 0).cast(pl.Int8).cum_max().over("listing_id", end.dt.date()) == 1)
                .with_columns(pl.when(end.dt.time() <= time(9, 30)).then(end).otherwise(end - timedelta(minutes=1)).alias("start"))
                .group_by("listing_id", "start", maintain_order=True)   # the opening auction joins the first minute
                .agg(pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(),
                     (pl.col("volume").sum() * 100).alias("volume"), pl.col("amount").sum())
                .rename({"start": "at"})
                .with_columns(pl.col("open", "high", "low", "close").round(4))
                .select(list(MINUTES)).cast(MINUTES))

    def days(self, listing_ids: Iterable[str], start: date, end: date) -> pl.DataFrame:
        """The daily bars from `start` to `end`, as traded (`adjust=none`): listing_id, date, open, high, low, close,
        volume (lots × 100: shares) and amount (yuan); ≤100 symbols a call, on the K-line budget. The data checks'
        witness (data/verify.py): verified 2026-10-11, the Beijing exchange's too."""
        ids = {symbol(x): x for x in listing_ids}
        window = [int(datetime.combine(d, time(0, 0), TZ).timestamp() * 1000) for d in (start, end + timedelta(days=1))]
        rows, syms = [], list(ids)
        for k in range(0, len(syms), BATCH):
            data = self._get("/v1/klines/batch", {"symbols": ",".join(syms[k:k + BATCH]), "period": "1d", "start_time": window[0],
                                                  "end_time": window[1], "count": (end - start).days + 10, "adjust": "none"})
            for sym, c in (data or {}).items():
                if sym in ids:
                    rows += [(ids[sym], datetime.fromtimestamp(t / 1000, TZ).date(), *v) for t, *v in
                             zip(c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"], c["amount"], strict=True)]
        schema = {"listing_id": pl.Utf8, "date": pl.Date, **{f: pl.Float64 for f in ("open", "high", "low", "close", "volume", "amount")}}
        return (pl.DataFrame(rows, schema=schema, orient="row").filter(pl.col("date").is_between(start, end))
                .with_columns(pl.col("open", "high", "low", "close").round(4), pl.col("volume") * 100))

    def quotes(self, listing_ids: Iterable[str]) -> pl.DataFrame:
        """The day so far at this moment (POST /v1/quotes, ≤100 symbols a call; not the K-line batch's endpoint, so off
        its budget) as QUOTES — as traded; a price not traded yet (0, before the auction) is null. The fast path's
        (design 2026-10-10 §5)."""
        ids = list(dict.fromkeys(listing_ids))
        back, answer = {symbol(x): x for x in ids}, []
        for i in range(0, len(ids), BATCH):
            answer += self._call("POST", "/v1/quotes", json={"symbols": [symbol(x) for x in ids[i:i + BATCH]]}) or []
        return quote_frame(answer, back)

    def whole_market(self) -> pl.DataFrame:
        """Every A share's day so far in one call (POST /v1/quotes by the universe CN_Equity_A, off the K-line budget)
        as QUOTES; the 市场 board's (design 2026-10-12 §8.2). Before the open each row is the day before, stamped with
        its close; during the call auction the price is the auction's reference and nothing has traded (volume 0)."""
        return quote_frame(self._call("POST", "/v1/quotes", json={"universes": [UNIVERSE]}) or [])

    def _get(self, path: str, params: dict) -> dict:
        return self._call("GET", path, budget=True, params=params)

    def _call(self, method: str, path: str, budget: bool = False, **kw) -> dict | list | None:
        """`budget`: a K-line endpoint's call, on their shared 30 a minute."""
        key = self._key()
        if not key:
            raise TickFlowError("还没有 TickFlow 的 API key：在 设置 → 盘中行情 里填（命令行：wb secret set tickflow_api_key）")
        if budget:
            self._limiter.acquire()
        r = (self._http or _shared()).request(method, path, headers={"x-api-key": key}, **kw)
        if r.status_code == 200:
            return r.json().get("data")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 401:
            raise TickFlowError("TickFlow 不认这个 API key（填错了或已经过期）：在 设置 → 盘中行情 里重填")
        why = body.get("message") or body.get("code") or r.reason_phrase
        raise TickFlowError(f"TickFlow {r.status_code}：{why}")
