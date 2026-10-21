"""盘中 (spec §4.8, §7.2, §8.5): while a market trades, its realtime source (routed by the data profile) is asked for
today's minutes of the stocks you follow — your watch groups and the 潜伏 lists of the enabled rules' latest daily
scans — every poll. Each row carries its change on the previous close, the intraday volume ratio ((today's volume ÷
minutes traded) ÷ (5-day average ÷ minutes in the day), as A-share software defines it) and its distance to the box's
top; the day's minutes are kept in a file (market/<M>/intraday/date=YYYY-MM-DD.parquet, the last KEEP_DAYS days) and
every poll goes to the page. Each poll also raises the day's alerts (§7.2): a 潜伏 stock whose rule is met on today's
temporary bar (waiting for the close), a price crossing a line you drew or a 潜伏 box's top; a stock whose price is
beyond its day's limits (an ex-rights day the factor has not caught) is not judged. Outside the session the rows are
the latest close. Nothing here is ever a signal: the daily scan on the close is the record."""
import io
import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from wb.config import Paths
from wb.data.quality import _vwap_outside
from wb.data.store import Store
from wb.datause import SOURCES, allowed, quiet
from wb.db import connect
from wb.engine.l1.frame import build_frame
from wb.engine.l1.frame import available_columns
from wb.engine.l2.box import resumed
from wb.engine.l3.match import Matcher, box_tol
from wb.engine.l3.understand import names
from wb.files import move_into_place, tmp_for
from wb.jobs.daily import DEMO
from wb.market.tape import MarketTape
from wb.listing import parse_id
from wb.markets import MARKETS, load_market
from wb.notify.notices import MARKET_NAME, listing_title
from wb.notify.outbox import Outbox
from wb.realtime.alerts import Alerts, notices
from wb.realtime.clock import clock_for, share
from wb.realtime.layers import auction, judge, with_stats
from wb.realtime.micro import WAVE, waves
from wb.realtime.lines import day_limit, drawn_lines, local, own_boxes, reachable
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.realtime.session import Session, in_session, minute_index, session_at
from wb.realtime.today import (Shared, day_basis, day_factors, evaluate_today, ex_rights, snapshot_bars, temp_bars, turnover,
                               with_day_factors)
from wb.registry import REALTIME, autoload
from wb.screening.scan import enabled_rules
from wb.secrets import secret_source
from wb.workspace.store import Workspace
from wb.workspace.watch import ALERTING, Watch

log = logging.getLogger("wb.live")
INDEXES = {"JP": ["JP:IDX:N225", "JP:IDX:TOPIX"], "CN": ["CN:IDX:000001", "CN:IDX:399001", "CN:IDX:399006"]}
# the realtime source of each data profile (spec §4.8): Yahoo's delayed minutes to test the chain; prod: Tushare rt_k
# (A股日线RT, a permission of its own) — or TickFlow's minutes once its key is set — and Tachibana e支店 (read-only;
# logged in when you ask)
ROUTES = {"test": {"CN": "yfinance_rt", "JP": "yfinance_rt"}, "prod": {"CN": "tushare_rt", "JP": "tachibana"}}
KEEP_DAYS = 60   # days of 1-minute bars kept (10-13: the minute charts' indicators warm up on them — a 60-minute MACD
# settles over about 40 days before the 20 it shows; the multi-day 分时 shows up to 20)
PREFETCH_BATCH = 100   # stocks a call fetching ahead (TickFlow's K-line batch takes 100)
PREFETCH_DAYS = 5      # days a call fetching ahead (≤ 5,000 bars a stock a call; 5 days of 241: a call ~10 MB)
PREFETCH_EVERY = 3     # seconds between those calls: 20 a minute, the budget's other 5 for a chart opened meanwhile
PART_DAYS = 15         # days a stock's missing minutes are asked for in one call (≤ 5,000 bars: about 20 days)
VOLUME = {"volume", "raw_volume", "amount", "turnover_rate"}   # a rule whose formulas read these waits out the first minutes (§7.2)
BUYING = ("research", "watching")   # 待研究、观察中: the buying side's alerts go to Feishu (design 2026-10-10 §0)
INDEX_NAME = {"CN:IDX:000001": "上证指数", "CN:IDX:399001": "深证成指", "CN:IDX:399006": "创业板指", "JP:IDX:N225": "日经 225",
              "JP:IDX:TOPIX": "TOPIX"}
LABEL = {"box": "箱顶", "fail": "失效线", "line": "你画的线"}
FAST, AUCTION_EVERY, LATE = 3, 15, 15   # 快线 (design 2026-10-10 §5): seconds between quotes; before the open; a quote's age that is 行情延迟
VIEW, VIEWED = timedelta(minutes=10), 10   # K 线 for any stock (design 2026-10-12 §9): how long a look keeps a stock polled; how many
# what the same alerts did over a year of A-share minutes (design 2026-10-10 §1), for the card
# measured on A shares' 1-minute bars (61 sampled days, design 2026-10-10 §1; the buying side at the box top: ledger 2026-10-07)
HISTORY = {("near", "up"): "同类提醒约六成当天碰线，中位提前 7 分钟", ("near", "down"): "同类提醒近六成当天跌到线，中位提前 15 分钟",
           ("touch", "up"): "碰线的日子约一半收在线上", ("touch", "down"): "碰线的日子约六成收在线下"}


WAVE_TAG = {("up", "新高量不足"): "新高量不足：后量不超前量", ("down", "下跌放量"): "下跌放量：这一波跌得比上一波量大"}


def tags(up: bool, vwap_gap: float | None, to_limit: float | None, weak: bool, late: bool, wave: str | None) -> list[str]:
    """What to be warned of at this moment, for the card (they never stop an alert): the main index down 1%
    (大盘弱); 5 points and more above the average price (and in the last 30 minutes the next day tends to fall back,
    as the page's note says); 2 points from the limit-up (可能封板); the open wave's
    flag (micro.waves, the 分时 detail's): a new high on less volume than the high before (后量不超前量) on the
    buying side, a fall on more volume than the fall before on the selling side."""
    out = ["大盘弱"] * weak
    if up and (vwap_gap or 0) >= 0.05:
        out.append("远离均价线 5 个点以上" + ("，尾盘还这样，次日容易回落" if late else "")
                   + "（经验说法，没有经过检验）")   # as the page's note says it (live.ts vwapNote)
    if up and to_limit is not None and to_limit <= 0.02:
        out.append("可能封板")
    if (t := WAVE_TAG.get(("up" if up else "down", wave))) is not None:
        out.append(t)
    return out


def _vol3(market: str, three: list[dict], avg5: float | None, total: int, clock: tuple[float, ...] | None) -> float | None:
    """The last minutes' volume against what those minutes of the day usually trade: the 5-day average day × the share
    of a day the market's volume clock gives them (volume is compared on one period against one reference — the open's minutes are always heavy)."""
    if not avg5 or not three:
        return None
    sessions = load_market(market).sessions
    k = [pl.select(minute_index(sessions, pl.lit(r["at"]))).item() for r in (three[0], three[-1])]
    cum = lambda n: 0.0 if n <= 0 else share(clock, n, total)   # noqa: E731 - the share of a day done by n minutes
    usual = avg5 * (cum(k[1] + 1) - cum(k[0]))
    return sum(r["volume"] for r in three) / usual if usual > 0 else None


def box_top(detail: str | None) -> float | None:
    """A watch row's break line (its box's top × (1 + tol)) from the explanation the daily scan kept, on the session
    after the scan's: a slanted small box's line (a 金品's) a bar further along it."""
    if not detail:
        return None
    x = next((d for d in json.loads(detail).get("distances", []) if d.get("id") == "break_up"), None)
    return float(x["threshold"]) + float(x.get("per_bar") or 0) if x and x.get("threshold") is not None else None


def _price(v: float, market: str) -> str:
    return f"{v:,.{2 if market == 'CN' else 1}f}".rstrip("0").rstrip(".")


def _write(df: pl.DataFrame, path: Path, row_group: int | None = None) -> None:
    tmp = tmp_for(path)
    df.write_parquet(tmp, row_group_size=row_group)
    move_into_place(tmp, path, tries=20)   # a reader never sees half a file; on Windows one still reading it holds it a moment


def _merge(kept: pl.DataFrame, got: pl.DataFrame) -> pl.DataFrame:
    """A day's minutes kept and those just fetched: one source gives each minute's turnover (TickFlow), another not."""
    return pl.concat([kept, got], how="diagonal_relaxed")


def _read(path: Path) -> pl.DataFrame:
    """An intraday file read whole and let go at once (never memory-mapped: Windows cannot replace a mapped file)."""
    return pl.read_parquet(io.BytesIO(path.read_bytes()))


def _read_one(path: Path, listing_id: str) -> pl.DataFrame:
    """One stock's rows of an intraday file: a past day's holds the whole market once fetched ahead (10-13, 1.3 million
    rows), kept in listing order in small row groups — only those that can hold the stock are read."""
    return pl.scan_parquet(path).filter(pl.col("listing_id") == listing_id).collect()


def _days_of(paths_, market: str) -> list[date]:
    store = Store(paths_, market)
    return store.read("calendar").filter(pl.col("is_open"))["date"].sort().to_list() if store.latest() else []


def source_for(paths: Paths, market: str):
    autoload("wb.realtime.providers")
    name = ROUTES[paths.profile][market]
    if paths.profile == "prod" and market == "CN" and secret_source("tickflow_api_key"):
        name = "tickflow_rt"   # real minutes with their turnover, not rt_k's day so far sampled each poll
    return REALTIME[name](market)


class Live:
    def __init__(self, paths_of: Callable[[str], Paths], *, source_for: Callable = source_for,
                 publish: Callable[[dict], None] | None = None, send: Callable[[], None] | None = None, markets=MARKETS,
                 allow_demo: bool = False):
        """`send`: send the outbox now (an alert is not left for the scheduler's next round, design 2026-10-10 §5);
        `allow_demo`: poll on demo data too (tests); the app never sends `wb devdata`'s market to a real source."""
        self.paths_of, self.source_for, self.markets, self.allow_demo = paths_of, source_for, tuple(markets), allow_demo
        self.publish, self.send = publish or (lambda e: None), send or (lambda: None)
        self._state: dict[str, dict] = {}
        self._polled: dict[str, datetime] = {}
        self._clocks: dict[tuple, tuple[float, ...] | None] = {}   # (market, day) -> the day's volume clock
        self._said: dict[str, tuple] = {}   # the session each market's page last heard
        self._checked: dict[tuple, dict[str, datetime]] = {}   # (market, day) -> each line's last minute judged
        self._boxes: dict[tuple, dict[str, dict | None]] = {}   # (market, snapshot) -> each watched stock's own box
        self._upto: dict[tuple, int] = {}    # (market, day) -> the minutes the day's data covers
        self._down: dict[str, date] = {}     # a market whose feed was said to have stopped (the day it was)
        self._zone: dict[tuple, dict[str, dict]] = {}   # (market, day) -> the lines a price is within the zone of, untouched
        self._boxes_of: dict[str, dict[str, list]] = {}   # market -> each stock's box lines: every 潜伏 rule's, or its own box
        self._fasted: dict[str, datetime] = {}  # when each market's quotes were last asked for
        self._error: dict[str, str] = {}     # a market whose source failed: why (the page says 行情中断, not stale prices)
        self._tried: set[tuple] = set()      # (market, day, listing) a history fetch was made for: never asked again
        self._filled: dict[str, datetime] = {}   # when each market's days before were last looked over
        self._ahead: dict[str, datetime] = {}    # when each market's minutes were last fetched ahead
        self._ahead_asked: set[tuple] = set()    # (market, window's first day, listing) fetched ahead this run
        self._ahead_got: dict[tuple, list[pl.DataFrame]] = {}   # (market, window) -> its batches so far, written at its end
        self._stop = threading.Event()
        self._saying = threading.Lock()   # a line's alerts are said by one loop at a time: the poll's minutes or the fast path
        self.tape = MarketTape(paths_of, source_for, lambda e: self.publish(e))   # 市场看板 (design 2026-10-12)
        self._viewing: dict[str, datetime] = {}   # a stock opened on the K-line page -> when (design 2026-10-12 §9)
        self._view_fetched: set[tuple] = set()    # (market, day, listing) whose minutes were looked for
        self._scans: dict[str, dict] = {}   # market -> where each judged stock's box machines stood after the bars before today
        self._past_of: dict[str, dict] = {}   # market -> (profile, snapshot, day, stocks) -> each one's last close before the day
        self._basis: dict[str, tuple] = {}   # market -> ((profile, snapshot, day), day_basis): today's factors' inputs

    # ---- what is followed ---------------------------------------------------------------------------------------
    @staticmethod
    def _latent(paths: Paths, market: str) -> list[tuple]:
        """(rule, version, its 潜伏 and signal rows) of each enabled rule's latest daily scan in `market`."""
        conn, out = connect(paths.app_db, "app"), []
        for rule, version in enabled_rules(paths):
            run = conn.execute("SELECT id FROM rule_run WHERE rule_id=? AND market=? AND mode='date' AND pool='null' "
                               "ORDER BY day DESC, created_at DESC LIMIT 1", (rule.id, market)).fetchone()
            path = paths.profile_dir / "runs" / f"{run['id']}.parquet" if run else None
            if path is not None and path.exists():
                out.append((rule, version, pl.read_parquet(path).filter(pl.col("status").is_in(["watch", "signal"]))))
        return out

    def _latent_rows(self, market: str) -> tuple[dict[str, dict], dict[str, list]]:
        """Each stock on an enabled rule's 潜伏 list (its latest daily scan): its row — rule and rule_id (the first rule
        whose list has it), top (the box's frozen top edge, 上沿, in adjusted prices: the line watched) and line (its
        break line: the scan keeps it, the edge × (1 + tol)), stage — and every rule's line of it."""
        rows: dict[str, dict] = {}
        boxes: dict[str, list] = {}   # every rule's line of a stock (the row shows the first rule's)
        for rule, _, latent in self._latent(self.paths_of(market), market):
            tol, named = box_tol(rule), names(rule)["stages"]
            for r in latent.iter_rows(named=True):
                d = json.loads(r["detail"]) if r.get("detail") else {}
                line = box_top(r.get("detail"))
                top = line / (1 + tol) if line is not None else None
                if top is not None:   # watched at the top itself, not its break line (ledger 2026-10-07, 2026-10-10)
                    boxes.setdefault(r["listing_id"], []).append((rule.id, rule.name, top))
                stage = d.get("deepest_stage")   # shown, by its name (names(): 中箱, 金品 …), never the rule's id for it
                rows.setdefault(r["listing_id"], {"listing_id": r["listing_id"], "rule": rule.name, "rule_id": rule.id,
                                                  "top": top, "line": line, "stage": named.get(stage, stage) if stage else None})
        return rows, boxes

    def followed(self, market: str) -> pl.DataFrame:
        """The stocks 盯盘 follows — yours (10-12): those in your watch groups and those you drew a line on with 提醒
        ticked (design 2026-10-10 §2). listing_id, rule and rule_id (the first enabled rule whose 潜伏 list has it, with
        its line: top, line, stage — see _latent_rows), watched (in one of your groups), status (its 观察条目's); a stock
        观察中 or 待研究 on no 潜伏 list has its own box's (the mid or long box it is in at its last close; stage 中箱 / 大箱).
        A 潜伏 stock you do not watch is not followed: the 潜伏 list shows it by the whole-market quote (latent())."""
        paths = self.paths_of(market)
        ws = Workspace(paths)
        latent, every = self._latent_rows(market)
        mine = [item["listing_id"] for g in ws.groups() for item in ws.items(g["id"]) if item["listing_id"].startswith(f"{market}:")]
        ids = list(dict.fromkeys([lid for lid in latent if lid in set(mine)] + mine + ws.alerted(market)))
        empty = {"rule": None, "rule_id": None, "top": None, "line": None, "stage": None}
        rows = {lid: {"listing_id": lid, **(latent.get(lid) or empty), "watched": lid in set(mine)} for lid in ids}
        boxes = {lid: every[lid] for lid in ids if lid in every}
        statuses = Watch(self.paths_of).statuses(market)
        for lid, r in rows.items():
            r["status"] = statuses.get(lid)
        # its own box: a stock 观察中 or 待研究 on no 潜伏 list (design 2026-10-10 §2) — on one, the rules' lines stand
        for lid, b in self._own(market, [lid for lid, r in rows.items() if lid not in latent and r["status"] in BUYING]).items():
            rows[lid] |= {"top": b["top"], "line": b["line"], "stage": b["scale"]}
            boxes[lid] = [("own", b["scale"], b["top"])]
        self._boxes_of[market] = boxes
        return pl.DataFrame(list(rows.values()), schema={"listing_id": pl.Utf8, "rule": pl.Utf8, "rule_id": pl.Utf8,
                                                          "top": pl.Float64, "line": pl.Float64, "stage": pl.Utf8,
                                                          "watched": pl.Boolean, "status": pl.Utf8})

    def latent(self, market: str, now: datetime | None = None) -> dict:
        """The 潜伏 stocks you do not watch (10-12: 盯盘 follows yours only) — seen, never judged: each one's rule and stage,
        how far it is from its box top at the day's whole-market quote (every A share, a minute old at most; what the
        K-line's forming bar reads) with the quote's time; the latest close outside the session or before the quote has it."""
        s = self.session(market, now or datetime.now().astimezone())
        paths, (latent, _) = self.paths_of(market), self._latent_rows(market)
        store, mine = Store(paths, market), set(self.followed(market)["listing_id"].to_list())
        snap = store.latest()
        day = self._snapshot(market, s.day) if s.state != "closed" and not (snap and snap.as_of >= s.day) else None
        f = pl.DataFrame([r for lid, r in latent.items() if lid not in mine], schema={
            "listing_id": pl.Utf8, "rule": pl.Utf8, "rule_id": pl.Utf8, "top": pl.Float64, "line": pl.Float64, "stage": pl.Utf8})
        session = {"state": s.state, "label": s.label, "day": s.day.isoformat(), "elapsed": s.elapsed, "total": s.total}
        if f.is_empty():
            return {"market": market, "session": session, "rows": []}
        q = (day.select("listing_id", pl.col("last").alias("now"), pl.col("pre_close").alias("pc"), pl.col("at"))
             if day is not None else pl.DataFrame(schema={"listing_id": pl.Utf8, "now": pl.Float64, "pc": pl.Float64, "at": pl.Datetime}))
        hist = self._past(paths, store, market, f["listing_id"].to_list(), s.day if day is not None else s.day + timedelta(days=1))
        hist = self._on_day(market, s.day, hist, day) if day is not None else hist
        names = dict(store.read("instruments").select("listing_id", "name").iter_rows()) if snap else {}
        rows = (f.join(hist, on="listing_id", how="left").join(q, on="listing_id", how="left")
                .with_columns(pl.col("top") / pl.col("factor"), pl.col("line") / pl.col("factor"), pl.coalesce("now", "prev").alias("last"))
                .with_columns(pl.when(pl.col("now").is_not_null()).then(pl.col("last") / pl.col("pc") - 1)
                              .otherwise(pl.col("move") - 1).alias("chg"), (pl.col("last") / pl.col("top") - 1).alias("to_top")))
        out = [{**r, "name": names.get(r["listing_id"]), "at": r["at"].isoformat(timespec="seconds") if r["at"] else None}
               for r in rows.select("listing_id", "rule", "rule_id", "stage", "top", "line", "last", "chg", "to_top", "at").to_dicts()]
        return {"market": market, "session": session, "rows": out}

    def _own(self, market: str, ids: list[str]) -> dict[str, dict]:
        """Each stock's own box (lines.own_boxes), worked out once per snapshot."""
        snap = Store(self.paths_of(market), market).latest()
        if snap is None or not ids:
            return {}
        self._boxes = {k: v for k, v in self._boxes.items() if k[0] != market or k[1] == snap.id}
        kept = self._boxes.setdefault((market, snap.id), {})
        if need := [x for x in ids if x not in kept]:
            got = own_boxes(self.paths_of(market), market, need)
            kept.update({x: got.get(x) for x in need})
        return {x: kept[x] for x in ids if kept.get(x)}

    # ---- the day's numbers ---------------------------------------------------------------------------------------
    @staticmethod
    def _history(paths: Paths, store: Store, market: str, ids: list[str], day: date) -> pl.DataFrame:
        """Each listing's last close before `day`, read as formulas read it: prev (the exchange's price), factor
        (adjusted ÷ exchange price: the scan's box top, in adjusted prices, divided by it is in that day's prices),
        move (that close on the one before, adjusted: a split between them is no fall), avg5 (the average volume of
        the five bars to it)."""
        snap = store.latest()
        if snap is None or not ids:
            return pl.DataFrame(schema={"listing_id": pl.Utf8, "prev": pl.Float64, "factor": pl.Float64, "move": pl.Float64,
                                        "avg5": pl.Float64})
        bars = (build_frame(store, market, paths.profile, {"close", "raw_close", "raw_volume"}, ids, snap=snap)
                .filter(pl.col("date") < day).drop_nulls(["close", "raw_close"]).sort("listing_id", "date"))
        # ponytail: the exchange's volumes, so a split inside the five days skews the volume ratio until it rolls out
        return bars.group_by("listing_id").agg(
            pl.col("raw_close").last().alias("prev"), (pl.col("close").last() / pl.col("raw_close").last()).alias("factor"),
            (pl.col("close").last() / pl.col("close").tail(2).first()).alias("move"), pl.col("raw_volume").tail(5).mean().alias("avg5"))

    def _day_basis(self, market: str, day: date) -> tuple[pl.DataFrame, pl.DataFrame] | None:
        """day_basis (each listing's last stored bar, the day's corporate actions), read once per snapshot and day."""
        paths = self.paths_of(market)
        store = Store(paths, market)
        snap = store.latest()
        if snap is None:
            return None
        key = (paths.profile_dir, snap.id, day)
        if (got := self._basis.get(market)) is None or got[0] != key:
            got = self._basis[market] = (key, day_basis(store, day))
        return got[1]

    def _on_day(self, market: str, day: date, hist: pl.DataFrame, quote: pl.DataFrame | None) -> pl.DataFrame:
        """_history in `day`'s prices while the day trades unpublished (review 10-13): on an ex-date — the exchange's 昨收 in
        the whole-market quote (`quote`: its reference price) moved off the close before, or the day's corporate action
        on file — the factor and close before become today's (day_factors), so the box top, 失效线 and your lines stand
        in today's prices and the change is against the exchange's reference, not a 送转 / dividend off. `day_ratio`:
        today's factor ÷ the day before's (1 on an ordinary day)."""
        basis = self._day_basis(market, day)
        acts = basis[1] if basis is not None else pl.DataFrame(schema={"listing_id": pl.Utf8, "_ratio": pl.Float64, "_cash": pl.Float64})
        x = hist.select("listing_id", pl.col("prev").alias("_close"), pl.col("factor").alias("_adj"),
                        pl.lit(None, pl.Float64).alias("_share")).join(acts, on="listing_id", how="left")
        if quote is not None and "pre_close" in quote.columns:
            x = x.join(quote.select("listing_id", "pre_close"), on="listing_id", how="left")
        today = day_factors(x, market).select("listing_id", (pl.col("adj_factor") / pl.col("_adj")).alias("day_ratio"))
        return (hist.join(today, on="listing_id", how="left").with_columns(pl.col("day_ratio").fill_null(1.0))
                .with_columns(pl.col("factor") * pl.col("day_ratio"), pl.col("prev") / pl.col("day_ratio")))

    def _factored(self, market: str, day: date, bars: pl.DataFrame) -> pl.DataFrame:
        """Today's temporary bars with today's factors (today.day_factors): an ex-date's change before it is published."""
        basis = self._day_basis(market, day)
        return with_day_factors(bars, market, basis) if basis is not None and not bars.is_empty() else bars

    def _past(self, paths: Paths, store: Store, market: str, ids: list[str], day: date) -> pl.DataFrame:
        """_history, worked out once per snapshot, day and stocks followed (it reads every bar before the day); the
        last two kept."""
        snap = store.latest()
        key = (paths.profile_dir, snap.id if snap else None, day, frozenset(ids))
        kept = self._past_of.setdefault(market, {})
        if (got := kept.get(key)) is None:
            got = kept[key] = self._history(paths, store, market, ids, day)
            for k in list(kept)[:-2]:
                kept.pop(k, None)
        return got

    @staticmethod
    def _latest(paths: Paths, day: date, market: str) -> dict[str, dict]:
        """Each stock's latest alert of the day."""
        return {a["listing_id"]: {k: a[k] for k in ("kind", "text", "status", "at")} for a in Alerts(paths).day(day, market)}

    def _bars(self, market: str, s: Session, minutes: pl.DataFrame, snap: pl.DataFrame | None) -> pl.DataFrame:
        """Today's temporary bars: a snapshot source's day as it reports it, else put together from the minutes."""
        upto = self._covered(market, minutes, s)
        clock = self._clock(market, s.day)
        return self._factored(market, s.day, snapshot_bars(snap, s.day, s.total, upto, clock) if snap is not None else
                              temp_bars(self._stocks(minutes), s.day, s.total, upto, clock))

    def _clock(self, market: str, day: date) -> tuple[float, ...] | None:
        """The market's volume clock (design 2026-10-10 §6), worked out once a day."""
        if (market, day) not in self._clocks:
            self._clocks = {k: v for k, v in self._clocks.items() if k[0] != market}
            self._clocks[(market, day)] = clock_for(self.paths_of(market), market)
        return self._clocks[(market, day)]

    def _event(self, market: str, s: Session, minutes: pl.DataFrame | None, source, snap: pl.DataFrame | None = None) -> dict:
        paths = self.paths_of(market)
        store = Store(paths, market)
        f = self.followed(market)
        names = dict(store.read("instruments").select("listing_id", "name").iter_rows()) if store.latest() else {}
        live = minutes is not None and minutes.height > 0
        hist = self._past(paths, store, market, f["listing_id"].to_list(), s.day if live else s.day + timedelta(days=1))
        day_snap = snap if snap is not None else (self._snapshot(market, s.day) if live else None)
        hist = self._on_day(market, s.day, hist, day_snap) if live else hist.with_columns(pl.lit(1.0).alias("day_ratio"))
        rows = f.join(hist, on="listing_id", how="left").with_columns(pl.col("top") / pl.col("factor"), pl.col("line") / pl.col("factor"))
        vol = pl.col("volume").sum()
        now = (minutes.sort("at").group_by("listing_id").agg(
            pl.col("close").last().alias("now"), vol,
            pl.when(vol > 0).then(turnover(minutes).sum() / vol).alias("vwap"))   # the 分时图's yellow line
               if live else pl.DataFrame(schema={"listing_id": pl.Utf8, "now": pl.Float64, "volume": pl.Float64, "vwap": pl.Float64}))
        if day_snap is not None and day_snap.height:   # a snapshot source's day as it reports it: the average is exact —
            # unless amount ÷ volume falls outside the day's range (another unit; the daily bars' vwap_range check)
            exact = day_snap.filter((pl.col("volume") > 0) & ~_vwap_outside()).select(
                "listing_id", (pl.col("amount") / pl.col("volume")).alias("_vw"))
            now = now.join(exact, on="listing_id", how="left").with_columns(pl.coalesce("_vw", "vwap").alias("vwap")).drop("_vw")
        upto = self._covered(market, minutes, s) if live else 1
        rows = rows.join(now, on="listing_id", how="left").with_columns(  # not polled (or outside the session): the latest close
            pl.col("now").is_not_null().alias("live"), pl.coalesce("now", "prev").alias("last"),
            pl.when(pl.col("now").is_not_null()).then(pl.col("prev")).otherwise(pl.col("prev") / pl.col("move")).alias("prev"),
            (pl.col("volume") / share(self._clock(market, s.day), upto, s.total) / pl.col("avg5")).alias("vol_ratio"))
        rows = rows.with_columns((pl.col("last") / pl.col("prev") - 1).alias("chg"),
                                 (pl.col("last") / pl.col("top") - 1).alias("to_top"),   # +1.2%: 1.2% above the top
                                 ((pl.col("last") - pl.col("vwap")) / pl.col("prev")).alias("vwap_gap"))   # points, as the axis reads
        ex = ex_rights(paths, market, self._bars(market, s, minutes, snap)) if live else set()
        latest = self._latest(paths, s.day, market)
        out = [{**r, "name": names.get(r["listing_id"]), "ex_rights": r["listing_id"] in ex, "alert": latest.get(r["listing_id"])}
               for r in rows.select("listing_id", "rule", "rule_id", "watched", "status", "stage", "top", "line", "last", "prev", "chg",
                                    "vol_ratio", "to_top", "vwap", "vwap_gap", "live", "factor", "day_ratio", "avg5").to_dicts()]
        at = s.now
        if live:   # the prices' time (design 2026-10-12 §14), not the poll's: a source answering with old minutes ages
            end = minutes["at"].max() + timedelta(minutes=1 + (getattr(source, "delay", 0) if source else 0))
            at = min(at, end.replace(tzinfo=at.tzinfo))
        return {"type": "quotes", "market": market, "at": at.isoformat(timespec="seconds"),
                "session": {"state": s.state, "label": s.label, "day": s.day.isoformat(), "elapsed": s.elapsed, "total": s.total},
                "source": {"name": source.name, "delay": source.delay, "approx_volume": source.approx_volume,
                           "approx_minutes": getattr(source, "kind", "minutes") == "snapshot"} if source else None,
                "error": self._error.get(market), "quote_at": self._quote_at(market),
                "rows": out, "indexes": self._indexes(store, s, minutes)}

    def _quote_at(self, market: str) -> str | None:
        """The whole-market quote's time (the market board's last fetch) — what the K-line's forming bar and 观察 read:
        beside the poll's own time, the status bar says when either stops."""
        x = self.tape.latest(market)
        return x.get("at") if x else None

    @staticmethod
    def _indexes(store: Store, s: Session, minutes: pl.DataFrame | None) -> list[dict]:
        ids = INDEXES[s.market]
        daily = (store.scan("index_bars_1d").filter(pl.col("index_id").is_in(ids)).select("index_id", "date", "close")
                 .sort("index_id", "date").collect()) if store.latest() else pl.DataFrame(
            schema={"index_id": pl.Utf8, "date": pl.Date, "close": pl.Float64})
        out = []
        for iid in ids:
            d = daily.filter(pl.col("index_id") == iid)
            live = minutes.filter(pl.col("listing_id") == iid).sort("at") if minutes is not None else None
            if live is not None and live.height:
                prev = d.filter(pl.col("date") < s.day)["close"]
                last = live["close"][-1]
                out.append({"listing_id": iid, "last": last, "chg": last / prev[-1] - 1 if prev.len() else None, "live": True})
            elif d.height:
                out.append({"listing_id": iid, "last": d["close"][-1], "chg": d["close"][-1] / d["close"][-2] - 1 if d.height > 1 else None,
                            "live": False})
        return out

    # ---- polling ----------------------------------------------------------------------------------------------------
    def session(self, market: str, now: datetime) -> Session:
        store = Store(self.paths_of(market), market)
        cal = store.read("calendar") if store.latest() else pl.DataFrame(schema={"date": pl.Date, "is_open": pl.Boolean})
        opens = dict(zip(cal["date"], cal["is_open"], strict=True))
        return session_at(market, now, lambda d: opens.get(d))

    def _ranked(self, market: str, s: Session) -> list[str]:
        """The stocks to ask the source for, most wanted first (a source takes only so many): yours, then the rest by
        how near they are to their box tops at the last prices seen."""
        seen = sorted((self._state.get(market) or self._event(market, s, None, None))["rows"],
                      key=lambda r: (not r["watched"], r["to_top"] is None, -(r["to_top"] or 0)))
        ids = self.followed(market)["listing_id"].to_list()
        known = [r["listing_id"] for r in seen if r["listing_id"] in ids]
        viewed = self._viewed(market, s.now)   # the stocks you are looking at come first (design 2026-10-12 §9)
        return viewed + [x for x in known + [x for x in ids if x not in known] if x not in viewed]

    # ---- K 线 for any stock (design 2026-10-12 §9) --------------------------------------------------------------------
    def view(self, listing_id: str, now: datetime | None = None) -> None:
        """A stock opened on the K-line page (its chart or its 分时): polled first for VIEW, the VIEWED latest at most;
        the market board fetches every 10 s meanwhile, so its forming bar keeps up."""
        now = now or datetime.now().astimezone()
        self._viewing[listing_id] = now
        latest = sorted(((t, lid) for lid, t in self._viewing.items() if now - t <= VIEW), reverse=True)[:VIEWED * len(MARKETS)]
        self._viewing = {lid: t for t, lid in latest}
        self.tape.want(parse_id(listing_id)[0], now)

    def _viewed(self, market: str, now: datetime) -> list[str]:
        """The stocks of `market` looked at within VIEW, the latest first, VIEWED at most."""
        mine = sorted(((t, lid) for lid, t in self._viewing.items() if lid.startswith(f"{market}:") and now - t <= VIEW), reverse=True)
        return [lid for _, lid in mine[:VIEWED]]

    def _viewed_minutes(self, market: str, s: Session, src) -> None:
        """A stock looked at without minutes today gets them now — one call, once a day — not at the next poll; only
        fetched (盯盘 alerts on your stocks alone). The page hears it."""
        if s.state != "trading":
            return
        todo = [lid for lid in self._viewed(market, s.now) if (market, s.day, lid) not in self._view_fetched]
        if not todo:
            return
        path = self._file(market, s.day)
        have = set(_read(path)["listing_id"].unique().to_list()) if path.exists() else set()
        self._view_fetched |= {(market, s.day, lid) for lid in todo}
        need = [lid for lid in todo if lid not in have]
        if not need:
            return
        got = src.minutes(need, s.day, ()).filter(in_session(load_market(market).sessions, pl.col("at")))
        if got.height:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write((_merge(_read(path), got) if path.exists() else got).unique(["listing_id", "at"], keep="last")
                   .sort("listing_id", "at"), path)
            self.publish(self.state(market, s.now))

    def minutes_of(self, listing_id: str, days: int, now: datetime | None = None) -> pl.DataFrame:
        """A stock's 1-minute bars of its last `days` trading days to today, for the minute K-line (10-12): the kept ones
        (盯盘's, a look's); the past days it has none of, fetched once in one call (minutes_between, else a call a day)
        and kept; today's as polled, its last minute moved by the whole-market quote when that is newer — the bar
        forming moves between polls."""
        market = parse_id(listing_id)[0]
        s = self.session(market, now or datetime.now().astimezone())
        paths, empty = self.paths_of(market), pl.DataFrame(schema=MINUTES)
        store = Store(paths, market)
        snap = store.latest()
        if snap is None:
            return empty
        past = store.read("calendar").filter(pl.col("is_open") & (pl.col("date") < s.day))["date"].sort().to_list()
        want = [*past, *([s.day] if s.state != "closed" else [])][-days:]   # today when the session says it trades

        def of(d: date) -> pl.DataFrame:
            path = self._file(market, d)
            return _read_one(path, listing_id) if path.exists() else empty
        kept = {d: of(d) for d in want}
        real = self.allow_demo or snap.note != DEMO   # demo data is never sent to a real source, as the poll
        src = self.source_for(paths, market) if real else None
        sessions = load_market(market).sessions
        # today counts once its morning or its day is over and the poll's late window has passed (the poll fills it till
        # then: 盯盘's stocks and those looked at) — a stock first opened in the evening has today too
        ended = [b for _, b in sessions if s.now.time() >= b]
        over = src is not None and s.state in ("lunch", "after") and bool(ended) and \
            s.now > datetime.combine(s.day, max(ended), tzinfo=s.now.tzinfo) + timedelta(minutes=src.delay + 5)

        def upto(d: date) -> clock_time:   # the last minute of a day kept whole (today: of its part over)
            return (datetime.combine(d, sessions[-1][1] if d < s.day else max(ended)) - timedelta(minutes=1)).time()
        # a source giving a range in one call (TickFlow) also fills a day kept only in part — 盯盘 began late or stopped
        # (10-12: the app closed at 13:30, its 13:29 kept mid-minute) — a day not run open to close; others the empty ones
        part = hasattr(src, "minutes_between")
        missing = [d for d in want if real and (d < s.day or over) and (market, d, listing_id, upto(d)) not in self._tried and (
            kept[d].is_empty() or part and not (kept[d]["at"].min().time() <= sessions[0][0] and kept[d]["at"].max().time() >= upto(d)))]
        if missing:
            self._tried |= {(market, d, listing_id, upto(d)) for d in missing}   # once: what the source has not is not asked again
            try:   # a range: in parts of PART_DAYS days (a call gives a stock ≤ 5,000 bars, about 20 days)
                got = (pl.concat([src.minutes_between([listing_id], w[0], w[-1]) for w in
                                  (missing[k:k + PART_DAYS] for k in range(0, len(missing), PART_DAYS))], how="diagonal_relaxed")
                       if part else pl.concat([src.minutes([listing_id], d, ()) for d in missing], how="diagonal_relaxed"))
            except Exception as e:  # noqa: BLE001 - the chart shows the days it has
                log.warning("live %s: minutes of %s: %s", market, listing_id, e)
                got = empty
            got = got.filter(in_session(sessions, pl.col("at")))
            for d in missing:
                if (part := got.filter(pl.col("at").dt.date() == d)).height:
                    path = self._file(market, d)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _write((_merge(_read(path), part) if path.exists() else part).unique(["listing_id", "at"], keep="last")
                           .sort("listing_id", "at"), path)
            kept |= {d: of(d) for d in missing}
        parts = [x for x in kept.values() if x.height]
        out = pl.concat(parts, how="diagonal_relaxed").sort("at") if parts else empty
        q = self._snapshot(market, s.day) if s.day in want and s.state != "closed" and snap.as_of < s.day else None
        row = q.filter(pl.col("listing_id") == listing_id) if q is not None else None
        if row is not None and row.height and row["last"][0] is not None:   # the quote newer than the last minute kept
            minute, last = row["at"][0].replace(second=0, microsecond=0), row["last"][0]
            today = out.filter(pl.col("at").dt.date() == s.day)
            if today.is_empty() or today["at"].max() < minute:
                out = pl.concat([out, pl.DataFrame([(listing_id, minute, last, last, last, last, 0.0)], schema=MINUTES, orient="row")],
                                how="diagonal_relaxed")
            elif today["at"].max() == minute:
                hit = pl.col("at") == minute
                out = out.with_columns(pl.when(hit).then(pl.lit(last)).otherwise(pl.col("close")).alias("close"),
                                       pl.when(hit).then(pl.max_horizontal("high", pl.lit(last))).otherwise(pl.col("high")).alias("high"),
                                       pl.when(hit).then(pl.min_horizontal("low", pl.lit(last))).otherwise(pl.col("low")).alias("low"))
        return out.sort("at")

    def _file(self, market: str, day: date) -> Path:
        return self.paths_of(market).market_dir(market) / "intraday" / f"date={day}.parquet"

    @staticmethod
    def _stocks(minutes: pl.DataFrame) -> pl.DataFrame:
        return minutes.filter(~pl.col("listing_id").str.contains(":IDX:"))

    @staticmethod
    def _covered(market: str, minutes: pl.DataFrame, s: Session) -> int:
        """The minutes the data covers, to its last one (a late source's are fewer than the clock's)."""
        return min(minutes.select(minute_index(load_market(market).sessions, pl.col("at").max())).item() + 1, s.total)

    def _alerts(self, market: str, s: Session, minutes: pl.DataFrame, event: dict, snap: pl.DataFrame | None = None) -> list[dict]:
        """Spec §7.2, this poll's alerts: each 潜伏 stock whose rule is met on today's temporary bar (volume scaled to the
        day; a rule that reads volume waits out the first minutes), then each price line crossed in the new minutes — a
        line you drew, a 潜伏 box's top (upward only, and not when its rule already alerted). A stock you muted today, or
        one beyond its day's limits, is quiet."""
        paths = self.paths_of(market)
        settings, alerts, day = Workspace(paths).settings(), Alerts(paths), s.day
        stocks = self._stocks(minutes)
        rows = {r["listing_id"]: r for r in event["rows"]}
        quiet = {lid for lid, r in rows.items() if r["ex_rights"]} | alerts.muted(day)
        upto = self._covered(market, minutes, s)
        bars = self._bars(market, s, minutes, snap).filter(   # yours only: a stock looked at on the K-line is fetched, not judged
            pl.col("listing_id").is_in(list(rows)) & ~pl.col("listing_id").is_in(list(quiet)))
        at = s.now.replace(tzinfo=None)
        title = {lid: listing_title(lid, {lid: r["name"]}) for lid, r in rows.items()}
        new = []
        for lid, r in rows.items():   # an ex-rights day the factor has not caught: said once, never judged (design 04)
            if r["ex_rights"] and lid not in alerts.muted(day):
                a = alerts.add(day=day, market=market, listing_id=lid, kind="ex", key="ex", at=at, price=r["last"], title=title[lid],
                               text=f"现价比昨收 {(r['chg'] or 0) * 100:+.1f}%，超出当天的涨跌停幅度：疑似除权")
                if a:
                    new.append(a)
        store = Store(paths, market)
        available = available_columns(store, store.latest())
        told = {(a["key"], a["listing_id"]) for a in alerts.day(day, market) if a["kind"] == "rule"}   # once a day: not judged again
        judge = []   # after the close a rule met on the day's bar is the evening scan's to decide (you chose, 10-13)
        for rule, version, latent in self._latent(paths, market) if s.state != "after" else []:
            waiting = latent.filter(pl.col("status") == "watch")   # 潜伏 only: a stock its rule signalled at the close is no news
            mine = bars.filter(pl.col("listing_id").is_in(waiting["listing_id"].implode())
                               & ~pl.col("listing_id").is_in([lid for key, lid in told if key == rule.id]))
            try:   # one rule's trouble (e.g. edited to leave this market) never stops the others or the quotes
                if not mine.is_empty() and not (upto < int(settings["live.quiet_minutes"]) and Matcher.reads(rule, market, available) & VOLUME):
                    judge.append((rule, version, mine))
            except Exception:  # noqa: BLE001
                log.exception("live %s: rule %s on today's bar", market, rule.id)
        shared = Shared(paths, market, bars, [(rule, mine["listing_id"].to_list()) for rule, _, mine in judge])
        with resumed(self._scans.setdefault(market, {})):   # the bars before today: scanned once a day
            for rule, version, mine in judge:
                try:
                    judged = evaluate_today(paths, rule, market, mine, shared)
                except Exception:  # noqa: BLE001
                    log.exception("live %s: rule %s on today's bar", market, rule.id)
                    continue
                for lid, r in judged.items():
                    if r["status"] != "signal":
                        continue
                    a = alerts.add(day=day, market=market, listing_id=lid, kind="rule", key=rule.id, at=at, price=rows[lid]["last"],
                                   title=title[lid], text=f"{rule.name} v{version} · 盘中满足（量按全天成交节奏折算）", detail={"lines": r["lines"]})
                    if a:
                        new.append(a)
        with self._saying:
            new += self._line_alerts(market, s, stocks, rows, quiet, settings, alerts, title, upto, event["indexes"])
        new += self._market_alerts(market, s, event["indexes"], settings, alerts, at)
        return new

    def _lines(self, market: str, s: Session, rows: dict[str, dict], stocks: pl.DataFrame, settings: dict) -> list[dict]:
        """Today's lines (design 2026-10-10 §2) in today's prices: key, listing_id, kind (box: a box's top;
        fail: a fail line of a stock 持有中; line: one you drew), side (up: the buying side, above the price; down: below
        it), price, note, notify (Feishu too: the buying side of a stock 观察中 or 待研究, the selling side of one 持有中, a
        line you drew on a stock you watch; the rest is said in the app), since (a line set today: from then, on the
        price then). A line already past, or that today's limits cannot reach, is not watched."""
        tz, out = ZoneInfo(load_market(market).timezone), []
        for lid, r in rows.items():
            if not r["factor"] or r["status"] not in BUYING:   # a box's top: the buying side of a stock 观察中 or 待研究
                continue
            kept: list[float] = []
            for key, note, adjusted in self._boxes_of.get(market, {}).get(lid, []):   # each rule's box; one line once
                price = adjusted / r["factor"]
                if not any(abs(price / k - 1) <= 0.002 for k in kept):
                    kept.append(price)
                    out.append({"key": f"box:{key}:{lid}", "listing_id": lid, "kind": "box", "side": "up", "price": price,
                                "note": note, "notify": r["status"] in BUYING, "since": None})
        for c in Watch(self.paths_of).selling_lines(market):
            r, at = rows.get(c["listing_id"]), local(c["created_at"], tz)
            if r is not None and r["factor"]:
                out.append({"key": f"fail:{c['id']}", "listing_id": c["listing_id"], "kind": "fail", "side": "down", "note": c["text"],
                            "price": c["price"] * c["factor"] / r["factor"] if c["factor"] else c["price"], "notify": True,
                            "since": at if at.date() == s.day else None})
        for x in drawn_lines(self.paths_of(market), market, list(rows), s.day):   # priced at the last stored bar's factor
            out.append({**x, "price": x["price"] / (rows[x["listing_id"]].get("day_ratio") or 1.0), "kind": "line", "side": None,
                        "note": x["text"], "notify": rows[x["listing_id"]]["status"] in ALERTING})
        for x in out:
            then = stocks.filter((pl.col("listing_id") == x["listing_id"]) & (pl.col("at") < x["since"])) if x["since"] else None
            x["ref"] = then.sort("at")["close"][-1] if then is not None and then.height else rows[x["listing_id"]]["prev"]
            if x["ref"] is not None and x["side"] is None:   # a line you drew: on whichever side of the price it lies
                x["side"] = "up" if x["price"] > x["ref"] else "down"
        return [x for x in out if x["ref"] is not None and rows[x["listing_id"]]["prev"]
                and (x["price"] > x["ref"] if x["side"] == "up" else x["price"] < x["ref"])
                and reachable(x["listing_id"], x["price"], rows[x["listing_id"]]["prev"])]

    def _line_alerts(self, market: str, s: Session, stocks: pl.DataFrame, rows: dict[str, dict], quiet: set[str], settings: dict,
                     alerts: Alerts, title: dict[str, str], upto: int, indexes: list[dict]) -> list[dict]:
        """Each line's layers (layers.py): 竞价 on the open's minute while the day is young, then 逼近 and 触线 on the
        minutes since the line was last looked at (that minute again: it may have been forming) — the first look of a
        day takes a touch over the whole day and an approach only now; and a 持有中 stock still under its fail line ten
        minutes before the close. Each line speaks once a layer a day; an auction near the line keeps the approach
        quiet, one beyond it the touch too."""
        lines = [x for x in self._lines(market, s, rows, stocks, settings) if x["listing_id"] not in quiet]
        if not lines:
            return []
        day, zone = s.day, settings["live.zone"] / 100
        ids = list({x["listing_id"] for x in lines})
        m = with_stats(stocks.filter(pl.col("listing_id").is_in(ids)), {lid: rows[lid]["prev"] for lid in ids})
        by = {k[0]: g.to_dicts() for k, g in m.group_by("listing_id", maintain_order=True)}
        for k in [k for k in self._checked if k[0] == market and k[1] != day]:   # each day starts its lines afresh
            del self._checked[k]
        checked = self._checked.setdefault((market, day), {})
        said = {(a["kind"], a["key"]): a for a in alerts.day(day, market)}
        sessions = load_market(market).sessions
        opening = datetime.combine(day, sessions[0][0])
        closing = datetime.combine(day, sessions[-1][1]) - timedelta(minutes=10)
        weak = any(i["live"] and i["chg"] is not None and i["chg"] <= -0.01 for i in indexes[:1])   # 大盘弱: the main index down 1%
        new, zone_now = [], {}
        for x in lines:
            lid, key = x["listing_id"], x["key"]
            ms = [r for r in by.get(lid, []) if x.get("since") is None or r["at"] >= x["since"]]
            if not ms:
                continue
            seen, checked[key] = checked.get(key), ms[-1]["at"]
            hits = []
            if settings["live.auction"] and (x.get("since") is None or x["since"] <= opening) and ms[0]["at"] == opening and upto <= 2:
                if how := auction(x, ms[0]["open"], zone):
                    hits.append(("auction", ms[0], how))
            told = said.get(("auction", key))
            how = (told["detail"] or {}).get("how") if told else next((h for k, _, h in hits if k == "auction"), None)
            if seen is None:
                found = judge(x, ms, zone, near=False) or judge(x, ms[-1:], zone, near=how is None)
            else:   # a touch since the last look is a fact, said with its time; an approach is about now — after a gap (an
                # outage, a slow poll) only the last two minutes, never one from minutes ago the price has left since
                fresh = [r for r in ms if r["at"] >= seen]
                found = judge(x, fresh, zone, near=False) or judge(x, fresh[-2:], zone, near=how is None)
            if any(k == "touch" for k, _ in found) or s.state == "after":   # an approach and its touch in one look: the
                found = [h for h in found if h[0] == "touch"]   # touch says it; after the close an approach is moot (10-13)
            hits += [(k, r, None) for k, r in found if not (k == "touch" and how == "beyond")]
            if x["kind"] == "fail" and closing <= s.now.replace(tzinfo=None) < closing + timedelta(minutes=10) and ms[-1]["close"] < x["price"]:
                hits.append(("hold", ms[-1], None))
            for layer, r, h in hits:
                if not settings[f"live.{layer}"] or (layer, key) in said:
                    continue
                text, detail = self._card(market, s, layer, x, r, h, rows[lid], ms, weak)
                a = alerts.add(day=day, market=market, listing_id=lid, kind=layer, key=key, at=r["at"],
                               price=r["open"] if layer == "auction" else r["close"], title=title[lid], text=text, detail=detail)
                if a:
                    new.append(a)
                    said[(layer, key)] = a
            gap = (x["price"] - ms[-1]["close"]) / x["price"] * (1 if x["side"] == "up" else -1)
            if 0 < gap <= zone and ("touch", key) not in said and how != "beyond" and x.get("since") is None:
                zone_now[key] = {"x": x, "row": rows[lid], "weak": weak, "gap": gap, "title": title[lid]}
        self._zone[(market, day)] = zone_now   # for the fast path: a line set today is judged on its minutes only
        return new

    def _fast(self, market: str, s: Session, src) -> None:
        """快线 (design 2026-10-10 §5), for a source with quotes (TickFlow): while the market trades, every FAST seconds
        the stocks within the zone of a line they have not touched today (the nearest 100, one request) — a quote's high
        or low of the day past the line is the touch, said within seconds; and once the auction is done (9:25–9:30),
        every AUCTION_EVERY seconds every stock with a line, its open judged for the auction as soon as it is there."""
        quotes = getattr(src, "quotes", None)
        now = s.now.replace(tzinfo=None)
        window = market == "CN" and s.state == "pre" and clock_time(9, 25) <= now.time() < clock_time(9, 30)
        last = self._fasted.get(market)
        if quotes is None or not (s.state == "trading" or window) or \
                (last is not None and (s.now - last).total_seconds() < (AUCTION_EVERY if window else FAST)):
            return
        paths = self.paths_of(market)
        settings, alerts = Workspace(paths).settings(), Alerts(paths)
        if window:
            rows, stocks = self._rows_now(market, s)
            todo = [{"x": x, "row": rows[x["listing_id"]], "weak": False, "title": listing_title(x["listing_id"], {x["listing_id"]: rows[x["listing_id"]]["name"]})}
                    for x in self._lines(market, s, rows, stocks, settings)] if settings["live.auction"] else []
            muted = alerts.muted(s.day)
            todo = [z for z in todo if z["x"]["listing_id"] not in muted]
        else:
            todo = sorted((self._zone.get((market, s.day)) or {}).values(), key=lambda z: z["gap"])
        ids = list(dict.fromkeys(z["x"]["listing_id"] for z in todo))[:None if window else 100]
        if not ids:
            return
        self._fasted[market] = s.now
        try:
            q = {r["listing_id"]: r for r in quotes(ids).to_dicts()}
        except Exception:  # noqa: BLE001 - the minutes go on every 30 seconds
            log.exception("live %s: quotes", market)
            return
        with self._saying:   # the poll's minutes may be saying this line's touch too
            zone, new = settings["live.zone"] / 100, []
            for z in todo:
                x, row, r = z["x"], z["row"], q.get(z["x"]["listing_id"])
                if r is None or r["last"] is None or r["at"] is None:
                    continue
                m = {"at": r["at"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["last"], "vwap": row.get("vwap")}
                if window:   # an open beyond the day's limits: an ex-rights day whose factor has not come, not judged (§7.2)
                    if r["open"] is None or r["at"].time() < clock_time(9, 25) or not reachable(x["listing_id"], r["open"], row["prev"]) \
                            or not (how := auction(x, r["open"], zone)):
                        continue
                    layer, price = "auction", r["open"]
                else:
                    if not ((r["high"] or 0) >= x["price"] if x["side"] == "up" else (r["low"] or 1e18) <= x["price"]):
                        continue
                    layer, how, price = "touch", None, r["last"]
                text, detail = self._card(market, s, layer, x, m, how, row, [m], z["weak"], quick=True)
                if (now - r["at"]).total_seconds() > LATE:
                    detail["tags"].append("行情延迟")
                if a := alerts.add(day=s.day, market=market, listing_id=x["listing_id"], kind=layer, key=x["key"], at=r["at"], price=price,
                                   title=z["title"], text=text, detail=detail):
                    new.append(a)
                (self._zone.get((market, s.day)) or {}).pop(x["key"], None)
        self._say(market, src, new)

    def _rows_now(self, market: str, s: Session) -> tuple[dict[str, dict], pl.DataFrame]:
        """Each followed stock's row and the day's minutes so far — before the open (or without minutes), the last
        close as the price the lines are measured from."""
        path = self._file(market, s.day)
        minutes = _read(path) if s.state != "closed" and path.exists() else None
        rows = {r["listing_id"]: {**r, "prev": r["prev"] if r["live"] else r["last"]} for r in self._event(market, s, minutes, None)["rows"]}
        return rows, (self._stocks(minutes) if minutes is not None else pl.DataFrame(schema=MINUTES))

    def _card(self, market: str, s: Session, layer: str, x: dict, m: dict, how: str | None, row: dict, ms: list[dict],
              weak: bool, quick: bool = False) -> tuple[str, dict]:
        """An alert's words (with numbers, for the app and Feishu; `what`: without one, for a source that keeps its
        numbers out of notifications) and the moment's facts (design 2026-10-10 §3's card) — from a quote (`quick`),
        no minute's move or volume."""
        up, p, px, clock = x["side"] == "up", x["price"], lambda v: _price(v, market), self._clock(market, s.day)
        label = LABEL[x["kind"]] + (f"（{x['note']}）" if x.get("note") else "")
        plan = "（你的计划以收盘为准）" if x["kind"] == "fail" else ""
        if layer == "auction":
            what = f"{'高' if up else '低'}开{'在' if how == 'beyond' else '到'}{label}{('之上' if up else '之下') if how == 'beyond' else '附近'}"
            text = f"{what}：开 {px(m['open'])}，线 {px(p)}（{(m['open'] / p - 1) * 100:+.1f}%）"
        elif layer == "near":
            what = f"可能{'突破' if up else '跌破'}，注意看：{label}"
            text = f"可能{'突破' if up else '跌破'}，注意看：离{label} {px(p)} {'还差' if up else '还有'} {abs(1 - m['close'] / p) * 100:.1f}%"
        elif layer == "touch":
            what = f"已到{label}{plan}"
            text = f"已到{label} {px(p)}{plan}"
        else:
            what = f"收盘前仍在{label}下：按你的计划收盘走"
            text = f"收盘前仍在{label} {px(p)} 下：按你的计划收盘走"
        prev, i = row["prev"], next(k for k, r in enumerate(ms) if r["at"] == m["at"])
        width = day_limit(x["listing_id"], prev)
        d = {"notify": x["notify"], "layer": layer, "kind": x["kind"], "side": x["side"], "line": p, "how": how, "what": what,
             "move2": None if quick else m["close"] / (ms[i - 2]["close"] if i >= 2 else prev) - 1,
             "vol3": None if quick else _vol3(market, ms[max(0, i - 2):i + 1], row.get("avg5"), s.total, clock),
             "vol_ratio": row.get("vol_ratio"), "vwap_gap": (m["close"] - m["vwap"]) / prev if m.get("vwap") else None,
             "to_limit": ((prev + width - m["close"]) if up else (m["close"] - prev + width)) / prev,
             "history": HISTORY.get((layer, x["side"])) if market == "CN" else None}   # measured on A shares
        wave = None
        if not quick and i >= 1:   # the open wave of the day so far (the waves as the 分时 detail draws them)
            w = waves(pl.DataFrame([{k: r[k] for k in ("at", "high", "low", "volume")} for r in ms[:i + 1]]), WAVE[1])
            wave = w["flag"][-1] if w.height and w["open"][-1] else None
        d["tags"] = tags(up, d["vwap_gap"], d["to_limit"], weak, s.total - s.elapsed <= 30, wave)
        return text, d

    def _market_alerts(self, market: str, s: Session, indexes: list[dict], settings: dict, alerts: Alerts, at: datetime) -> list[dict]:
        """大盘急跌 (design 2026-10-10 §3): one of the market's indexes down `live.crash_pct` today — once a day, to everyone."""
        crash = settings["live.crash_pct"] / 100
        fallen = [i for i in indexes if i["live"] and i["chg"] is not None and i["chg"] <= -crash]
        if not settings["live.crash"] or not fallen:
            return []
        i, name = fallen[0], INDEX_NAME.get(fallen[0]["listing_id"], fallen[0]["listing_id"])
        a = alerts.add(day=s.day, market=market, listing_id=INDEXES[market][0], kind="market", key="crash", at=at, price=i["last"],
                       title=f"{MARKET_NAME[market]} · 大盘", text=f"大盘急跌：{name} 今天 {i['chg'] * 100:.1f}%",
                       detail={"notify": True, "what": f"大盘急跌：{name} 今天跌到 {settings['live.crash_pct']:g}% 以上"})
        return [a] if a else []

    def _from_snapshot(self, market: str, s: Session, snap: pl.DataFrame) -> pl.DataFrame:
        """A snapshot source's minutes, put together (approximate: one sample a poll): each stock at the minute of its
        last trade, the price then as open, high, low and close, and the volume since the minutes already kept."""
        if snap.is_empty():
            return pl.DataFrame(schema=MINUTES)
        path = self._file(market, s.day)
        kept = _read(path) if path.exists() else pl.DataFrame(schema=MINUTES)
        now = snap.select("listing_id", pl.col("at").dt.truncate("1m"), "last", pl.col("volume").alias("_cum"))
        before = (now.select("listing_id", "at").join(kept.select("listing_id", pl.col("at").alias("_t"), "volume"), on="listing_id")
                  .filter(pl.col("_t") < pl.col("at")).group_by("listing_id").agg(pl.col("volume").sum().alias("_kept")))
        return now.join(before, on="listing_id", how="left").select(
            "listing_id", "at", *[pl.col("last").alias(c) for c in ("open", "high", "low", "close")],
            (pl.col("_cum") - pl.col("_kept").fill_null(0)).clip(lower_bound=0).alias("volume")).cast(MINUTES)

    def poll(self, market: str, s: Session) -> dict | None:
        """One call to the source; what it brings is merged into the day's minutes (a poll that brings fewer stocks
        or fewer minutes keeps the rest). A poll that brings nothing — a rate limit, a blip — changes nothing."""
        paths = self.paths_of(market)
        src = self.source_for(paths, market)
        ids, snap = self._ranked(market, s)[:src.max_listings], None
        if getattr(src, "kind", "minutes") == "snapshot":
            snap = src.snapshot(ids, s.day)
            got = self._from_snapshot(market, s, snap)
        else:
            got = src.minutes(ids, s.day, INDEXES[market])
        self._error.pop(market, None)   # the source answered (before its first trade, maybe with nothing)
        got = got.filter(in_session(load_market(market).sessions, pl.col("at")))
        if got.is_empty():
            log.warning("live %s: %s gave no minutes for %s", market, src.name, s.day)
            return None
        path = self._file(market, s.day)
        path.parent.mkdir(parents=True, exist_ok=True)
        minutes = (_merge(_read(path), got) if path.exists() else got).unique(["listing_id", "at"], keep="last")
        _write(minutes.sort("listing_id", "at"), path)
        if snap is not None:   # the day as the source reports it, for the K-line's forming bar (a restart keeps it)
            day_snap = self._snapshot(market, s.day)
            _write(pl.concat([day_snap, snap]).unique("listing_id", keep="last") if day_snap is not None else snap,
                   path.parent / f"snapshot={s.day}.parquet")
        for old in sorted(path.parent.glob("date=*.parquet"))[:-KEEP_DAYS] + sorted(path.parent.glob("snapshot=*.parquet"))[:-1]:
            old.unlink(missing_ok=True)
        self._upto[(market, s.day)] = self._covered(market, minutes, s)
        self._fasted[market] = s.now   # the minutes just came: the fast path looks again FAST seconds from now
        event = self._event(market, s, minutes, src, snap)
        lag = s.elapsed - self._upto[(market, s.day)] - getattr(src, "delay", 0)
        behind = s.state == "trading" and (lag >= 3 or (self._down.get(market) == s.day and lag > 1))   # as _feed says it
        try:   # the quotes go out whatever the alerts' trouble; 3 minutes or more behind the clock 盘中提醒暂停 — nothing
            # judged on minutes that old until the data catches up, a touch in the gap said then with its time (you chose, 10-13)
            new = [] if behind else self._alerts(market, s, minutes, event, snap)
        except Exception:  # noqa: BLE001
            log.exception("live %s: alerts", market)
            new = []
        if new:   # the rows say what alerted
            latest = self._latest(paths, s.day, market)
            event = {**event, "rows": [{**r, "alert": latest.get(r["listing_id"])} for r in event["rows"]]}
        self._state[market], self._said[market] = event, (s.day, s.state)
        self.publish(event)
        self._say(market, src, new)
        return event

    def _say(self, market: str, src, new: list[dict]) -> None:
        """New alerts to the page, and those marked for Feishu to the outbox (a delayed source's: shown, not sent —
        §7.1, 1.34; a source that keeps its numbers out of notifications: words only, §4.10)."""
        if not new:
            return
        self.publish({"type": "alert", "market": market, "alerts": new})
        paths = self.paths_of(market)
        settings = Workspace(paths).settings()
        if settings["notify.live"] and getattr(src, "delay", 0) == 0:
            box, queued = Outbox(paths), notices(new, quiet(settings, {market: src.name}))
            for n in queued:
                box.enqueue(n["kind"], n["idem"], n["payload"], n["market"], n["day"])
            if queued:
                self.send()

    def _feed(self, market: str, s: Session, src) -> None:
        """行情中断 (design 2026-10-10 §3): while the market trades, its data 3 minutes or more behind the clock (beyond
        the source's own delay) — failing, or bringing nothing new — is said once, so silence is not taken for calm; its
        return too."""
        if s.state != "trading":
            return
        if (market, s.day) not in self._upto:   # after a restart: what the day's kept minutes cover
            path = self._file(market, s.day)
            self._upto[(market, s.day)] = self._covered(market, _read(path), s) if path.exists() else 0
        lag = s.elapsed - self._upto[(market, s.day)] - getattr(src, "delay", 0)
        stopped = self._down.get(market) == s.day
        if (not stopped and lag < 3) or (stopped and lag > 1):   # back once within a minute of the clock: no flapping
            return
        paths = self.paths_of(market)
        if not Workspace(paths).settings()["live.feed"]:
            return
        text = f"盘中行情中断 {lag} 分钟，盘中提醒暂停（{self._error.get(market) or '没有新数据'}）" if not stopped else "盘中行情恢复，盘中提醒继续"
        if stopped:
            del self._down[market]
        else:
            self._down[market] = s.day
        at = s.now.replace(tzinfo=None)
        a = Alerts(paths).add(day=s.day, market=market, listing_id=INDEXES[market][0], kind="feed", key=f"{'up' if stopped else 'down'}:{at:%H:%M}",
                              at=at, price=None, title=f"{MARKET_NAME[market]} · 盘中行情", text=text,
                              detail={"notify": True, "what": "盘中行情恢复，盘中提醒继续" if stopped else "盘中行情中断，盘中提醒暂停"})
        self._say(market, src, [a] if a else [])

    def lines(self, market: str, now: datetime | None = None) -> list[dict]:
        """The lines each followed stock is watched against today (design 2026-10-10 §2), as _lines says them — before
        the open from the last close."""
        s = self.session(market, now or datetime.now().astimezone())
        rows, stocks = self._rows_now(market, s)
        return self._lines(market, s, rows, stocks, Workspace(self.paths_of(market)).settings())

    def _backfill(self, market: str, s: Session, src) -> None:
        """The multi-day 分时: a minute source that keeps history (`history_days`: yfinance keeps about a week of
        1-minute bars) fills the previous trading days a followed stock has no minutes for — once per stock and day, one
        day a call (a first launch spreads its downloads over as many minutes)."""
        n = getattr(src, "history_days", 0)
        last = self._filled.get(market)
        if not n or (last is not None and (s.now - last).total_seconds() < 60):
            return
        self._filled[market] = s.now
        store = Store(self.paths_of(market), market)
        days = (store.read("calendar").filter(pl.col("is_open") & (pl.col("date") < s.day))["date"].sort().to_list()[-n:]
                if store.latest() else [])
        ids = set(self._ranked(market, s)[:src.max_listings])
        for d in reversed(days):   # the nearest days first: a 5-day view is whole long before a 20-day one
            path = self._file(market, d)
            have = set(_read(path)["listing_id"].unique().to_list()) if path.exists() else set()
            need = sorted(lid for lid in ids - have if (market, d, lid) not in self._tried)
            if not need:
                continue
            self._tried |= {(market, d, lid) for lid in need}
            got = src.minutes(need, d, ()).filter(in_session(load_market(market).sessions, pl.col("at")))
            if got.height:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write((_merge(_read(path), got) if path.exists() else got).unique(["listing_id", "at"], keep="last")
                       .sort("listing_id", "at"), path)
            return

    def prefetch_tick(self, now: datetime) -> None:
        """The minute charts' history fetched ahead (chosen 10-13): outside the session —
        after the close and its late window, before the open's last 45 minutes, a day without one — the whole market's
        1-minute bars of the last KEEP_DAYS trading days before today, newest first: PREFETCH_DAYS days of PREFETCH_BATCH
        stocks a call, one every PREFETCH_EVERY seconds (in a thread of its own); never while the market trades (盯盘's
        budget), nor with 设置 off (minutes.prefetch). Each window's days are written once its last batch is in — the
        file in listing order, small row groups (a chart reads one stock's). Today's file stays 盯盘's: fetched tomorrow."""
        for market in self.markets:
            try:
                self._prefetch(market, now)
            except Exception:  # noqa: BLE001 - the history is fetched on the chart's demand meanwhile
                log.exception("live %s: minutes ahead", market)

    def _prefetch(self, market: str, now: datetime) -> None:
        paths = self.paths_of(market)
        src = self.source_for(paths, market)
        if src is None or not hasattr(src, "minutes_between") or not Workspace(paths).settings()["minutes.prefetch"]:
            return
        snap = Store(paths, market).latest()
        if snap is None or (not self.allow_demo and snap.note == DEMO):   # demo data never reaches a real source
            return
        s = self.session(market, now)
        sessions = load_market(market).sessions
        opening = datetime.combine(s.day, sessions[0][0], tzinfo=now.tzinfo) - timedelta(minutes=45)
        closing = datetime.combine(s.day, sessions[-1][1], tzinfo=now.tzinfo) + timedelta(minutes=getattr(src, "delay", 0) + 10)
        if s.state in ("trading", "lunch") or (s.state == "pre" and now >= opening) or (s.state == "after" and now < closing):
            return
        if (last := self._ahead.get(market)) is not None and (now - last).total_seconds() < PREFETCH_EVERY:
            return
        days = [d for d in _days_of(paths, market) if d < s.day and d <= snap.as_of][-KEEP_DAYS:]
        for k in range(len(days), 0, -PREFETCH_DAYS):   # newest window first
            window = days[max(0, k - PREFETCH_DAYS):k]
            todo = self._ahead_todo(market, window)
            if todo:
                break
        else:
            return
        self._ahead[market] = now
        batch = todo[:PREFETCH_BATCH]
        try:
            got = src.minutes_between(batch, window[0], window[-1]).filter(in_session(sessions, pl.col("at")))
        except Exception as e:  # noqa: BLE001 - a source refusing (its limit, an outage): again in 10 minutes, said once
            self._ahead[market] = now + timedelta(minutes=10)
            log.warning("live %s: minutes ahead of %s…%s: %s (again in 10 minutes)", market, window[0], window[-1], e)
            return
        self._ahead_asked |= {(market, window[0], x) for x in batch}
        key = (market, window[0], window[-1])
        self._ahead_got.setdefault(key, []).append(got)
        if len(todo) > PREFETCH_BATCH:
            return
        parts = [x for x in self._ahead_got.pop(key) if x.height]
        new = pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=MINUTES)
        for d in window:
            day = new.filter(pl.col("at").dt.date() == d)
            if day.is_empty():
                continue
            path = self._file(market, d)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write((_merge(_read(path), day) if path.exists() else day).unique(["listing_id", "at"], keep="last")
                   .sort("listing_id", "at"), path, row_group=20_000)

    def _ahead_todo(self, market: str, window: list[date]) -> list[str]:
        """The listings a window still needs fetched ahead: the stocks traded on one of its days (their daily bars) and
        the market's indexes, without that day in its file, not asked this run."""
        paths = self.paths_of(market)
        store = Store(paths, market)
        traded = (store.scan("bars_1d").filter(pl.col("date").is_between(window[0], window[-1]) & (pl.col("volume") > 0))
                  .select("listing_id", "date").collect())
        traded = pl.concat([traded, pl.DataFrame({"listing_id": [x for x in INDEXES[market] for _ in window],
                                                  "date": [d for _ in INDEXES[market] for d in window]})], how="vertical_relaxed")
        have = []
        for d in window:
            path = self._file(market, d)
            if path.exists():
                have.append(pl.scan_parquet(path).select("listing_id").unique().collect().with_columns(pl.lit(d).alias("date")))
        if have:
            traded = traded.join(pl.concat(have), on=["listing_id", "date"], how="anti")
        return sorted(x for x in traded["listing_id"].unique().to_list() if (market, window[0], x) not in self._ahead_asked)

    def tick(self, now: datetime) -> None:
        """One turn of every loop: 盯盘's (watch_tick), its fast path's (fast_tick) and the 市场 board's record (tape_tick)."""
        self.watch_tick(now)
        self.fast_tick(now)
        self.tape_tick(now)

    def fast_tick(self, now: datetime) -> None:
        """快线 (design 2026-10-10 §5) — in a loop of its own (start), so a poll, seconds on a long watch list (the rules on
        today's bar), never holds up the quotes every few seconds."""
        for market in self.markets:
            try:
                s = self.session(market, now)
                src = self.source_for(self.paths_of(market), market)
                snap = Store(self.paths_of(market), market).latest()
                if snap is not None and (self.allow_demo or snap.note != DEMO) and \
                        allowed(Workspace(self.paths_of(market)).settings(), src.name, "store"):
                    self._fast(market, s, src)
            except Exception:  # noqa: BLE001 - one market's trouble never stops the other
                log.exception("live %s: fast path", market)

    def tape_tick(self, now: datetime) -> None:
        """The 市场 board's record (design 2026-10-12 §8.2) — in a loop of its own (start), so 盯盘's poll, a minute or more
        on a long watch list, never holds up the minute's row."""
        for market in self.markets:
            try:
                s = self.session(market, now)
                snap = Store(self.paths_of(market), market).latest()
                if snap is not None and (self.allow_demo or snap.note != DEMO):
                    self.tape.tick(market, s, self.source_for(self.paths_of(market), market), s.now)
            except Exception:  # noqa: BLE001 - the board's trouble never stops the other market's
                log.exception("live %s: market tape", market)

    def watch_tick(self, now: datetime) -> None:
        """Poll each trading market when its source's interval has passed — and for the source's delay after the
        close, so a late source gives its last minutes. When a market's session moves on (lunch, the close, a new
        day) the page hears it. Then, every market's prices of the minute done, the days before for the multi-day 分时
        (the fast path between polls is fast_tick's)."""
        fill = []
        for market in self.markets:
            try:
                s = self.session(market, now)
                src = self.source_for(self.paths_of(market), market)
                if not allowed(Workspace(self.paths_of(market)).settings(), src.name, "store"):   # §4.10: never fetched
                    self._error[market] = f"盘中行情源 {SOURCES.get(src.name, src.name)} 在 设置 → 数据使用 里关了“本地存储”，不取"
                    if self._said.get(market) != ("off", s.day):
                        self._said[market] = ("off", s.day)
                        self.publish(self.state(market, now))
                    continue
                snap = Store(self.paths_of(market), market).latest()
                real = snap is not None and (self.allow_demo or snap.note != DEMO)
                if real:
                    fill.append((market, s, src))
                if self._due(market, s, src) and real:
                    last = self._polled.get(market)
                    if last is None or (s.now - last).total_seconds() >= src.poll_seconds:
                        self._polled[market] = s.now
                        try:
                            self.poll(market, s)
                        except Exception as e:  # noqa: BLE001 - said on the page: 行情中断 and why
                            log.exception("live %s: poll", market)
                            self._error[market] = str(e) or type(e).__name__
                            self.publish(self.state(market, now))
                    self._feed(market, s, src)
                else:
                    self._error.pop(market, None)   # no prices due now: nothing is 中断 (lunch, the evening, a holiday)
                    if s.state != "lunch":   # the box scans kept for the polls (~0.2 MB a stock) wait for the afternoon
                        self._scans.pop(market, None)
                    if self._said.get(market) != (s.day, s.state):
                        self._said[market] = (s.day, s.state)
                        self.publish(self.state(market, now))
                if real:
                    try:
                        self._viewed_minutes(market, s, src)
                    except Exception:  # noqa: BLE001 - a look's minutes are a convenience: the poll brings them too
                        log.exception("live %s: a viewed stock's minutes", market)
            except Exception:  # noqa: BLE001 - one market's trouble never stops the other
                log.exception("live %s", market)
        for market, s, src in fill:
            try:
                self._backfill(market, s, src)
            except Exception:  # noqa: BLE001 - the days before are a convenience: today's polls go on
                log.exception("live %s: backfill", market)

    def temp_day(self, listing_id: str, now: datetime | None = None) -> dict | None:
        """Today's bar still forming (spec §8.5, the K-line's last bar) until the day's close is published: open, high,
        low, close, and the volume and turnover so far as they are (a process value, never scaled up to a whole day).
        From the day's snapshot when it has the stock — the market board's whole-market quote (any A share that has
        traded today, design 2026-10-12 §9) or a snapshot source's — else from the day's kept minutes. None outside a
        trading day, before the stock's first trade (the auction's match) or for a stock with neither."""
        market = parse_id(listing_id)[0]
        s = self.session(market, now or datetime.now().astimezone())
        snap = Store(self.paths_of(market), market).latest()
        if s.state == "closed" or (snap and snap.as_of >= s.day):
            return None
        day = self._snapshot(market, s.day)
        r = day.filter(pl.col("listing_id") == listing_id) if day is not None else None
        if r is not None and not r.is_empty():   # the day as the exchange reports it so far, not the prices polled
            bar = {"date": s.day, **{c: r[c][0] if r[c][0] is not None else r["last"][0] for c in ("open", "high", "low")},
                   "close": r["last"][0], "volume": r["volume"][0], "amount": r["amount"][0], "pre_close": r["pre_close"][0]}
        else:
            path = self._file(market, s.day)
            m = _read(path).filter(pl.col("listing_id") == listing_id).sort("at") if path.exists() else None
            if m is None or m.is_empty():
                return None
            bar = {"date": s.day, "open": m["open"][0], "high": m["high"].max(), "low": m["low"].min(), "close": m["close"][-1],
                   "volume": m["volume"].sum(), "amount": m.select(turnover(m).sum()).item(), "pre_close": None}
        # with today's factors: on an ex-date the day before's would gap the K-line and its structures (review 10-13)
        row = self._factored(market, s.day, pl.DataFrame([{"listing_id": listing_id, **bar}], schema_overrides={"pre_close": pl.Float64}))
        return {k: v for k, v in row.row(0, named=True).items() if k != "listing_id"}

    def _snapshot(self, market: str, day: date) -> pl.DataFrame | None:
        path = self.paths_of(market).market_dir(market) / "intraday" / f"snapshot={day}.parquet"
        return _read(path) if path.exists() else None

    def _due(self, market: str, s: Session, src) -> bool:
        """Prices are due: the market trades, or a session just ended within its source's delay (+5 minutes) — a late
        source still gives its last minutes (the morning's, the day's)."""
        ended = [b for _, b in load_market(market).sessions if s.now.time() >= b]
        late = s.state in ("lunch", "after") and bool(ended) and \
            s.now <= datetime.combine(s.day, max(ended), tzinfo=s.now.tzinfo) + timedelta(minutes=src.delay + 5)
        return s.state == "trading" or late

    def state(self, market: str, now: datetime | None = None) -> dict:
        """What the page shows: the day's last poll until the day's close is published (after a restart, from the
        day's minutes kept on disk), otherwise the latest close."""
        s = self.session(market, now or datetime.now().astimezone())
        kept = self._state.get(market)
        snap = Store(self.paths_of(market), market).latest()
        if market not in self._error and not (snap and snap.as_of >= s.day):   # a failed source: the close, not stale prices
            if kept and kept["session"]["day"] == s.day.isoformat():
                return {**kept, "session": {**kept["session"], "state": s.state, "label": s.label}, "quote_at": self._quote_at(market)}
            if s.state != "closed" and self._file(market, s.day).exists():
                return self._event(market, s, _read(self._file(market, s.day)), self.source_for(self.paths_of(market), market))
        return self._event(market, s, None, None)

    def start(self, every: float = FAST, clock: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> threading.Thread:
        """盯盘's loop, its fast path and the 市场 board's record, each in its own thread (neither the quotes every few
        seconds nor the record's minute waits on a poll)."""
        def loop(turn):
            while not self._stop.is_set():
                turn(clock())
                self._stop.wait(every)
        threading.Thread(target=loop, args=(self.tape_tick,), name="market-tape", daemon=True).start()
        threading.Thread(target=loop, args=(self.fast_tick,), name="live-fast", daemon=True).start()
        threading.Thread(target=loop, args=(self.prefetch_tick,), name="minutes-ahead", daemon=True).start()
        t = threading.Thread(target=loop, args=(self.watch_tick,), name="live", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
