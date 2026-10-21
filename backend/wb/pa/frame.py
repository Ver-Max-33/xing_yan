"""价格行为分析's input (design 2026-10-20 §3.3, §4): a stock's latest daily bars as PA_Agent's KlineFrame — 前复权
anchored on day 0 and in cents, so K1 is the price that traded and the AI's prices are real ones; the exchange's own
volume (shares); each bar's 涨跌停 mark from the raw prices; the next trading day's limits for the table's header; and the
workbench's previous plan for PA's continuity check."""
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_analysis_frame

from wb.config import Paths
from wb.data.store import Store
from wb.devdata import DEMO
from wb.engine.l1.frame import build_frame
from wb.listing import parse_id
from wb.markets import cn_limit_pct
from wb.pa.plans import PRICES

BARS = 100    # PA's snapshot: the bars the AI reads; up to INDICATOR_WARMUP_BARS older ones only warm EMA20 / ATR14 up
COLUMNS = {"open", "high", "low", "close", "raw_close", "raw_high", "raw_low", "raw_volume", "up_limit", "down_limit",
           "pre_close"}
CENT = Decimal("0.01")


class NotEnoughBars(ValueError):
    def __init__(self, n: int):
        super().__init__(f"数据不足：要 {BARS} 根日线，这只股票只有 {n} 根")


@dataclass(frozen=True)
class PreviousPlan:
    id: str
    time: str
    decision: dict          # its final plan, in 前复权 prices anchored on its own day 0
    bars: int               # trading days from that analysis's day 0 to this one's
    adj0: float | None = None   # its day 0's 后复权 factor: moves its prices onto another day 0's basis


@dataclass(frozen=True)
class DailyInput:
    listing_id: str
    code: str
    day: date
    profile: str
    demo: bool
    adjusted: bool
    k1_close: float
    frame: KlineFrame
    bars_json: list[dict]
    adj0: float = 1.0       # day 0's 后复权 factor (close ÷ raw close): 前复权 here = 后复权 ÷ it


def limit_mark(raw_close: float, raw_high: float, raw_low: float, up: float | None, down: float | None) -> str:
    """§3.3: where the exchange's own prices closed against the day's limits, compared in cents."""
    c = round(raw_close, 2)
    if up is not None and c >= round(up, 2):
        return "一字涨停" if round(raw_low, 2) >= round(up, 2) else "涨停"
    if down is not None and c <= round(down, 2):
        return "一字跌停" if round(raw_high, 2) <= round(down, 2) else "跌停"
    return ""


LIMITS = (0.05, 0.10, 0.20, 0.30)   # the A-share daily limits there are


def next_limits(code: str, close: float, up: float | None, pre_close: float | None) -> tuple[float, float]:
    """§3.3: the next trading day's limit prices — day 0's close times the stock's own limit that day (its limit price
    over its reference price, so ST and rule changes follow the data; the nearest of the limits there are, as a price
    under 1 yuan rounds 10% to 11%), else its board's — rounded half up to the cent; 北交所's toward the close (the
    limit-up down, the limit-down up), as its own limit prices are."""
    pct = Decimal(str(min(LIMITS, key=lambda x: abs(up / pre_close - 1 - x)) if up and pre_close else cn_limit_pct(code)))
    c = Decimal(str(close))
    bse = cn_limit_pct(code) == 0.30   # only 北交所 has 30%
    return (float((c * (1 + pct)).quantize(CENT, ROUND_FLOOR if bse else ROUND_HALF_UP)),
            float((c * (1 - pct)).quantize(CENT, ROUND_CEILING if bse else ROUND_HALF_UP)))


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def daily_input(paths: Paths, listing_id: str) -> DailyInput:
    """The latest snapshot's bars of one CN stock as PA's frame, with `previous_plan_known` and no plan (`with_previous`
    adds it once the records say which came before this day 0). A bar with a row-level data problem is left out like a
    suspended day: its prices are unknown (spec §4.6), and the AI must not read made-up ones."""
    _, code = parse_id(listing_id)
    store = Store(paths, "CN")
    snap = store.latest()
    if snap is None:
        raise NotEnoughBars(0)
    df = build_frame(store, "CN", paths.profile, COLUMNS, listings=[listing_id], snap=snap).drop_nulls(
        ["open", "high", "low", "close", "raw_close", "raw_high", "raw_low", "raw_volume"])
    if df.height < BARS:
        raise NotEnoughBars(df.height)
    rows = list(df.tail(BARS + INDICATOR_WARMUP_BARS).reverse().iter_rows(named=True))     # newest first, as PA reads
    qfq = rows[0]["raw_close"] / rows[0]["close"]          # 后复权 → 前复权 on day 0
    bars = [KlineBar(seq=i + 1, ts_open=_ms(r["date"]), open=round(r["open"] * qfq, 2), high=round(r["high"] * qfq, 2),
                     low=round(r["low"] * qfq, 2), close=round(r["close"] * qfq, 2), volume=r["raw_volume"], closed=True)
            for i, r in enumerate(rows)]
    frame = build_analysis_frame(bars, BARS, code, "1d")   # never None: every bar is closed and there are enough
    marks = tuple(limit_mark(r["raw_close"], r["raw_high"], r["raw_low"], r["up_limit"], r["down_limit"])
                  for r in rows[:BARS])
    up, down = next_limits(code, rows[0]["raw_close"], rows[0]["up_limit"], rows[0]["pre_close"])
    frame = dataclasses.replace(frame, limit_marks=marks, next_limit_up=up, next_limit_down=down, previous_plan_known=True)
    bars_json = [{"date": r["date"].isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                  "volume": b.volume, "mark": m} for r, b, m in zip(rows, frame.bars, marks)][::-1]
    return DailyInput(listing_id, code, rows[0]["date"], paths.profile, snap.note == DEMO, paths.profile != "test",
                      frame.bars[0].close, frame, bars_json, rows[0]["close"] / rows[0]["raw_close"])


def with_previous(inp: DailyInput, previous: PreviousPlan | None) -> DailyInput:
    """The workbench's previous plan on the frame, for PA's continuity check (its CSV is never read), its prices moved
    from its own day 0's 前复权 basis onto this one's: with an ex-date between, PA would compare it with bars a dividend
    or a bonus issue apart."""
    if previous is None:
        return inp
    d = dict(previous.decision)
    if previous.adj0:
        k = previous.adj0 / inp.adj0
        d |= {f: round(d[f] * k, 2) for f in PRICES if isinstance(d.get(f), (int, float))}
    plan = (("time", previous.time), *d.items())
    return dataclasses.replace(inp, frame=dataclasses.replace(inp.frame, previous_plan=plan, previous_plan_bars=previous.bars))
