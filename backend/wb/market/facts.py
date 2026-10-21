"""市场看板的「当天的底」(design 2026-10-12 §8.1): for a session day, what is known of each listing before the open —
its close before as traded, today's limit prices, its limit-up run to its last bar and whether that bar (yesterday's)
closed at the limit, its industries at that bar (申万一级 and 二级; 日股 33 業種), the adjusted closes its 5- and
20-bar returns need, its average amount over 5 bars — with the whole market's amount of the day before and each
index's lines. From one data snapshot: when the snapshot does not reach the trading day before (the evening update
failed), what leans on yesterday is unknown."""
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Snapshot, Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate
from wb.listing import tushare_id_expr

LOOKBACK = 150   # calendar days of bars read: 20 bars back and a limit-up run's own length (the longest was 25)
FORMULAS = {"streak": "LIMIT_UP_STREAK", "lu": "LIMIT_UP_CLOSE", "prev": "RAW_C", "adj_last": "C", "c4": "REF(C, 4)",
            "c5": "REF(C, 5)", "c19": "REF(C, 19)", "c20": "REF(C, 20)", "amount5": "MA(AMOUNT, 5)"}
INDEXES = {"CN": ("CN:IDX:000001", "CN:IDX:399001", "CN:IDX:399006"), "JP": ("JP:IDX:N225", "JP:IDX:TOPIX")}
STOCKS = {"listing_id": pl.Utf8, "name": pl.Utf8, "last_day": pl.Date, "prev": pl.Float64, "adj_last": pl.Float64,
          "c4": pl.Float64, "c5": pl.Float64, "c19": pl.Float64, "c20": pl.Float64, "streak": pl.Int64, "lu_prev": pl.Boolean,
          "l1": pl.Utf8, "l2": pl.Utf8, "amount5": pl.Float64, "up_limit": pl.Float64, "down_limit": pl.Float64}


@dataclass(frozen=True)
class Facts:
    market: str
    day: date                  # the session day
    snapshot: str | None       # the data snapshot's id
    as_of: date | None         # its last day
    prev_day: date | None      # the trading day before `day` (the stored calendar)
    stale: bool                # as_of < prev_day: yesterday's limit-ups, runs and amount are unknown
    stocks: pl.DataFrame       # STOCKS, one row a listing with a bar before `day`
    limits: str                # "today" (fetched for `day`), "stored" (the day's stk_limit in the data), "none"
    amount_before: float | None   # every listing's amount on prev_day (yuan)
    indexes: dict = field(default_factory=dict)   # id -> prev, sum59, sum199, ma60_prev, ma60_5, box
    top_ids: tuple[str, ...] = ()                 # yesterday's highest board (2 or more), its listings


def build_facts(paths: Paths, market: str, day: date, limits: pl.DataFrame | None = None, snap: Snapshot | None = None) -> Facts:
    """The day's base from the latest snapshot (or `snap`); `limits`: today's limit prices fetched for `day`
    (fetch_limits), else a stored day's own, else none."""
    store = Store(paths, market)
    snap = snap or store.latest()
    if snap is None:
        return Facts(market, day, None, None, None, True, pl.DataFrame(schema=STOCKS), "none", None)
    calendar = sorted(store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list())
    before = [d for d in calendar if d < day]
    prev_day = before[-1] if before else None
    stale = prev_day is None or snap.as_of < prev_day
    comp = Compiler(market, available=available_columns(store, snap))
    for name, formula in FORMULAS.items():
        comp.add(name, formula)
    prog = comp.program()
    frame = build_frame(store, market, paths.profile, prog.columns | {"industry"}, start=day - timedelta(days=LOOKBACK), snap=snap)
    frame = frame.filter(pl.col("date") < day)
    v = evaluate(frame, prog, calendar).join(frame.select("listing_id", "date", "industry"), on=["listing_id", "date"], how="left")
    last = (v.sort("listing_id", "date").group_by("listing_id", maintain_order=True).last()
            .rename({"date": "last_day", "industry": "l1"}))
    if stale:   # what needs the missing days is unknown — the windows would be shifted by them, not just a day late
        last = last.with_columns(pl.lit(None, pl.Int64).alias("streak"), pl.lit(None, pl.Boolean).alias("lu_prev"),
                                 *[pl.lit(None, pl.Float64).alias(c) for c in ("c4", "c5", "c19", "c20", "amount5")])
    else:
        last = last.with_columns(pl.col("streak").cast(pl.Int64), (pl.col("lu") & (pl.col("last_day") == prev_day)).alias("lu_prev"))
    last = last.join(_level_two(store, snap, last), on="listing_id", how="left").join(
        store.read("instruments", snap).select("listing_id", "name"), on="listing_id", how="left")
    lim, kind = _limits(store, snap, market, day, limits)
    stocks = last.join(lim, on="listing_id", how="left").select([pl.col(c).cast(t) for c, t in STOCKS.items()])
    amount = None
    if not stale:
        amount = store.scan("bars_1d", snap).filter(pl.col("date") == prev_day).select(pl.col("amount").sum()).collect().item()
    top: tuple[str, ...] = ()
    if not stale:
        hit = stocks.filter(pl.col("lu_prev").fill_null(False))
        height = hit["streak"].max() if hit.height else None
        if height is not None and height >= 2:
            top = tuple(sorted(hit.filter(pl.col("streak") == height)["listing_id"].to_list()))
    return Facts(market, day, snap.id, snap.as_of, prev_day, stale, stocks, kind, amount, _indexes(store, snap, market, day, stale), top)


def _level_two(store: Store, snap: Snapshot, last: pl.DataFrame) -> pl.DataFrame:
    """listing_id, l2: the 申万二级 of the membership spell each listing's last bar is in (null without the column)."""
    rows = store.scan("industry", snap)
    if "l2_name" not in rows.collect_schema().names():
        return pl.DataFrame(schema={"listing_id": pl.Utf8, "l2": pl.Utf8})
    j = (last.select("listing_id", "last_day").join(rows.select("listing_id", "in_date", "out_date", "l2_name").collect(), on="listing_id")
         .filter((pl.col("in_date") <= pl.col("last_day")) & (pl.col("out_date").is_null() | (pl.col("out_date") >= pl.col("last_day")))))
    return j.sort("in_date").unique("listing_id", keep="last").select("listing_id", pl.col("l2_name").alias("l2"))


def _limits(store: Store, snap: Snapshot, market: str, day: date, limits: pl.DataFrame | None) -> tuple[pl.DataFrame, str]:
    none = pl.DataFrame(schema={"listing_id": pl.Utf8, "up_limit": pl.Float64, "down_limit": pl.Float64})
    if market != "CN":   # the board's limits are 沪深京 limit prices; 日股 has its own (flags), not shown
        return none, "none"
    if limits is not None:
        return limits.select("listing_id", "up_limit", "down_limit"), "today"
    if day <= snap.as_of:
        got = store.scan("bars_1d", snap).filter(pl.col("date") == day).select("listing_id", "up_limit", "down_limit").collect()
        if got["up_limit"].drop_nulls().len():
            return got, "stored"
    return none, "none"


def _indexes(store: Store, snap: Snapshot, market: str, day: date, stale: bool = False) -> dict:
    """Each index's lines read before `day`: its close before (prev), the sums of its last 59 and 199 closes (with the
    price now they make today's 60- and 200-day lines), the 60-day line at prev's close and five bars before `day`,
    and its own box (the latest established, not ended). Stale: the box only (a price; the rest needs the missing days)."""
    from wb.market.view import _indexes as boxes
    ids = INDEXES.get(market, ())
    bars = (store.scan("index_bars_1d", snap).filter(pl.col("index_id").is_in(ids) & (pl.col("date") < day))
            .select("index_id", "date", "close").sort("index_id", "date").collect())
    box = {}
    for i in boxes(store, snap):
        b = i.get("box")
        box[i["index_id"]] = {"top": b["top"], "bottom": b["bottom"]} if b and not b["ended"] else None
    out = {}
    for iid in ids:
        c = bars.filter(pl.col("index_id") == iid)["close"].drop_nulls().to_list()
        if not c:
            continue
        if stale:
            out[iid] = {"prev": None, "sum59": None, "sum199": None, "ma60_prev": None, "ma60_5": None, "box": box.get(iid)}
            continue
        out[iid] = {"prev": c[-1], "sum59": sum(c[-59:]) if len(c) >= 59 else None, "sum199": sum(c[-199:]) if len(c) >= 199 else None,
                    "ma60_prev": sum(c[-60:]) / 60 if len(c) >= 60 else None,
                    "ma60_5": sum(c[-64:-4]) / 60 if len(c) >= 64 else None, "box": box.get(iid)}
    return out


def fetch_limits(day: date, tushare=None) -> pl.DataFrame:
    """listing_id, exchange (SH / SZ / BJ), up_limit, down_limit for `day` from Tushare's stk_limit — published before
    the open (2026-10-12: the 上交所 at 08:43, all three by 08:53)."""
    from wb.data.providers.tushare import Tushare
    t = tushare or Tushare()
    df = t.query("stk_limit", "ts_code,up_limit,down_limit", trade_date=f"{day:%Y%m%d}")
    return (df.select(tushare_id_expr().alias("listing_id"), pl.col("ts_code").str.slice(7).alias("exchange"),
                      pl.col("up_limit").cast(pl.Float64), pl.col("down_limit").cast(pl.Float64))
            .drop_nulls("listing_id"))


def limits_complete(before: pl.DataFrame | None, now: pl.DataFrame) -> bool:
    """All three exchanges are there and two fetches in a row are the same (the list grows while it is published)."""
    if before is None or now.is_empty() or not {"SH", "SZ", "BJ"} <= set(now["exchange"].unique().to_list()):
        return False
    return now.sort("listing_id").equals(before.sort("listing_id"))
