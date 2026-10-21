"""What followed each signal (spec §9.1, §9.4). Bought at the open of the next trading day; the return h trading days
on (the entry day is day 1, sold at that day's close) on adjusted prices, and its excess over the market's benchmark
index from that day's open; the best and worst move (high / low against the entry) over HOLD days. A next-day open at
the limit up (CN: the stk_limit price; JP: UL with the open at the high) or a day the listing did not trade cannot be
bought: `blocked`. A listing that stopped trading before the HOLD exit is `gone` (listed apart, never dropped); a
window past the data is unknown. With the industry on the signal day and whether the benchmark closed above its
200-day line, and the next day's opening gap against the signal day's close (adjusted: an ex-date is no gap) — there
whether or not the open could be bought (a close far above the day's average tends to open lower)."""
import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import build_frame
from wb.engine.l3.rule import RuleError
from wb.markets import load_market

HORIZONS = (1, 3, 5, 10, 20, 60)
HOLD = 20  # the main horizon: the win rate, the best and worst move


def outcomes(paths: Paths, market: str, signals: pl.DataFrame) -> pl.DataFrame:
    """Each signal's moves 1 to 60 days on, its groups and gap (a trade plan's trades: wb.validate.trades)."""
    store = Store(paths, market)
    snap = store.latest()
    sig = (signals.select("listing_id", "signal_date").unique(["listing_id", "signal_date"], keep="first")
           .sort("signal_date", "listing_id").with_row_index("sid"))
    cal = (store.read("calendar", snap).filter(pl.col("is_open") & (pl.col("date") <= snap.as_of))
           .select("date").unique().sort("date").with_row_index("k"))
    s = sig.join(cal.rename({"date": "signal_date", "k": "k0"}), on="signal_date", how="left")
    for off in sorted({1, *HORIZONS}):  # the market's trading day `off` days on: null past the data
        s = (s.with_columns((pl.col("k0") + off).alias("_k"))
             .join(cal.rename({"date": f"d{off}", "k": "_k"}), on="_k", how="left").drop("_k"))
    lids = sig["listing_id"].unique().to_list()
    # the bars as formulas read them: traded days only, adjusted with the factor carried forward, a bad row unknown
    bars = (build_frame(store, market, paths.profile, {"open", "high", "low", "close", "raw_open", "raw_high", "up_limit", "ul"},
                        lids, snap=snap)
            .rename({c: f"{c}_a" for c in ("open", "high", "low", "close")})
            .with_columns(pl.int_range(pl.len()).over("listing_id").alias("j")))
    last = bars.group_by("listing_id").agg(pl.col("date").max().alias("_last"))
    entry = bars.select("listing_id", pl.col("date").alias("d1"), "raw_open", "raw_high", "up_limit", "ul",
                        pl.col("open_a").alias("_in"), pl.col("j").alias("_je"))
    s = s.join(entry, on=["listing_id", "d1"], how="left").join(last, on="listing_id", how="left")
    s = s.join(bars.select("listing_id", pl.col("date").alias("signal_date"), pl.col("close_a").alias("_c0")),
               on=["listing_id", "signal_date"], how="left").with_columns((pl.col("_in") / pl.col("_c0") - 1).alias("gap"))
    at_limit = pl.col("raw_open") >= pl.col("up_limit") - 1e-9
    if market == "JP":  # UL: the day touched its limit; at the open it is the open at the day's high
        at_limit = at_limit | (pl.col("ul") & (pl.col("raw_open") >= pl.col("raw_high")))
    s = s.with_columns(pl.when(pl.col("d1").is_null()).then(None)       # no next trading day yet: unknown, not blocked
                       .when(pl.col("_in").is_null()).then(pl.lit("halt"))
                       .when(at_limit.fill_null(False)).then(pl.lit("limit")).otherwise(None).alias("blocked"),
                       (pl.col(f"d{HOLD}").is_not_null() & (pl.col("_last") < pl.col(f"d{HOLD}"))).alias("gone"))
    # the benchmark over the same days, from the entry day's open
    iid = load_market(market).benchmark[paths.profile]
    idx = (store.scan("index_bars_1d", snap).filter(pl.col("index_id") == iid).select("date", "open", "close")
           .sort("date").collect())
    entries = s["d1"].drop_nulls()
    if entries.len() and not entries.is_in(idx["date"].implode()).any():  # every excess would be unknown, said nowhere
        raise RuleError(f"基准 {iid} 在这段区间没有日线（{entries.min()} 到 {entries.max()}），算不了超额：先补指数日线")
    s = s.join(idx.select(pl.col("date").alias("d1"), pl.col("open").alias("_io")), on="d1", how="left")
    closes = bars.select("listing_id", "date", "close_a", "j").sort("date")
    for h in HORIZONS:  # the listing's last close by that day (a suspension keeps its last price), while it still trades
        s = (s.sort(f"d{h}").join_asof(closes.rename({"date": "_d", "close_a": "_c", "j": "_j"}), left_on=f"d{h}", right_on="_d",
                                       by="listing_id", strategy="backward", check_sortedness=False)
             .join(idx.select(pl.col("date").alias(f"d{h}"), pl.col("close").alias("_ic")), on=f"d{h}", how="left"))
        alive = pl.col(f"d{h}").is_not_null() & (pl.col("_last") >= pl.col(f"d{h}"))
        r = pl.when(alive).then(pl.col("_c") / pl.col("_in") - 1)
        s = s.with_columns(r.alias(f"r{h}"), (r - (pl.col("_ic") / pl.col("_io") - 1)).alias(f"x{h}"),
                           *([pl.when(alive).then(pl.col("_j")).alias("_jx")] if h == HOLD else []))
        s = s.drop("_d", "_c", "_j", "_ic")
    # the best and worst move over the HOLD days, against the entry
    span = (s.filter(pl.col("_jx").is_not_null() & pl.col("_je").is_not_null())
            .select("sid", "listing_id", pl.int_ranges("_je", pl.col("_jx") + 1).alias("j")).explode("j", empty_as_null=False)
            .join(bars.select("listing_id", "j", "high_a", "low_a"), on=["listing_id", "j"])
            .group_by("sid").agg(pl.col("high_a").max().alias("_hi"), pl.col("low_a").min().alias("_lo")))
    s = s.join(span, on="sid", how="left").with_columns((pl.col("_hi") / pl.col("_in") - 1).alias("mfe"),
                                                         (pl.col("_lo") / pl.col("_in") - 1).alias("mae"))
    # the industry that day and the market's state (the benchmark against its 200-day line)
    ind = build_frame(store, market, paths.profile, {"industry"}, lids, snap=snap).select(
        "listing_id", pl.col("date").alias("signal_date"), "industry")
    above = idx.select(pl.col("date").alias("signal_date"),
                       (pl.col("close") > pl.col("close").rolling_mean(200)).alias("above200"))
    s = s.join(ind, on=["listing_id", "signal_date"], how="left").join(above, on="signal_date", how="left")
    return (s.sort("sid").select(pl.lit(market).alias("market"), "listing_id", "signal_date", pl.col("d1").alias("entry_date"),
                                 "blocked", "gap", *[c for h in HORIZONS for c in (f"r{h}", f"x{h}")], "mfe", "mae", "gone",
                                 "industry", "above200"))
