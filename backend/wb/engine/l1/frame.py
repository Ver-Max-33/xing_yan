"""The frame formulas are evaluated on (spec §4.4, §5.7): one row per traded bar, sorted by listing
then date, every bar from each listing's first stored bar (recursive indicators depend on their
start, so the history is never cut). A suspended or no-trade day keeps its row in the data but is
not a bar here, as in 通达信 and on the chart: windows span traded bars, and a listing suspended
on a day is simply absent that day.

Prices O/H/L/C are backward-adjusted (raw × cumulative adj_factor) and V is in shares of the first
bar (raw ÷ share_factor), so windows across ex-dates compare like with like. raw_* keep exchange
prices for absolute thresholds and limit checks. A missing factor is carried forward from the last
known one, and treated as 1 before the first."""

import numpy as np
import polars as pl

from wb.data.quality import AFFECTS, row_flags
from wb.data.store import Snapshot, Store
from wb.engine.l1.fields import FIELDS
from wb.files import move_into_place, tmp_for
from wb.markets import default_index, load_market

# column -> dataset it comes from (only joined when a formula needs it), from the field table
OPTIONAL = {f.column: f.dataset for f in FIELDS.values() if f.dataset != "bars_1d"} | {"industry": "industry"}
# columns joined from elsewhere: column -> (the dataset that must be in the snapshot, or None: always there)
EXTRA = {"board": "instruments", "s_share": "instruments", "idx_state": "index_bars_1d", "tags": None, "watch_groups": None}


def _ffill_factor(col: str) -> pl.Expr:
    return pl.col(col).forward_fill().over("listing_id").fill_null(1.0)


PRICES = ("open", "high", "low", "close")
DERIVED = {  # frame column -> stored bars_1d columns it is computed from
    **{p: {p} for p in PRICES}, **{f"raw_{p}": {p} for p in PRICES},
    "volume": {"volume", "share_factor"}, "raw_volume": {"volume"}, "amount": {"amount"}, "vwap": {"amount", "volume"},
    "up_limit": {"up_limit"}, "down_limit": {"down_limit"}, "pre_close": {"pre_close"}, "pre": {"pre_close"},
    "ul": {"ul"}, "ll": {"ll"},
}


def not_stocks(store: Store, market: str, snap: Snapshot | None = None) -> pl.LazyFrame | None:
    """The listings on a board the market says is not stocks (JP: ETFs, REITs, PRO Market …), or None: the stock pool
    (spec §7.1) leaves them out wherever the whole market is read."""
    boards = load_market(market).non_stock_boards
    if not boards:
        return None
    return store.scan("instruments", snap).filter(pl.col("board").is_in(list(boards))).select("listing_id")


def beijing_from(store: Store, snap: Snapshot | None = None) -> pl.LazyFrame:
    """北交所 listings and their first day there (`_from`): their bars before it are 新三板's, another market — not read by
    rules, structures nor the K-line (you chose, 10-13: 238 stocks carried 61,553 such bars from 2016, then months
    without one). A listing that changed its code keeps its history: only the exchange's own listing day cuts."""
    return (store.scan("instruments", snap).filter((pl.col("exchange") == "XBSE") & pl.col("list_date").is_not_null())
            .select("listing_id", pl.col("list_date").alias("_from")))


def from_listing(bars: pl.LazyFrame, store: Store, market: str, snap: Snapshot | None = None) -> pl.LazyFrame:
    """`bars` without a 北交所 listing's days before it listed there (beijing_from)."""
    if market != "CN" or "exchange" not in store.scan("instruments", snap).collect_schema().names():
        return bars
    return (bars.join(beijing_from(store, snap), on="listing_id", how="left")
            .filter(pl.col("_from").is_null() | (pl.col("date") >= pl.col("_from"))).drop("_from"))


def build_frame(store: Store, market: str, profile: str, columns: set[str] = frozenset(),
                listings: list[str] | None = None, end=None, snap: Snapshot | None = None, start=None,
                extra: pl.DataFrame | None = None) -> pl.DataFrame:
    """Only the columns a program needs are read and derived: the whole CN market over ten years is
    ~11M rows, so every extra float column costs ~90 MB. `start` cuts the history — only for formulas whose
    windows are bounded (the market view): a recursive indicator (EMA, MACD) needs every bar from the first.
    `extra`: today's temporary bars (盘中, spec §7.2) after the stored ones, in the exchange's prices; a missing
    factor is carried forward like any other. The whole market (`listings` None) is its stocks (`not_stocks`)."""
    want = set(columns) | {"close"}
    if "pre" in want:
        want.add("pre_close")
    if market == "JP" and want & {"up_limit", "down_limit", "pre_close"}:
        want |= {"raw_close", "raw_high", "raw_low", "up_limit", "down_limit", "pre_close", "ul"}
    src = {"listing_id", "date", "adj_factor"}
    for c in want:
        src |= DERIVED.get(c, set())
    if market == "JP":
        src -= {"up_limit", "down_limit"}  # JP limits come from the TSE table, not the source
    bars = store.scan("bars_1d", snap).select(sorted(src & set(store.scan("bars_1d", snap).collect_schema().names())))
    if listings is not None:
        bars = bars.filter(pl.col("listing_id").is_in(listings))
    elif (funds := not_stocks(store, market, snap)) is not None:
        bars = bars.join(funds, on="listing_id", how="anti")
    if end is not None:
        bars = bars.filter(pl.col("date") <= end)
    if start is not None:
        bars = bars.filter(pl.col("date") >= start)
    bars = from_listing(bars, store, market, snap)
    if extra is not None and extra.height:
        cols = bars.collect_schema()
        bars = pl.concat([bars.filter(pl.col("date") < extra["date"].min()),
                          extra.lazy().select([pl.col(c).cast(t) if c in extra.columns else pl.lit(None, t).alias(c) for c, t in cols.items()])])
    df = bars.sort("listing_id", "date").collect().with_columns(
        _ffill_factor("adj_factor").alias("_adj"))
    if "share_factor" in df.columns:  # factors carried forward first: a change can fall on a no-trade day
        df = df.with_columns(_ffill_factor("share_factor").alias("share_factor"))
    df = df.filter(pl.col("close").is_not_null())
    raw = [pl.col(p).alias(f"raw_{p}") for p in PRICES if f"raw_{p}" in want]
    df = df.with_columns(raw)
    df = df.with_columns([(pl.col(p) * pl.col("_adj")).alias(p) for p in PRICES if p in df.columns])
    if "vwap" in want:   # the day's average traded price, adjusted like the close; none without volume
        df = df.with_columns(pl.when(pl.col("volume") > 0).then(pl.col("amount") / pl.col("volume") * pl.col("_adj")).alias("vwap"))
    if want & {"volume", "raw_volume"}:
        df = df.with_columns(pl.col("volume").alias("raw_volume"))
        if "volume" in want:
            df = df.with_columns((pl.col("raw_volume") / _ffill_factor("share_factor")).alias("volume"))
    if market == "JP" and "up_limit" in want:
        df = _jp_limits(df)
    if "pre" in want:   # 昨收 in the close's (后复权) prices: the exchange's reference × the day's factor, else the bar before
        df = df.with_columns(pl.coalesce(pl.col("pre_close") * pl.col("_adj"),
                                         pl.col("close").shift(1).over("listing_id")).alias("pre"))
    df = _unknown_where_bad(df, store, snap)
    for col in sorted(set(columns) & set(OPTIONAL)):
        if col not in df.columns:
            df = _JOIN[OPTIONAL[col]](df, store, market, profile, snap)
    for col in sorted(set(columns) & set(EXTRA)):
        df = _EXTRA_JOIN[col](df, store, market, profile, snap)
    for col in sorted(c for c in columns if c.startswith(MEMBER)):
        df = _in_index(df, store, snap, col[len(MEMBER):])
    return df.drop([c for c in ("_adj", "share_factor") if c in df.columns])


def quality_rows(store: Store, snap: Snapshot | None = None) -> pl.DataFrame:
    """The snapshot's rows with a row-level data problem (quality.row_flags), computed once per
    snapshot and kept with its derived results."""
    snap = snap or store.latest()
    path = store.derived / snap.id / "quality_rows.parquet"
    if path.exists():
        return pl.read_parquet(path)
    cols = ["listing_id", "date", "open", "high", "low", "close", "volume", "amount", "up_limit", "down_limit",
            "pre_close", "adj_factor"]
    have = store.scan("bars_1d", snap).collect_schema().names()
    bars = store.scan("bars_1d", snap).select([c for c in cols if c in have]).collect()
    days = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    flags = row_flags(bars, days or None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_for(path)
    flags.write_parquet(tmp)
    try:
        move_into_place(tmp, path)
    except OSError:   # Windows: the other writer's file is open; it holds the same rows
        tmp.unlink(missing_ok=True)
    return flags


def _unknown_where_bad(df: pl.DataFrame, store: Store, snap: Snapshot | None) -> pl.DataFrame:
    """Spec §4.6: what a row-level data problem affects is unknown on that row (the bar stays, its
    values do not); `quality` names the problems."""
    flags = quality_rows(store, snap)
    df = df.join(flags, on=["listing_id", "date"], how="left", maintain_order="left")
    if flags.is_empty():
        return df.with_columns(pl.col("quality").cast(pl.List(pl.Utf8)))
    for check, cols in AFFECTS.items():
        hit = pl.col("quality").list.contains(check).fill_null(False)
        df = df.with_columns([pl.when(hit).then(None).otherwise(pl.col(c)).alias(c) for c in cols if c in df.columns])
    return df


def _jp_limits(df: pl.DataFrame) -> pl.DataFrame:
    """TSE daily limits from the previous close and the price-band table (spec §4.4). On an ex-date
    the base price is yesterday's close restated in today's share units, as the exchange does."""
    m = load_market("JP")
    bounds = np.array([b for b, _ in m.price_limits if b is not None], dtype=float)
    widths = np.array([w for _, w in m.price_limits], dtype=float)
    prev = (pl.col("raw_close").shift(1) * pl.col("_adj").shift(1) / pl.col("_adj")).over("listing_id")
    df = df.with_columns(prev.alias("_prev"))
    p = df["_prev"].to_numpy(allow_copy=True).astype(float)
    w = np.where(np.isnan(p), np.nan, widths[np.searchsorted(bounds, np.nan_to_num(p), side="right")])
    width = pl.Series("_w", w).fill_nan(None)
    df = df.with_columns(width).with_columns(
        (pl.col("_prev") + pl.col("_w")).alias("up_limit"), (pl.col("_prev") - pl.col("_w")).clip(lower_bound=1.0).alias("down_limit"),   # TSE: never under 1 yen
        pl.coalesce("pre_close", "_prev").alias("pre_close")).drop("_prev", "_w")
    # the exchange widens the band after days stuck at the limit: prices or the UL flag that
    # contradict the table make that day's limits unknown (spec §4.4), as on the chart
    eps = 1e-6
    widened = (pl.col("raw_high") > pl.col("up_limit") + eps) | (pl.col("raw_low") < pl.col("down_limit") - eps)
    if "ul" in df.columns:
        widened = widened | (pl.col("ul").fill_null(False) & (pl.col("raw_high") < pl.col("up_limit") - eps))
    return df.with_columns([pl.when(widened).then(None).otherwise(pl.col(c)).alias(c) for c in ("up_limit", "down_limit")])


def _valuation(df, store, market, profile, snap):
    v = store.scan("valuation", snap).select("listing_id", "date", *[c for c, d in OPTIONAL.items() if d == "valuation"]).collect()
    return df.join(v, on=["listing_id", "date"], how="left")


def _in_intervals(df: pl.DataFrame, iv: pl.DataFrame, name: str, value: str | None = None) -> pl.DataFrame:
    """Mark rows whose date lies in [start, end] of an interval of the same listing (end null = still on).
    With `value`, carry the interval's value instead of a flag."""
    j = (df.select("listing_id", "date").join(iv, on="listing_id", how="inner")
         .filter((pl.col("date") >= pl.col("start")) & (pl.col("end").is_null() | (pl.col("date") <= pl.col("end")))))
    if value is None:
        j = j.select("listing_id", "date", pl.lit(True).alias(name)).unique(["listing_id", "date"])
        return df.join(j, on=["listing_id", "date"], how="left").with_columns(pl.col(name).fill_null(False))
    j = j.sort("start").unique(["listing_id", "date"], keep="last").select("listing_id", "date", pl.col(value).alias(name))
    return df.join(j, on=["listing_id", "date"], how="left")


def _st(df, store, market, profile, snap):
    iv = store.scan("status_intervals", snap).filter(pl.col("kind") == "st").select("listing_id", "start", "end").collect()
    return _in_intervals(df, iv, "is_st")


def _industry(df, store, market, profile, snap):
    iv = store.scan("industry", snap).select("listing_id", pl.col("in_date").alias("start"),
                                             pl.col("out_date").alias("end"), "name").collect()
    if iv.height:  # CN: point-in-time membership (SW2021 level 1)
        return _in_intervals(df, iv, "industry", "name")
    inst = store.scan("instruments", snap).select("listing_id", "industry").collect()  # JP: current 33 業種
    return df.join(inst, on="listing_id", how="left")


def _index_close(df, store, market, profile, snap):
    ids = df.select("listing_id").unique().with_columns(
        pl.col("listing_id").map_elements(lambda x: default_index(x, profile), return_dtype=pl.Utf8).alias("index_id"))
    idx = store.scan("index_bars_1d", snap).select("index_id", "date", pl.col("close").alias("idx_close")).collect()
    return (df.join(ids, on="listing_id", how="left").join(idx, on=["index_id", "date"], how="left")
            .drop("index_id").sort("listing_id", "date"))


def _financials(df, store, market, profile, snap):
    """财务 PIT (spec §4.7): each day the latest report period known by its close — a restatement of that period from its
    own day; one of an older period never replaces a newer period's figures; several known the same day: the newest period."""
    cols = {c.removeprefix("fin_"): c for c, d in OPTIONAL.items() if d == "financials"}
    f = (store.scan("financials", snap).filter(pl.col("known_at").is_not_null())
         .select("listing_id", "known_at", "period_end", "ann_date", *cols).collect()
         .sort("listing_id", "known_at", "period_end", "ann_date"))
    f = (f.filter(pl.col("period_end") >= pl.col("period_end").cum_max().over("listing_id"))
         .unique(["listing_id", "known_at"], keep="last", maintain_order=True).rename(cols).drop("period_end", "ann_date"))
    out = df.sort("date").join_asof(f.sort("known_at"), left_on="date", right_on="known_at", by="listing_id", strategy="backward",
                                    check_sortedness=False)   # both sorted on the key just above
    return out.drop("known_at").sort("listing_id", "date")


def _float_holders(df, store, market, profile, snap):
    """FOREIGN_HOLDERS (多个外资持股, 10-17): 外资 as the 资料 page groups them (docs.profile.group_of —
    QFII, 香港 and foreign names, 陆股通; not the H-share nominee nor a domestic fund), each name once, in the latest list
    known: from the trading day after its announcement (A-share reports come after the close; a Saturday's on Monday), a late
    list of an older period never replacing a newer one's, as 财务. No list known: unknown."""
    from wb.docs.profile import group_of   # 0.2 s for ten years' 164,000 names
    h = store.scan("float_holders", snap).select("listing_id", "period_end", "ann_date", "holder", "holder_type").collect()
    names = h.select("holder", "holder_type").unique().with_columns(pl.struct("holder", "holder_type").map_elements(
        lambda r: group_of(r["holder"], r["holder_type"]) == "外资", return_dtype=pl.Boolean).alias("_fx"))
    lists = (h.join(names, on=["holder", "holder_type"], how="left", nulls_equal=True)
             .group_by("listing_id", "period_end", "ann_date")
             .agg(pl.col("holder").filter(pl.col("_fx")).n_unique().cast(pl.Float64).alias("foreign_holders"))
             .sort("listing_id", "ann_date", "period_end"))
    lists = (lists.filter(pl.col("period_end") >= pl.col("period_end").cum_max().over("listing_id"))
             .unique(["listing_id", "ann_date"], keep="last", maintain_order=True)
             .select("listing_id", (pl.col("ann_date") + pl.duration(days=1)).alias("_from"), "foreign_holders"))
    out = df.sort("date").join_asof(lists.sort("_from"), left_on="date", right_on="_from", by="listing_id", strategy="backward",
                                    check_sortedness=False)   # both sorted on the key just above
    return out.drop("_from").sort("listing_id", "date")


def _chips(df, store, market, profile, snap):
    from wb.engine.l1.chips import chip_rows   # computed once per snapshot over the full history (its 210-bar windows)
    return df.join(chip_rows(store, market, profile, snap), on=["listing_id", "date"], how="left")


_JOIN = {"valuation": _valuation, "status_intervals": _st, "industry": _industry, "index_bars_1d": _index_close,
         "financials": _financials, "chips": _chips, "float_holders": _float_holders}


def index_states(idx: pl.DataFrame) -> pl.DataFrame:
    """index_id, date, close, ma60, ma200, idx_state on the indexes' own trading days (spec §7.3 大盘状态): 1 上升
    (above both lines, the 60-day line higher than 5 bars ago), −1 下降 (below both, the 60-day line lower), 0 震荡;
    unknown until the 200-day line exists."""
    df = idx.sort("index_id", "date").with_columns(
        pl.col("close").rolling_mean(60).over("index_id").alias("ma60"),
        pl.col("close").rolling_mean(200).over("index_id").alias("ma200"))
    rising = pl.col("ma60") > pl.col("ma60").shift(5).over("index_id")
    falling = pl.col("ma60") < pl.col("ma60").shift(5).over("index_id")
    above = (pl.col("close") > pl.col("ma60")) & (pl.col("close") > pl.col("ma200"))
    below = (pl.col("close") < pl.col("ma60")) & (pl.col("close") < pl.col("ma200"))
    state = (pl.when(pl.col("ma200").is_null()).then(None).when(above & rising).then(1)
             .when(below & falling).then(-1).otherwise(0).cast(pl.Int8))
    return df.with_columns(state.alias("idx_state"))


def _idx_state(df, store, market, profile, snap):
    ids = df.select("listing_id").unique().with_columns(
        pl.col("listing_id").map_elements(lambda x: default_index(x, profile), return_dtype=pl.Utf8).alias("index_id"))
    idx = index_states(store.scan("index_bars_1d", snap).select("index_id", "date", "close").collect())
    return (df.join(ids, on="listing_id", how="left")
            .join(idx.select("index_id", "date", "idx_state"), on=["index_id", "date"], how="left")
            .drop("index_id").sort("listing_id", "date"))


def _board(df, store, market, profile, snap):
    return df.join(store.scan("instruments", snap).select("listing_id", "board").collect(), on="listing_id", how="left")


def _s_share(df, store, market, profile, snap):
    """未股改: the name starts with S but not ST (S佳通, S*ST…), by the name now — the data keeps no full name history
    (status_intervals keep the names of ST spells only), so a share that did the reform in 2013 is not left out before it."""
    name = pl.col("name")
    s = store.scan("instruments", snap).select("listing_id", (name.str.starts_with("S") & ~name.str.starts_with("ST")).alias("s_share"))
    return df.join(s.collect(), on="listing_id", how="left").with_columns(pl.col("s_share").fill_null(False))


def _names_per_listing(df: pl.DataFrame, pairs: list[tuple[str, str]], col: str) -> pl.DataFrame:
    """Join a list of names per listing (an empty list where there is none)."""
    names = (pl.DataFrame(pairs, schema={"listing_id": pl.Utf8, col: pl.Utf8}, orient="row")
             .group_by("listing_id").agg(pl.col(col)))
    return df.join(names, on="listing_id", how="left").with_columns(pl.col(col).fill_null([]))


def _tags(df, store, market, profile, snap):
    from wb.db import connect
    rows = connect(store.paths.shared_db, "shared").execute("SELECT listing_id, tag FROM listing_tag").fetchall()
    return _names_per_listing(df, [tuple(r) for r in rows], "tags")


def _watch_groups(df, store, market, profile, snap):
    from wb.db import connect
    rows = connect(store.paths.shared_db, "shared").execute(
        "SELECT i.listing_id, g.name FROM watch_item i JOIN watch_group g ON g.id = i.group_id").fetchall()
    return _names_per_listing(df, [tuple(r) for r in rows], "watch_groups")


_EXTRA_JOIN = {"board": _board, "s_share": _s_share, "idx_state": _idx_state, "tags": _tags, "watch_groups": _watch_groups}

# TOPIX's size classes as indexes of their own (JPX publishes one list with the class of each member)
TOPIX_PARTS = {"JP:IDX:CORE30": ("TOPIX Core30",), "JP:IDX:TOPIX100": ("TOPIX Core30", "TOPIX Large70"),
               "JP:IDX:TOPIX500": ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400")}
MEMBER = "in:"  # frame column "in:<index id>": the listing is in that index on the day


def index_membership(rows: pl.DataFrame) -> pl.DataFrame:
    """The stored lists, with TOPIX100 / TOPIX500 / Core30 drawn from TOPIX's size classes."""
    topix = rows.filter(pl.col("index_id") == "JP:IDX:TOPIX")
    return pl.concat([rows, *[topix.filter(pl.col("segment").is_in(list(segs))).with_columns(pl.lit(iid).alias("index_id"))
                              for iid, segs in TOPIX_PARTS.items()]])


def _in_index(df, store, snap, iid: str):
    """True / False from the day after a list that has / lacks the listing (a month-end list is known at the close
    of its day, so it counts from the next one); unknown before the index's first list."""
    col = f"{MEMBER}{iid}"
    m = index_membership(store.scan("index_members", snap).collect()).filter(pl.col("index_id") == iid)
    if m.is_empty():
        return df.with_columns(pl.lit(None, pl.Boolean).alias(col))
    lists = (m.select(pl.col("date").alias("_list")).unique().sort("_list")
             .with_columns((pl.col("_list") + pl.duration(days=1)).alias("_from")))
    days = df.select("date").unique().sort("date").join_asof(lists, left_on="date", right_on="_from", strategy="backward")
    members = m.select(pl.col("date").alias("_list"), "listing_id").unique().with_columns(pl.lit(True).alias("_in"))
    return (df.join(days.select("date", "_list"), on="date", how="left")
            .join(members, on=["_list", "listing_id"], how="left")
            .with_columns(pl.when(pl.col("_list").is_null()).then(None).otherwise(pl.col("_in").fill_null(False)).alias(col))
            .drop("_list", "_in").sort("listing_id", "date"))


def available_columns(store: Store, snap: Snapshot | None = None) -> set[str]:
    """Columns a frame for this snapshot can provide (so a formula needing more fails at compile time)."""
    snap = snap or store.latest()
    have = {"open", "high", "low", "close", "volume", "amount", "raw_open", "raw_high", "raw_low", "raw_close",
            "raw_volume", "up_limit", "down_limit", "pre_close", "pre", "ul", "ll", "vwap"}
    if snap is None:
        return have
    for col, ds in OPTIONAL.items():
        if snap.manifest.get("valuation" if ds == "chips" else ds):   # chips spread the days' turnover (valuation)
            have.add(col)
    have |= {col for col, ds in EXTRA.items() if ds is None or snap.manifest.get(ds)}
    if snap.manifest.get("index_members"):  # one column per index that has a list
        ids = index_membership(store.scan("index_members", snap).select("index_id", "segment").collect()
                               .with_columns(pl.lit(None, pl.Utf8).alias("listing_id")))["index_id"].unique()
        have |= {f"{MEMBER}{i}" for i in ids}
    return have
