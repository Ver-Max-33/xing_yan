"""危险提醒 (design 2026-10-15-danger-warnings; numbers: ledger of the same day, independently recomputed): five
situations after which, over ten years of A shares, a stock did worse than the same day's same-size stocks most of the
time — 解禁前, 收在涨停, 强势股高位放量, 弱势股放量急拉, 散户接盘. 沪深 only (北交所 and 日股 were not tested). Each is
judged from a day's close with what was known by it. The four that come from prices and money flow are computed for the
whole history once per snapshot and kept with its derived results; the unlock windows are read when asked."""
import threading
from datetime import date, timedelta

import polars as pl

from wb.data.quality import FLOW_BROKEN, flow_buy
from wb.data.store import Snapshot, Store
from wb.datasets import empty
from wb.engine.l1.frame import build_frame
from wb.files import move_into_place, tmp_for

VERSION = 4                       # a definition, threshold, window or kept column changed: the kept history is computed again
HISTORY_DAYS = 730                # the kept history: the last two years (all ten peaked at 6–8 GB, too much beside a scan)
BUFFER_DAYS = 260                 # calendar days read before it: 120 bars for r120, the 60-bar turnover mean, a run's 20 bars
SH_SZ = ("XSHG", "XSHE")
TOP, WEAK, RETAIL_TOP = 0.9, 0.4, 0.95   # ranks within the day: 名次 ÷ 有数的只数
RUN_GAP = 20                      # a run's first day: not flagged in the 20 bars before
SEASONED = 60                     # 收在涨停 counts from the 60th open day after listing (a new listing's streak is another thing)
HALF_FEN = 0.005
KEEP = 20                         # open days a warning stays shown
UNLOCK_MIN, WINDOW, LAST = 5.0, 20, 5     # % of the total shares; open days before the unlock day; its last week
IPO_DAYS, IPO_LATE = 365, 7       # a one-year IPO unlock: the first open day from listing + 365 days; said until 7 days past
IPO_AGE, OLD_AGE = 400, 1095      # days from listing: an unlock this soon is the IPO's; one past this, an old stock's
FLOW_COLS = ["buy_sm", "sell_sm", "buy_md", "sell_md", "buy_lg", "sell_lg", "buy_elg", "sell_elg"]
NOT_TESTED = "没有危险提醒：检验只用了沪深股票"
NUMBERS = ("r20", "r120", "ma20", "to5rel", "mfs20")   # the day's own values, which a warning's reason quotes
EVENT_SCHEMA = {"listing_id": pl.Utf8, "date": pl.Date, "kind": pl.Utf8, "boards": pl.Int32, **{c: pl.Float64 for c in NUMBERS}}
_COMPUTING = threading.Lock()     # one history computed at a time (2.7 GB each): a page and the daily job asking together wait


def open_days(store: Store, snap: Snapshot) -> pl.DataFrame:
    """The stored calendar's open days, numbered (td)."""
    return (store.read("calendar", snap).filter(pl.col("is_open")).select("date").unique().sort("date")
            .with_row_index("td").with_columns(pl.col("td").cast(pl.Int64)))


def listed(store: Store, snap: Snapshot) -> pl.DataFrame:
    """沪深 stocks: listing day and its open-day number (td0; a listing before the stored calendar counts as long ago; an
    unknown listing day leaves td0 unknown — the stock is covered, only what needs its age waits)."""
    schema = {"listing_id": pl.Utf8, "list_date": pl.Date, "td0": pl.Int64}
    ins = store.read("instruments", snap)
    if "exchange" not in ins.columns or ins.is_empty():
        return pl.DataFrame(schema=schema)
    days = open_days(store, snap)
    first = days["date"].min() if days.height else None
    s = ins.filter(pl.col("exchange").is_in(SH_SZ)).select("listing_id", "list_date")
    known = (s.filter(pl.col("list_date").is_not_null()).sort("list_date")
             .join_asof(days.rename({"date": "list_date", "td": "td0"}), on="list_date", strategy="forward")
             .with_columns(pl.when(pl.col("list_date") < first).then(-10_000).otherwise(pl.col("td0")).cast(pl.Int64).alias("td0")))
    return pl.concat([known.select(list(schema)), s.filter(pl.col("list_date").is_null()).with_columns(pl.lit(None, pl.Int64).alias("td0"))
                      .select(list(schema))])


def features(bars: pl.DataFrame, flow: pl.DataFrame, stocks: pl.DataFrame, days: pl.DataFrame) -> pl.DataFrame:
    """Per 沪深 bar (build_frame's: listing_id, date, close 后复权, raw_close, up_limit, amount, turnover_rate, circ_mv):
    r20 r120, ma20 (close ÷ its 20-bar mean − 1), to5rel (5-bar ÷ 60-bar mean turnover), mfs20 (20 bars of small-order
    net ÷ float value, bp: null unless every one of the 20 is a sound row), lu (closed at the limit up, half a fen of
    slack), boards (limit-up closes in a row), age (open days since listing)."""
    C, T = pl.col("close"), pl.col("turnover_rate")
    g = lambda e: e.over("listing_id")   # noqa: E731
    sound = flow_buy().is_not_null() & ((flow_buy() / pl.col("amount") - 1).abs() <= FLOW_BROKEN)
    f = (bars.join(stocks.select("listing_id", "td0"), on="listing_id")
         .join(flow.select("listing_id", "date", *FLOW_COLS), on=["listing_id", "date"], how="left")
         .join(days, on="date", how="left").sort("listing_id", "date"))
    f = f.with_columns(
        g(C / C.shift(20) - 1).alias("r20"), g(C / C.shift(120) - 1).alias("r120"), g(C / C.rolling_mean(20) - 1).alias("ma20"),
        g(T.rolling_mean(5) / T.rolling_mean(60)).alias("to5rel"),
        pl.when(sound).then((pl.col("buy_sm") - pl.col("sell_sm")) / pl.col("circ_mv") * 1e4).alias("mfs"),
        ((pl.col("raw_close") >= pl.col("up_limit") - HALF_FEN) & pl.col("up_limit").is_not_null()).fill_null(False).alias("lu"),
        (pl.col("td") - pl.col("td0")).alias("age"))
    f = f.with_columns(g(pl.col("mfs").rolling_sum(20)).alias("mfs20"),
                       (~pl.col("lu")).cast(pl.Int32).cum_sum().over("listing_id").alias("_run"))
    return (f.with_columns(pl.col("lu").cast(pl.Int32).cum_sum().over("listing_id", "_run").alias("boards"))
            .drop("_run", "mfs", *FLOW_COLS))


def _first(f: pl.DataFrame, flag: str) -> pl.DataFrame:
    """<flag>1: flagged today and in none of the RUN_GAP bars before (the first day of a run). An unknown bar after the
    stock's first known one (散户接盘 across a gap in its money flow) may have held it: a run right after one is not new —
    else a data gap starts a warning. The unknown bars before its first known one are no history yet, not a gap."""
    started = pl.col(flag).is_not_null().cast(pl.Int8).cum_max().over("listing_id") == 1
    seen = pl.col(flag).fill_null(False) | (pl.col(flag).is_null() & started)
    f = f.with_columns(seen.cast(pl.Int32).cum_sum().over("listing_id").alias("_cs"))
    before = (pl.col("_cs").shift(1).over("listing_id").fill_null(0) - pl.col("_cs").shift(RUN_GAP + 1).over("listing_id").fill_null(0))
    return f.with_columns((pl.col(flag).fill_null(False) & (before == 0)).alias(flag + "1")).drop("_cs")


def flags(f: pl.DataFrame) -> pl.DataFrame:
    """The day's ranks and the three warnings made of them — strong, weak, retail (null where the money flow is unknown) —
    each with its run's first day. 散户接盘's flow is what the 20-day move does not explain: the residual of the day's
    least-squares line of the flow's rank on the 20-day move's rank."""
    rk = lambda c: (pl.col(c).rank() / pl.col(c).count()).over("date")   # noqa: E731
    f = f.with_columns(rk("r120").alias("k120"), rk("ma20").alias("kma"), rk("to5rel").alias("kv"), rk("r20").alias("k20"),
                       rk("mfs20").alias("km"))
    both = pl.col("km").is_not_null() & pl.col("k20").is_not_null()
    f = f.with_columns(pl.when(both).then(pl.col("k20")).alias("_x"), pl.when(both).then(pl.col("km")).alias("_y"))
    mx, my = pl.col("_x").mean().over("date"), pl.col("_y").mean().over("date")
    f = f.with_columns(((pl.col("_x") * pl.col("_y")).mean().over("date") - mx * my).alias("_cxy"),
                       ((pl.col("_x") ** 2).mean().over("date") - mx ** 2).alias("_vx"), mx.alias("_mx"), my.alias("_my"))
    f = f.with_columns(pl.when(pl.col("_vx") > 0).then(pl.col("_y") - pl.col("_my") - pl.col("_cxy") / pl.col("_vx")
                                                      * (pl.col("_x") - pl.col("_mx"))).alias("_res"))
    f = f.with_columns(
        ((pl.col("k120") >= TOP) & (pl.col("kma") >= TOP) & (pl.col("kv") >= TOP)).fill_null(False).alias("strong"),
        ((pl.col("k120") <= WEAK) & (pl.col("k20") >= TOP) & (pl.col("kv") >= TOP)).fill_null(False).alias("weak"),
        (rk("_res") >= RETAIL_TOP).alias("retail"))
    for flag in ("strong", "weak", "retail"):
        f = _first(f, flag)
    return f.drop("_x", "_y", "_cxy", "_vx", "_mx", "_my", "_res")


def events(f: pl.DataFrame) -> pl.DataFrame:
    """One row an event: every limit-up close of a stock listed SEASONED open days, with its boards; the first day of each
    run of the three ranked warnings — each with the day's NUMBERS."""
    nums = [pl.col(c).cast(pl.Float64) for c in NUMBERS]
    parts = [f.filter(pl.col("lu") & (pl.col("age") >= SEASONED)).select("listing_id", "date", pl.lit("limit_up").alias("kind"),
                                                                         pl.col("boards").cast(pl.Int32), *nums)]
    for kind, flag in (("strong_burst", "strong"), ("weak_spike", "weak"), ("retail", "retail")):
        parts.append(f.filter(pl.col(flag + "1")).select("listing_id", "date", pl.lit(kind).alias("kind"),
                                                         pl.lit(None, pl.Int32).alias("boards"), *nums))
    return pl.concat(parts).sort("listing_id", "date", "kind")


def history_from(snap: Snapshot) -> date:
    """The first day the kept history covers."""
    return snap.as_of - timedelta(days=HISTORY_DAYS)


def _bars(store: Store, snap: Snapshot, stocks: pl.DataFrame, start: date, end: date | None = None,
          listings: list[str] | None = None) -> pl.DataFrame:
    """沪深 bars from `start`: 后复权 close, raw close and limit, amount (build_frame's, a bad row's values unknown) and the
    day's turnover and float value. The valuation is read for these days only: build_frame's own join reads all ten years
    of it, which took the history to 5 GB at its peak (the quality pass touches no valuation column, so nothing differs)."""
    bars = build_frame(store, store.market, store.paths.profile, {"raw_close", "up_limit", "amount"}, listings=listings, end=end,
                       snap=snap, start=start)
    val = store.scan("valuation", snap).filter(pl.col("date") >= start)
    if end is not None:
        val = val.filter(pl.col("date") <= end)
    if listings is not None:
        val = val.filter(pl.col("listing_id").is_in(listings))
    val = (val.select("listing_id", "date", "turnover_rate", "circ_mv").collect() if snap.manifest.get("valuation")
           else pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, "turnover_rate": pl.Float64, "circ_mv": pl.Float64}))
    return (bars.join(stocks.select("listing_id"), on="listing_id", how="semi").join(val, on=["listing_id", "date"], how="left")
            .select("listing_id", "date", "close", "raw_close", "up_limit", "amount", "turnover_rate", "circ_mv"))


def _flow(store: Store, snap: Snapshot, start: date, end: date | None = None, listings: list[str] | None = None) -> pl.DataFrame:
    if not snap.manifest.get("moneyflow"):
        return empty("moneyflow").select("listing_id", "date", *FLOW_COLS)
    q = store.scan("moneyflow", snap).filter(pl.col("date") >= start)
    if end is not None:
        q = q.filter(pl.col("date") <= end)
    if listings is not None:
        q = q.filter(pl.col("listing_id").is_in(listings))
    return q.select("listing_id", "date", *FLOW_COLS).collect()


def _inputs(store: Store, snap: Snapshot) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """沪深 bars, money flow, the stocks, the open days — from BUFFER_DAYS before the kept history (every window here is
    bounded, so the cut changes no value in it)."""
    stocks, days = listed(store, snap), open_days(store, snap)
    start = history_from(snap) - timedelta(days=BUFFER_DAYS)
    return _bars(store, snap, stocks, start), _flow(store, snap, start), stocks, days


def history(store: Store, snap: Snapshot | None = None) -> pl.DataFrame:
    """Every event of the four price / money-flow warnings over the snapshot's last HISTORY_DAYS, computed once and kept
    with its derived results (`derived/<snapshot>/danger-v<VERSION>.parquet`)."""
    snap = snap or store.latest()
    if snap is None or store.market != "CN":
        return pl.DataFrame(schema=EVENT_SCHEMA)
    path = store.derived / snap.id / f"danger-v{VERSION}.parquet"
    if path.exists():
        return pl.read_parquet(path)
    with _COMPUTING:
        if path.exists():   # computed while this one waited
            return pl.read_parquet(path)
        ev = events(flags(features(*_inputs(store, snap)))).filter(pl.col("date") >= history_from(snap))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tmp_for(path)
        ev.write_parquet(tmp)
        try:
            move_into_place(tmp, path)
        except OSError:   # Windows: the other writer's file is open; it holds the same rows
            tmp.unlink(missing_ok=True)
    return ev


WINDOW_SCHEMA = {"listing_id": pl.Utf8, "unlock_day": pl.Date, "left": pl.Int64, "ratio": pl.Float64, "types": pl.Utf8,
                 "kind": pl.Utf8, "predicted": pl.Boolean, "ann_date": pl.Date}


def _td(days: pl.DataFrame, day: date) -> int | None:
    """The open-day number of `day` (of the last open day on or before it)."""
    k = days.filter(pl.col("date") <= day)
    return int(k["td"][-1]) if k.height else None


def unlock_windows(unlocks: pl.DataFrame, stocks: pl.DataFrame, days: pl.DataFrame, day: date) -> pl.DataFrame:
    """Unlocks of ≥ UNLOCK_MIN% of the total shares due within WINDOW open days of `day` (the unlock day included), as known
    by `day`: an announced one from its ann_date; a one-year IPO unlock not announced yet from the listing day (the first
    open day on or after list_date + IPO_DAYS), said until IPO_LATE days past it. `left` is negative only for such a
    predicted one already past."""
    td_day = _td(days, day)
    if td_day is None or stocks.is_empty():
        return pl.DataFrame(schema=WINDOW_SCHEMA)
    known = unlocks.filter(pl.col("ann_date").is_not_null() & (pl.col("ann_date") <= day)).join(stocks.select("listing_id", "list_date"), on="listing_id")
    open_ = days.rename({"date": "unlock_day"})
    ann = (known.group_by("listing_id", "float_date", "list_date").agg(pl.col("float_ratio").sum().alias("ratio"), pl.col("ann_date").min(),
                                                                       pl.col("share_type").unique().sort().str.join("、").alias("types"))
           .sort("float_date").join_asof(open_, left_on="float_date", right_on="unlock_day", strategy="forward")
           .filter(pl.col("unlock_day").is_not_null() & ((pl.col("unlock_day") - pl.col("float_date")).dt.total_days() <= 7))
           .with_columns((pl.col("td") - td_day).alias("left"), (pl.col("unlock_day") - pl.col("list_date")).dt.total_days().alias("_age"))
           .filter((pl.col("ratio") >= UNLOCK_MIN) & pl.col("left").is_between(0, WINDOW)))
    kind = (pl.when(pl.col("_age") <= IPO_AGE).then(pl.lit("ipo")).when(pl.col("types").str.contains("定增")).then(pl.lit("placement"))
            .when(pl.col("_age") > OLD_AGE).then(pl.lit("old")).otherwise(pl.lit("other")))
    ann = ann.with_columns(kind.alias("kind"), pl.lit(False).alias("predicted")).select(list(WINDOW_SCHEMA))
    told = known.filter((pl.col("float_date") - pl.col("list_date")).dt.total_days().is_between(330, IPO_AGE)).select("listing_id").unique()
    pred = (stocks.filter(pl.col("list_date").is_not_null()).with_columns((pl.col("list_date") + pl.duration(days=IPO_DAYS)).alias("_due")).sort("_due")
            .join_asof(open_, left_on="_due", right_on="unlock_day", strategy="forward")
            .filter(pl.col("unlock_day").is_not_null())
            .with_columns((pl.col("td") - td_day).alias("left"))
            .filter((pl.col("left") <= WINDOW) & (pl.col("unlock_day") + pl.duration(days=IPO_LATE) >= day))
            .join(told, on="listing_id", how="anti")
            .select("listing_id", "unlock_day", "left", pl.lit(None, pl.Float64).alias("ratio"), pl.lit("").alias("types"),
                    pl.lit("ipo").alias("kind"), pl.lit(True).alias("predicted"), pl.lit(None, pl.Date).alias("ann_date")))
    return pl.concat([ann, pred.select(list(WINDOW_SCHEMA))]).sort("listing_id", "unlock_day")


def active(store: Store, ids: list[str], day: date | None = None, snap: Snapshot | None = None) -> dict:
    """What `ids` show on `day` (the snapshot's day by default): their warnings in danger_words' words (the events of the
    last KEEP open days, a limit-up its latest; the unlock windows), the warnings that cannot be judged for want of data
    (散户接盘 without 20 sound days of money flow) and the ids not covered (北交所, 日股)."""
    from wb import danger_words as W
    snap = snap or store.latest()
    out = {"day": None, "warnings": {}, "unknown": {}, "uncovered": {}, "unlocks_from": None, "history_from": None}
    if snap is None:
        return out
    day = day or snap.as_of
    out["day"], out["history_from"] = day.isoformat(), history_from(snap).isoformat()
    stocks = listed(store, snap)
    sh_sz = set(stocks["listing_id"].to_list())
    out["uncovered"] = {lid: NOT_TESTED for lid in ids if lid not in sh_sz}
    ids = [lid for lid in ids if lid in sh_sz]
    days = open_days(store, snap)
    td_day = _td(days, day)
    un = store.read("unlocks", snap) if snap.manifest.get("unlocks") else empty("unlocks")
    out["unlocks_from"] = un["float_date"].min().isoformat() if un.height else None
    if not ids or td_day is None:
        return out
    ev = (history(store, snap).filter(pl.col("listing_id").is_in(ids) & (pl.col("date") <= day)).join(days, on="date")
          .with_columns((td_day - pl.col("td")).alias("days")).filter(pl.col("days") < KEEP)
          .sort("date").group_by("listing_id", "kind").agg(pl.all().last()))
    found: dict[str, list[dict]] = {}
    for r in ev.iter_rows(named=True):
        found.setdefault(r["listing_id"], []).append(W.event(r["kind"], r["date"], r["boards"], int(r["days"]), {c: r[c] for c in NUMBERS}))
    prev = days.filter(pl.col("td") == td_day - 1)["date"]
    before = prev[0] if prev.len() else None
    for r in unlock_windows(un.filter(pl.col("listing_id").is_in(ids)), stocks.filter(pl.col("listing_id").is_in(ids)), days, day).iter_rows(named=True):
        new = r["left"] in (WINDOW, LAST) or (not r["predicted"] and before is not None and r["ann_date"] > before)
        found.setdefault(r["listing_id"], []).append(W.unlock(r, new))
    out["warnings"] = {lid: sorted(ws, key=lambda w: W.ORDER.index(w["kind"])) for lid, ws in found.items()}
    if snap.manifest.get("moneyflow"):   # 散户接盘 judged only with 20 sound days: else unknown, not "nothing"
        start = day - timedelta(days=60)
        f = features(_bars(store, snap, stocks, start, day, ids), _flow(store, snap, start, day, ids), stocks, days)
        last = f.sort("date").group_by("listing_id").agg(pl.col("mfs20").last())
        out["unknown"] = {lid: ["retail"] for lid in last.filter(pl.col("mfs20").is_null())["listing_id"].to_list()}
    else:   # no money flow stored at all (before its first fetch, a plan or profile without it): unknown, not nothing
        out["unknown"] = {lid: ["retail"] for lid in ids}
    return out
