"""Data quality checks (spec §4.6).

Severity:
  block  dataset-level problem (duplicate keys, wrong units, estimated volume): never publish
  row    a specific row is wrong: publish, record the issue; later phases treat it as unknown
  warn   suspicious but possibly real (gaps, factor jumps)
"""
import datetime as dt
import math
from dataclasses import dataclass

import polars as pl

MAX_ROW_ISSUES = 200  # per check; the count is still reported in full


@dataclass(frozen=True)
class Issue:
    check: str
    severity: str
    detail: str
    listing_id: str | None = None
    date: dt.date | None = None  # the field name shadows the type, hence `dt.date`


def _rows(df: pl.DataFrame, check: str, severity: str, detail: str) -> list[Issue]:
    issues = [Issue(check, severity, detail, r["listing_id"], r["date"])
              for r in df.head(MAX_ROW_ISSUES).iter_rows(named=True)]
    if df.height > MAX_ROW_ISSUES:
        issues.append(Issue(check, severity, f"{df.height - MAX_ROW_ISSUES} more rows not listed"))
    return issues


# Row-level problems that make a row's values unknown (spec §4.6), the predicate each check uses, and
# the frame columns it makes unknown. check_bars reports them at publish; row_flags marks every such
# row of a snapshot so formulas and structures read them as unknown, with the reason.
_PRICES = ("open", "high", "low", "close")
AFFECTS = {
    "ohlc": _PRICES + tuple(f"raw_{p}" for p in _PRICES),
    "volume_negative": ("volume", "raw_volume", "vwap"),
    "vwap_range": ("volume", "raw_volume", "amount", "vwap"),
    "limit_range": ("up_limit", "down_limit"),
    # the close outside the exchange's own limits AND the day's turnover ÷ volume outside the day's prices: the prices
    # are the wrong ones (a decimal shift, another listing's row) — alone, either can be real (an unlimited day the
    # limit table did not know, odd lots rounded to the yuan); both at once never happened in 10 years of A shares
    "price_range": _PRICES + tuple(f"raw_{p}" for p in _PRICES) + ("vwap",),
    "adj_identity": (*_PRICES, "vwap"),  # the backward-adjusted prices (the day's average too); the raw ones are fine
}


def _bad_ohlc() -> pl.Expr:
    return ((pl.col("low") > pl.min_horizontal("open", "close")) | (pl.col("high") < pl.max_horizontal("open", "close"))
            | (pl.col("low") > pl.col("high")))


def _vwap_outside() -> pl.Expr:
    vwap = pl.col("amount") / pl.col("volume")
    slack = 1.0 / pl.col("volume")  # amount is published rounded to 1 yuan/yen: matters for odd lots
    return (vwap < pl.col("low") * 0.98 - slack) | (vwap > pl.col("high") * 1.02 + slack)


def _limit_outside() -> pl.Expr:
    return (pl.col("close") > pl.col("up_limit") + 1e-6) | (pl.col("close") < pl.col("down_limit") - 1e-6)


def check_bars(df: pl.DataFrame, trading_days: list[dt.date] | None = None, halts: set[dt.date] | frozenset = frozenset()) -> list[Issue]:
    issues: list[Issue] = []
    dup = df.group_by("listing_id", "date").len().filter(pl.col("len") > 1)
    if dup.height:
        issues.append(Issue("unique_key", "block", f"{dup.height} duplicate (listing_id, date) keys"))

    # A day where most rows read as suspended is a half-published or misread day (null close/volume) — every
    # other check would see nothing to check on it — when it jumps there from an ordinary day. A market-wide
    # halt builds up over days instead (A 股 2015-07: 562 → 760 → 1,312 → 1,438 of ~2,800, then back): the day
    # before already had at least half its share suspended. That one is real, published with a warning; so is a whole
    # day the exchange halted (`halts`, the market's list: TSE 2020-10-01), which jumps from an ordinary day.
    per_day = (df.group_by("date").agg(pl.len().alias("n"), pl.col("suspended").fill_null(False).sum().alias("s"))
               .sort("date").with_columns((pl.col("s") / pl.col("n")).shift(1).alias("before")))
    for r in per_day.filter((pl.col("n") >= 20) & (pl.col("s") > 0.5 * pl.col("n"))).iter_rows(named=True):
        built_up = r["before"] is not None and r["before"] >= 0.5 * r["s"] / r["n"]
        if r["date"] in halts:
            issues.append(Issue("mass_suspension", "warn", f"{r['s']}/{r['n']} rows suspended: the exchange halted "
                                "trading all day", None, r["date"]))
            continue
        issues.append(Issue("mass_suspension", "warn", f"{r['s']}/{r['n']} rows suspended, after a day with "
                            f"{r['before']:.0%}: a market-wide halt", None, r["date"]) if built_up else
                      Issue("mass_suspension", "block",
                            f"{r['s']}/{r['n']} rows suspended on one day: the source's data looks incomplete",
                            None, r["date"]))

    traded = df.filter(~pl.col("suspended").fill_null(False))
    missing_px = traded.filter(pl.col("close").is_null())
    issues += _rows(missing_px, "missing_price", "row", "trading row without close")

    px = traded.filter(pl.col("close").is_not_null())
    issues += _rows(px.filter(_bad_ohlc()), "ohlc", "row", "OHLC containment violated")
    issues += _rows(df.filter(pl.col("volume") < 0), "volume_negative", "row", "negative volume")
    issues += _rows(df.filter(pl.col("suspended").fill_null(False) & (pl.col("volume") == 0)),
                    "suspended_zero_volume", "row", "suspended day must have null volume, not 0")

    with_amt = px.filter(pl.col("amount").is_not_null() & (pl.col("volume") > 0))
    if with_amt.height >= 20:
        vwap = pl.col("amount") / pl.col("volume")
        exact = ((vwap - pl.col("close")).abs() / pl.col("close")) < 1e-9
        # the whole batch, and each day of 20 rows or more on its own: a daily update's batch comes with the day before
        # (published, sound), which halves the share of a day that is all wrong — estimated_volume could not block it
        per = (with_amt.group_by("date").agg(pl.len().alias("n"), _vwap_outside().sum().alias("outside"), exact.sum().alias("exact"))
               .filter(pl.col("n") >= 20).sort("date"))
        outside = with_amt.filter(_vwap_outside())
        wrong = per.filter(pl.col("outside") > 0.05 * pl.col("n"))
        if outside.height > 0.05 * with_amt.height:
            issues.append(Issue("units", "block",
                                f"amount/volume outside [low, high] on {outside.height}/{with_amt.height} rows; "
                                "volume or amount unit is wrong"))
        elif wrong.height:
            issues += [Issue("units", "block", f"amount/volume outside [low, high] on {r['outside']}/{r['n']} rows of {r['date']}; "
                             "volume or amount unit is wrong", None, r["date"]) for r in wrong.iter_rows(named=True)]
        else:
            issues += _rows(outside, "vwap_range", "row", "amount/volume outside the day's range")
        if with_amt.filter(exact).height > 0.95 * with_amt.height or per.filter(pl.col("exact") > 0.95 * pl.col("n")).height:
            issues.append(Issue("estimated_volume", "block",
                                "volume equals amount/close on almost every row: it was estimated, not reported"))

    issues += _rows(px.filter(_limit_outside()), "limit_range", "row", "close outside the day's limit-up/limit-down prices")
    if "amount" in px.columns:
        issues += _rows(px.filter(pl.col("amount").is_not_null() & (pl.col("volume") > 0) & _vwap_outside() & _limit_outside()),
                        "price_range", "row", "prices outside the day's limits and apart from turnover ÷ volume: read as unknown")
    issues += _factor_jumps(px)
    issues += _adj_identity(px, trading_days)
    if trading_days is not None:
        issues += _gaps(df, trading_days)
        issues += _day_sanity(df, trading_days, halts)
    return issues


DAY_LIMITS = {"day_coverage": 0.2, "day_no_factor": 0.05, "day_no_volume": 0.5, "day_identity": 0.2, "day_repeat": 0.2}


def _day_sanity(df: pl.DataFrame, trading_days: list[dt.date], halts: set[dt.date] | frozenset) -> list[Issue]:
    """A day as a whole against the trading day before it (in `df`): the source answered only part of the day, or an old
    day under a new date, or without a column it gave the day before — never published (block). Measured on the stored
    real data 2016-10 → 2026-10 (CN 2,430 days, JP 2,441): of the listings that traded one day at most 0.84% (CN) have no
    row the next; no traded row ever lacked adj_factor or had volume 0; adj_identity failed on at most 0.07% of a day; at
    most 0.14% (CN) / 0.44% (JP) of a day's rows repeated the day before exactly.
      day_coverage   more than 20% of the listings that traded the day before have no row at all (half published)
      day_no_factor  more than 5% of the traded rows lack adj_factor where the day before had them (an empty answer)
      day_no_volume  more than half the traded rows have volume 0 (a day filled with the close before; not a halt)
      day_identity   adj_identity fails on more than 20% of the day's comparable rows (yesterday's day re-dated)
      day_repeat     more than 20% of the day's rows repeat the day before exactly (the same, where no close before is
                     given to check against: JP)"""
    days = sorted(trading_days)
    nxt = pl.DataFrame({"d0": days[:-1], "date": days[1:]}, schema={"d0": pl.Date, "date": pl.Date})
    have = set(df["date"].unique().to_list())
    pairs = nxt.filter(pl.col("d0").is_in(list(have)) & pl.col("date").is_in(list(have)))
    if pairs.is_empty():
        return []
    traded = df.filter(pl.col("close").is_not_null() & ~pl.col("suspended").fill_null(False))
    out = []
    for d0, d1 in pairs.iter_rows():
        before, today = traded.filter(pl.col("date") == d0), traded.filter(pl.col("date") == d1)
        rows = df.filter(pl.col("date") == d1).select("listing_id")
        if before.height >= 20:
            gone = before.select("listing_id").join(rows, on="listing_id", how="anti").height / before.height
            if gone > DAY_LIMITS["day_coverage"]:
                out.append(Issue("day_coverage", "block", f"{gone:.0%} of the {before.height} listings that traded on {d0} have no "
                                 "row: the source's day looks half published", None, d1))
        if today.height < 20:
            continue
        if "adj_factor" in df.columns and before.height and before["adj_factor"].null_count() <= DAY_LIMITS["day_no_factor"] * before.height:
            miss = today["adj_factor"].null_count() / today.height
            if miss > DAY_LIMITS["day_no_factor"]:
                out.append(Issue("day_no_factor", "block", f"{miss:.0%} of the day's traded rows have no adj_factor (the day before "
                                 "had them): an ex-date would read as a price drop", None, d1))
        same = today.join(before, on=["open", "high", "low", "close", "volume", "listing_id"], how="semi").height / today.height
        if same > DAY_LIMITS["day_repeat"]:
            out.append(Issue("day_repeat", "block", f"{same:.0%} of the day's rows repeat {d0} exactly (prices and volume): "
                             "the day before under a new date?", None, d1))
        zero = today.filter(pl.col("volume") == 0).height / today.height
        if zero > DAY_LIMITS["day_no_volume"] and d1 not in halts:
            out.append(Issue("day_no_volume", "block", f"{zero:.0%} of the day's traded rows have volume 0: not a trading day's data",
                             None, d1))
        both = traded.filter(pl.col("date").is_in([d0, d1]))
        comparable = both.filter(pl.col("pre_close").is_not_null() & pl.col("adj_factor").is_not_null()) if "pre_close" in both.columns else both.head(0)
        n = comparable.filter(pl.col("date") == d1).height
        if n >= 20 and comparable.filter(pl.col("date") == d0).height:
            bad = _adj_mismatch(comparable, [d0, d1]).filter(pl.col("date") == d1).height / n
            if bad > DAY_LIMITS["day_identity"]:
                out.append(Issue("day_identity", "block", f"close the day before ÷ today's close before disagrees with the factors on "
                                 f"{bad:.0%} of the rows: an old day under a new date?", None, d1))
    return out


def _adj_identity(px: pl.DataFrame, trading_days: list[dt.date] | None) -> list[Issue]:
    return _rows(_adj_mismatch(px, trading_days), "adj_identity", "row",
                 "close[t-1]/pre_close[t] disagrees with the adj_factor ratio")


def _adj_mismatch(px: pl.DataFrame, trading_days: list[dt.date] | None) -> pl.DataFrame:
    """Same-source cross-check (CN): on consecutive trading days
         close[t-1] / pre_close[t]  ==  adj_factor[t] / adj_factor[t-1]
    because the exchange's pre_close already removes the ex-rights effect. A mismatch means a
    missing/wrong adjustment factor or a missing day."""
    need = ("pre_close", "adj_factor")
    if px.is_empty() or any(c not in px.columns or px.get_column(c).null_count() == px.height for c in need):
        return px.select("listing_id", "date").clear()
    j = px.sort("listing_id", "date").with_columns(
        pl.col("date").shift(1).over("listing_id").alias("prev_date"),
        pl.col("close").shift(1).over("listing_id").alias("prev_close"),
        pl.col("adj_factor").shift(1).over("listing_id").alias("prev_adj"),
    ).drop_nulls(["prev_close", "prev_adj", "pre_close", "adj_factor"])
    if trading_days is not None:  # only compare rows that really are consecutive trading days
        nxt = pl.DataFrame({"prev_date": sorted(trading_days)[:-1], "date": sorted(trading_days)[1:]},
                           schema={"prev_date": pl.Date, "date": pl.Date})
        j = j.join(nxt, on=["prev_date", "date"], how="semi")
    return j.filter(((pl.col("prev_close") / pl.col("pre_close")) / (pl.col("adj_factor") / pl.col("prev_adj")) - 1).abs() > 0.005)


def row_flags(bars: pl.DataFrame, trading_days: list[dt.date] | None = None) -> pl.DataFrame:
    """(listing_id, date, quality: list of check names) for every traded row with a row-level problem
    in AFFECTS — the same predicates check_bars reports, applied to every row (not capped)."""
    px = bars.filter(pl.col("close").is_not_null())
    parts = [px.filter(_bad_ohlc()).select("listing_id", "date", pl.lit("ohlc").alias("check")),
             px.filter(pl.col("volume") < 0).select("listing_id", "date", pl.lit("volume_negative").alias("check"))]
    if "amount" in px.columns:
        parts.append(px.filter(pl.col("amount").is_not_null() & (pl.col("volume") > 0) & _vwap_outside())
                     .select("listing_id", "date", pl.lit("vwap_range").alias("check")))
    if "up_limit" in px.columns:
        parts.append(px.filter(_limit_outside()).select("listing_id", "date", pl.lit("limit_range").alias("check")))
        if "amount" in px.columns:
            parts.append(px.filter(pl.col("amount").is_not_null() & (pl.col("volume") > 0) & _vwap_outside() & _limit_outside())
                         .select("listing_id", "date", pl.lit("price_range").alias("check")))
    parts.append(_adj_mismatch(px, trading_days).select("listing_id", "date", pl.lit("adj_identity").alias("check")))
    return (pl.concat(parts).group_by("listing_id", "date").agg(pl.col("check").unique().sort().alias("quality"))
            .sort("listing_id", "date"))


def check_index_bars(df: pl.DataFrame, trading_days: list[dt.date],
                     since: dict[str, dt.date] | None = None) -> list[Issue]:
    """Every index must have a row on every trading day of the batch (catches sources that return
    a single row for a month, as Yahoo did for 000300.SS on 2026-10-02) — from the day it exists
    (`since`: index_id -> first day with data; 科创50 starts 2019-12-31)."""
    if df.is_empty():
        return []
    days = [d for d in trading_days if df["date"].min() <= d <= df["date"].max()]
    ids = df.select("index_id").unique().with_columns(
        pl.col("index_id").replace_strict(since or {}, default=None, return_dtype=pl.Date).alias("since"))
    expected = ids.join(pl.DataFrame({"date": days}, schema={"date": pl.Date}), how="cross").filter(
        pl.col("since").is_null() | (pl.col("date") >= pl.col("since"))).drop("since")
    missing = expected.join(df.select("index_id", "date"), on=["index_id", "date"], how="anti")
    return [Issue("index_missing_day", "warn", "no index row on a trading day", r["index_id"], r["date"])
            for r in missing.sort("index_id", "date").head(MAX_ROW_ISSUES).iter_rows(named=True)]


def _factor_jumps(px: pl.DataFrame) -> list[Issue]:
    if px.get_column("adj_factor").null_count() == px.height:
        return []
    j = (
        px.sort("listing_id", "date")
        .with_columns((pl.col("close") * pl.col("adj_factor")).alias("h"))
        .with_columns((pl.col("h") / pl.col("h").shift(1).over("listing_id")).log().abs().alias("lr"))
        .filter(pl.col("lr") > math.log(1.6))
    )
    return _rows(j, "adj_jump", "warn", "adjusted close moved more than 60% in one day")


def _gaps(df: pl.DataFrame, trading_days: list[dt.date]) -> list[Issue]:
    """Trading days between a listing's first and last row that have no row at all.
    Suspended days have a row with suspended=True, so a gap means the fetch missed data."""
    days = pl.DataFrame({"date": sorted(trading_days)}, schema={"date": pl.Date})
    span = df.group_by("listing_id").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"))
    expected = span.join(days, how="cross").filter(pl.col("date").is_between(pl.col("first"), pl.col("last")))
    missing = expected.join(df.select("listing_id", "date"), on=["listing_id", "date"], how="anti")
    return _rows(missing.sort("listing_id", "date"), "missing_day", "warn", "no row on a trading day")


def check_intraday(minutes: pl.DataFrame, bars: pl.DataFrame, end: dt.time) -> list[Issue]:
    """分钟线和日线对账: each day whose kept minutes carry their own turnover (a source that gives it — TickFlow) and
    reach the session's last minute (the one before `end`) is summed and set beside the exchange's daily bar: the open,
    high, low and close must be the bar's, the volume and the turnover within 0.05% — or 50 shares and 120 yuan, what
    counting lots and rounding to the yuan leave a quiet stock (TickFlow's day comes to the exchange's shares rounded to
    a lot: never more than 50 off on 20 days of 300 stocks). A day the minutes do not reach the close (the app was closed
    then) or a source without each minute's turnover is not judged. Measured 2026-10-10: all agree but 北交所's, whose
    minutes count lots per minute and put block trades in the last one."""
    if minutes.is_empty() or "amount" not in minutes.columns:
        return []
    last = (dt.datetime.combine(dt.date.min, end) - dt.timedelta(minutes=1)).time()
    day = (minutes.with_columns(pl.col("at").dt.date().alias("date")).sort("at").group_by("listing_id", "date").agg(
        pl.col("open").first().alias("m_open"), pl.col("high").max().alias("m_high"), pl.col("low").min().alias("m_low"),
        pl.col("close").last().alias("m_close"), pl.col("volume").sum().alias("m_volume"), pl.col("amount").sum().alias("m_amount"),
        pl.col("amount").null_count().alias("_no_turnover"), pl.col("at").max().dt.time().alias("_last"))
        .filter((pl.col("_no_turnover") == 0) & (pl.col("_last") >= last)))
    out = []
    for r in day.join(bars, on=["listing_id", "date"]).sort("listing_id", "date").iter_rows(named=True):
        said = [f"{zh} {r['m_' + k]:g} / {r[k]:g}" for k, zh in (("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"))
                if r.get(k) is not None and abs(r["m_" + k] - r[k]) > 5e-4]
        said += [f"{zh} {r['m_' + k] / r[k] - 1:+.1%}" for k, zh, floor in (("volume", "成交量", 50), ("amount", "成交额", 120))
                 if r.get(k) and abs(r["m_" + k] - r[k]) > max(5e-4 * r[k], floor)]
        if said:
            out.append(Issue("intraday", "warn", "盘中分钟线和日线对不上：" + "，".join(said), r["listing_id"], r["date"]))
    return out[:MAX_ROW_ISSUES] + ([Issue("intraday", "warn", f"{len(out) - MAX_ROW_ISSUES} more rows not listed")]
                                   if len(out) > MAX_ROW_ISSUES else [])


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "block"]


# ---- 资金流向 (design 2026-10-15 §5.1) ----------------------------------------------------------------------------------
FLOW_PAIR, FLOW_BAR, FLOW_BROKEN, FLOW_BAD_DAY = 0.01, 0.05, 0.5, 0.2
FLOW_KINDS = ("sm", "md", "lg", "elg")


def flow_buy() -> pl.Expr:
    """A money-flow row's buy total (the four order sizes); null when the row is missing."""
    return pl.when(pl.col("buy_sm").is_not_null()).then(pl.sum_horizontal([f"buy_{k}" for k in FLOW_KINDS]))


def flow_sell() -> pl.Expr:
    return pl.when(pl.col("sell_sm").is_not_null()).then(pl.sum_horizontal([f"sell_{k}" for k in FLOW_KINDS]))


def check_moneyflow(flow: pl.DataFrame, bars: pl.DataFrame, recent_from: dt.date) -> list[Issue]:
    """资金流向 against itself and the day's bars. Tushare counts both sides of every trade, so the buy and sell totals
    agree within 1% and the buy total is within 5% of the bar's amount — since 2019; in 2016–2018 a tenth or more of each
    day's rows did not (ledger 2026-10-15: the way it was kept then, not broken data). A recent day (from `recent_from`)
    with over a fifth of its 沪深 rows out is refused (block: asked again next run); older days, and a day under that
    share, are only said (warn). 北交所 is not judged: its amounts and flows never agree."""
    if flow.is_empty():
        return []
    x = (flow.filter(pl.col("listing_id").str.slice(3, 1).is_in(["0", "3", "6"]))   # 沪深 A-share codes
         .join(bars.select("listing_id", "date", "amount"), on=["listing_id", "date"], how="left")
         .with_columns((((flow_buy() / flow_sell() - 1).abs() <= FLOW_PAIR)
                        & ((flow_buy() / pl.col("amount") - 1).abs() <= FLOW_BAR)).fill_null(False).alias("ok")))
    out = []
    for (d,), g in sorted(x.group_by("date"), key=lambda t: t[0][0]):
        bad = int((~g["ok"]).sum())
        if not bad:
            continue
        refuse = d >= recent_from and bad > FLOW_BAD_DAY * g.height
        out.append(Issue("moneyflow_day" if refuse else "moneyflow_rows", "block" if refuse else "warn",
                         f"{d}：{bad} / {g.height} 只沪深股票的资金流向和成交额对不上", None, d))
    return out
