"""Chart series (spec §8.1, §8.6): one security's bars in the display basis with every indicator
the chart draws, computed by the backend over the full history and then paged.

Bases (spec §4.4):
  qfq  前复权 (default): the latest bar shows real traded prices; history is scaled backward.
  hfq  后复权.
  raw  不复权: indicators are computed on raw prices, so they jump at ex-dates (as in 通达信).
qfq and hfq differ by one constant per security, so every ratio, cross and sign is identical.
When the adjustment factor is unknown (Tushare free tier) all three bases equal raw and
`adjusted` is False so the UI can say "未复权".
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

import polars as pl

from wb.engine.l1.functions.base import CROSS, REF
from wb.engine.l1.functions.course import AMPLITUDE, GROUND_VOL, NEW_HIGH, VOL_MULT, YEAR_LEFT
from wb.engine.l1.functions.trend import BOLL, KDJ, MACD, MAS, VOL_MAS
from wb.markets import limit_width, load_market

PERIODS = ("day", "week", "month")
MINUTE_PERIODS = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60}   # the K-line page's 分钟 bars (10-12)
MINUTE_DAYS = {"m1": 3, "m5": 10, "m15": 20, "m30": 20, "m60": 20}     # trading days each shows
MINUTE_HISTORY = 60   # trading days a minute chart's series is worked out on (10-13): its MACD, KDJ, MA60 warm up on the days
# before those it shows, as 通达信's (a 60-minute MACD settles over about 40 days)
BASES = ("qfq", "hfq", "raw")
PRICE_COLS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class Series:
    listing_id: str
    period: str
    basis: str
    adjusted: bool          # False when no adjustment factor is known: prices are raw
    frame: pl.DataFrame     # sorted by date; one row per bar
    scales: list = ()       # daily (date, display/raw) change points: drawings convert by their own day


def _basis(daily: pl.DataFrame, basis: str) -> tuple[pl.DataFrame, bool]:
    """Convert raw daily rows to the display basis. Adds `scale` = display price / raw price."""
    adj = pl.col("adj_factor")
    adjusted = daily["adj_factor"].null_count() < daily.height
    latest_adj = daily.filter(adj.is_not_null())["adj_factor"][-1] if adjusted else None
    latest_share = daily.filter(pl.col("share_factor").is_not_null())["share_factor"]
    latest_share = latest_share[-1] if latest_share.len() else None
    if basis == "raw" or not adjusted:
        scale = pl.lit(1.0)
    elif basis == "hfq":
        scale = adj.fill_null(strategy="forward").fill_null(1.0)
    else:
        scale = adj.fill_null(strategy="forward").fill_null(1.0) / latest_adj
    out = daily.with_columns(scale.alias("scale"))
    # Volume follows the share factor: hfq = shares at the first bar, qfq = today's share units.
    if basis == "raw" or latest_share is None:
        vol = pl.col("volume")
    elif basis == "hfq":
        vol = pl.col("volume") / pl.col("share_factor").fill_null(strategy="forward").fill_null(1.0)
    else:
        vol = pl.col("volume") / pl.col("share_factor").fill_null(strategy="forward").fill_null(1.0) * latest_share
    return out.with_columns(
        [(pl.col(c) * pl.col("scale")).alias(c) for c in (*PRICE_COLS, "pre_close", "up_limit", "down_limit")]
        + [vol.alias("volume")]
    ), adjusted


def _jp_limits(df: pl.DataFrame) -> pl.DataFrame:
    """TSE limit prices from the previous raw close and the price-band table (spec §4.4)."""
    m = load_market("JP")
    # The base price on an ex-date is the previous close in today's share units (TSE adjusts it).
    adj = df["adj_factor"].fill_null(strategy="forward").fill_null(1.0)
    prev = df["raw_close"].shift(1) * adj.shift(1) / adj
    width = [limit_width(m, p) if p is not None else None for p in prev.to_list()]
    w = pl.Series("w", width, dtype=pl.Float64)
    df = df.with_columns((prev + w).alias("up_limit_raw"), (prev - w).clip(lower_bound=1.0).alias("down_limit_raw"),   # TSE: never under 1 yen
                       prev.alias("pre_close_raw"))
    # The exchange widens the band after days stuck at the limit (spec §4.4): when the day's prices
    # or the UL flag contradict the table, the limits that day are unknown, not the table's.
    eps = 1e-6
    widened = (pl.col("high") > pl.col("up_limit_raw") + eps) | (pl.col("low") < pl.col("down_limit_raw") - eps)
    if "ul" in df.columns:
        widened = widened | (pl.col("ul").fill_null(False) & (pl.col("high") < pl.col("up_limit_raw") - eps))
    return df.with_columns([pl.when(widened).then(None).otherwise(pl.col(c)).alias(c)
                            for c in ("up_limit_raw", "down_limit_raw")])


def daily_series(daily: pl.DataFrame, market: str, basis: str = "qfq") -> tuple[pl.DataFrame, bool]:
    """Raw daily rows (bars_1d of one security, any order) -> display-basis daily rows."""
    if basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}")
    d = (daily.filter(pl.col("close").is_not_null()).sort("date")
         .with_columns(pl.col("close").alias("raw_close"), pl.col("volume").alias("raw_volume")))
    if market == "JP":
        d = _jp_limits(d).with_columns(
            pl.col("up_limit_raw").alias("up_limit"), pl.col("down_limit_raw").alias("down_limit"),
            pl.coalesce("pre_close", "pre_close_raw").alias("pre_close")).drop(
            "up_limit_raw", "down_limit_raw", "pre_close_raw")
    out, adjusted = _basis(d, basis)
    eps = 1e-6
    if market == "JP":
        lim_up = pl.col("ul").fill_null(False) & (pl.col("close") >= pl.col("high") - eps)
        lim_dn = pl.col("ll").fill_null(False) & (pl.col("close") <= pl.col("low") + eps)
    else:
        lim_up = pl.col("close") >= pl.col("up_limit") - eps * pl.col("scale")
        lim_dn = pl.col("close") <= pl.col("down_limit") + eps * pl.col("scale")
    return out.with_columns(lim_up.fill_null(False).alias("limit_up"), lim_dn.fill_null(False).alias("limit_down")), adjusted


def aggregate(day: pl.DataFrame, period: str, calendar_open: list[date] | None = None) -> pl.DataFrame:
    """Daily display rows -> weekly/monthly bars dated by their last trading day.
    `complete` is False for a period that still has trading days ahead (per the calendar)."""
    if period == "day":
        return day.with_columns(pl.lit(True).alias("complete"))
    key = ([pl.col("date").dt.iso_year().alias("_y"), pl.col("date").dt.week().alias("_p")] if period == "week"
           else [pl.col("date").dt.year().alias("_y"), pl.col("date").dt.month().alias("_p")])
    # each day's 均价 × its volume, both as shown: a week with an ex-date weighs its days in their own scales (summing the
    # raw 成交额 over raw shares and applying the last day's scale put a split week's 均价 far above its high)
    pv = pl.when(pl.col("raw_volume") > 0).then(pl.col("amount") / pl.col("raw_volume") * pl.col("scale") * pl.col("volume"))
    g = (day.with_columns(*key, pv.alias("_pv")).group_by("_y", "_p", maintain_order=True).agg(
        pl.col("date").last(), pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
        pl.col("close").last(), pl.col("volume").sum(), pl.col("amount").sum(), pl.col("raw_volume").sum(),
        pl.col("pre_close").first(), pl.col("limit_up").any(), pl.col("limit_down").any(),
        pl.col("scale").last(), pl.col("_pv").sum()))
    if calendar_open:
        cal = pl.DataFrame({"date": calendar_open}, schema={"date": pl.Date}).with_columns(key)
        last_open = cal.group_by("_y", "_p").agg(pl.col("date").max().alias("_last_open"))
        # complete only when the calendar reaches past the period's end: a calendar that stops with
        # the data (right after a backfill) cannot say whether trading days are still to come
        end = (pl.col("date") + pl.duration(days=7 - pl.col("date").dt.weekday()) if period == "week"
               else pl.col("date").dt.month_end())  # the period's Sunday / last calendar day
        g = g.join(last_open, on=["_y", "_p"], how="left").with_columns(
            (pl.col("_last_open").is_not_null() & (pl.col("date") >= pl.col("_last_open"))
             & (pl.lit(max(calendar_open)) >= end)).alias("complete"))
    else:
        g = g.with_columns(pl.lit(True).alias("complete"))
    return g.sort("date").drop([c for c in ("_y", "_p", "_last_open") if c in g.columns])


def with_indicators(bars: pl.DataFrame, period: str) -> pl.DataFrame:
    c, h, lo, v = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("volume")
    out = bars.with_columns(**MAS(c), **VOL_MAS(v), **MACD(c), **KDJ(c, h, lo), **BOLL(c))
    out = out.with_columns(
        (pl.col("ma60") > REF(pl.col("ma60"), 1)).alias("ma60_up"),
        pl.when(CROSS(pl.col("dif"), pl.col("dea")))
          .then(pl.when(pl.col("dif") < 0).then(pl.lit("below")).otherwise(pl.lit("above")))
          .alias("macd_cross"),                      # golden cross 水下 / 水上 (spec §8.6)
        pl.when(CROSS(pl.col("dif"), pl.col("dea")) & pl.col("ma60").is_not_null())
          .then(pl.when(pl.col("close") < pl.col("ma60")).then(pl.lit("below")).otherwise(pl.lit("above")))
          .alias("macd_cross_ma60"),                 # the same cross, by price against MA60 (§8.6 switch)
        VOL_MULT(v).alias("vol_mult"),
    )
    if period == "day":
        out = out.with_columns(
            AMPLITUDE(h, lo, 60).alias("amp60"), AMPLITUDE(h, lo, 120).alias("amp120"),
            NEW_HIGH(c, h, 120).alias("new_high_120"), NEW_HIGH(c, h, 250).alias("new_high_250"),
            GROUND_VOL(v, 20).alias("ground_vol"),
            **{f"year_{k}": e for k, e in YEAR_LEFT(c, h).items()})
    else:
        out = out.with_columns([pl.lit(None, dtype).alias(n) for n, dtype in
                                [("amp60", pl.Float64), ("amp120", pl.Float64), ("new_high_120", pl.Boolean),
                                 ("new_high_250", pl.Boolean), ("ground_vol", pl.Boolean), ("year_half", pl.Int64),
                                 ("year_year", pl.Int64), ("year_gap", pl.Int64)]])
    # 倍量 colouring (spec §8.6): limit-up with >= 2x volume; down day with <= half volume; 地量.
    down = pl.col("close") < pl.coalesce(pl.col("pre_close"), REF(pl.col("close"), 1))
    vol_class = (pl.when(pl.col("limit_up") & (pl.col("vol_mult") >= 2)).then(pl.lit("limit_double"))
                 .when(down & (pl.col("vol_mult") <= 0.5)).then(pl.lit("down_half"))
                 .when(pl.col("ground_vol").fill_null(False)).then(pl.lit("ground")))
    return out.with_columns(vol_class.alias("vol_class"))


def index_series(index_bars: pl.DataFrame, index_id: str, period: str = "day",
                 calendar_open: list[date] | None = None) -> Series:
    """Index bars (no adjustment, no limits) with the same indicator columns as a stock."""
    d = (index_bars.filter(pl.col("close").is_not_null()).sort("date").with_columns(pl.col("volume").alias("raw_volume"),
        pl.lit(None, pl.Float64).alias("pre_close"), pl.lit(None, pl.Float64).alias("up_limit"),
        pl.lit(None, pl.Float64).alias("down_limit"), pl.lit(False).alias("limit_up"),
        pl.lit(False).alias("limit_down"), pl.lit(1.0).alias("scale")))
    bars = aggregate(d, period, calendar_open)
    if period != "day":
        bars = bars.with_columns(pl.lit(None, pl.Float64).alias("up_limit"), pl.lit(None, pl.Float64).alias("down_limit"))
    return Series(index_id, period, "raw", True, _turnover(with_indicators(bars, period), "index").select(OUTPUT))


def to_epoch_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


OUTPUT = ["date", "open", "high", "low", "close", "volume", "amount", "pre_close", "up_limit", "down_limit",
          "limit_up", "limit_down", "complete", "scale", "ma5", "ma10", "ma20", "ma60", "ma60_up", "vol_ma5", "vol_ma10",
          "dif", "dea", "macd", "macd_cross", "macd_cross_ma60", "k", "d", "j", "boll_mid", "boll_up", "boll_low",
          "amp60", "amp120", "new_high_120", "new_high_250", "year_half", "year_year", "year_gap", "vol_mult", "vol_class",
          "turnover_rate", "vwap"]


def _turnover(bars: pl.DataFrame, period: str) -> pl.DataFrame:
    """换手率 (%) of a daily bar; none for a week or a month (筹码分布 spreads days) or where it is not known. And the
    bar's 均价 in the shown prices (成交额 ÷ 成交股数 × the basis' scale), which indicators read (VWAP)."""
    vwap = (pl.when(pl.col("volume") > 0).then(pl.col("_pv") / pl.col("volume")) if "_pv" in bars.columns   # a week, a month
            else pl.when(pl.col("raw_volume") > 0).then(pl.col("amount") / pl.col("raw_volume") * pl.col("scale"))).alias("vwap")
    if period == "day" and "turnover_rate" in bars.columns:
        return bars.with_columns(vwap)
    return bars.with_columns(pl.lit(None, pl.Float64).alias("turnover_rate"), vwap)


def build(daily: pl.DataFrame, listing_id: str, market: str, period: str = "day", basis: str = "qfq",
          calendar_open: list[date] | None = None) -> Series:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    day, adjusted = daily_series(daily, market, basis)
    bars = aggregate(day, period, calendar_open)
    if period != "day":
        bars = bars.with_columns(pl.lit(None, pl.Float64).alias("up_limit"), pl.lit(None, pl.Float64).alias("down_limit"))
    frame = _turnover(with_indicators(bars, period), period).select(OUTPUT)
    changes = day.filter(pl.col("scale") != pl.col("scale").shift(1).fill_null(-1.0)).select("date", "scale")
    scales = [(d.isoformat(), s) for d, s in changes.iter_rows()]
    return Series(listing_id, period, basis, adjusted, frame, scales)


def minute_bars(minutes: pl.DataFrame, sessions: list[tuple[time, time]], n: int) -> pl.DataFrame:
    """1-minute bars (at: the minute's start, exchange time) into n-minute bars inside each session — none spans lunch —
    each stamped `end` with its end, as A-share software shows them (the 5-minute bar of 9:30–9:35 is 09:35); `date`
    its day. open, high, low, close, volume and amount (when the minutes have it) of the minutes in it."""
    m = pl.col("at").dt.hour().cast(pl.Int32) * 60 + pl.col("at").dt.minute().cast(pl.Int32)
    first, last = pl.lit(None, pl.Int32), pl.lit(None, pl.Int32)
    for a, b in reversed(sessions):   # a minute starting at a session's close (its closing auction) is that session's last
        a_, b_ = a.hour * 60 + a.minute, b.hour * 60 + b.minute
        first = pl.when(m.is_between(a_, b_)).then(pl.lit(a_)).otherwise(first)
        last = pl.when(m.is_between(a_, b_)).then(pl.lit(b_)).otherwise(last)
    k = (m.clip(upper_bound=last - 1) - first) // n
    end = first + pl.min_horizontal((k + 1) * n, last - first)
    stamped = minutes.with_columns(pl.col("at").dt.date().alias("date"), end.alias("_end")).filter(pl.col("_end").is_not_null())
    stamped = stamped.with_columns((pl.col("date").cast(pl.Datetime) + pl.duration(minutes=pl.col("_end"))).alias("end"))
    agg = [pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(), pl.col("volume").sum()]
    if "amount" in minutes.columns:
        agg.append(pl.col("amount").sum())
    return stamped.sort("at").group_by("date", "end", maintain_order=True).agg(agg).sort("end")


def minute_series(minutes: pl.DataFrame, listing_id: str, market: str, period: str, basis: str, adjusted: bool,
                  scales: dict[date, float] | None = None) -> Series:
    """A stock's n-minute bars (MINUTE_PERIODS) with the chart's columns: prices in the shown basis by each day's scale
    (the daily chart's: a split between days does not jump), the indicators a day's bar has (those of a day alone —
    振幅、新高、地量、年线 — left out), its 均价 (VWAP). `date` is the bar's end in the exchange's clock."""
    b = minute_bars(minutes.filter(pl.col("listing_id") == listing_id), load_market(market).sessions, MINUTE_PERIODS[period])
    scales = scales or {}
    if "amount" not in b.columns:
        b = b.with_columns(pl.lit(None, pl.Float64).alias("amount"))
    b = (b.with_columns(pl.col("date").replace_strict(list(scales), list(scales.values()), default=1.0, return_dtype=pl.Float64).alias("scale"))
         .with_columns(*[pl.col(c) * pl.col("scale") for c in PRICE_COLS], pl.col("volume").alias("raw_volume"),
                       pl.lit(None, pl.Float64).alias("pre_close"), pl.lit(None, pl.Float64).alias("up_limit"),
                       pl.lit(None, pl.Float64).alias("down_limit"), pl.lit(False).alias("limit_up"), pl.lit(False).alias("limit_down"),
                       pl.lit(True).alias("complete"))
         .drop("date").rename({"end": "date"}))
    return Series(listing_id, period, basis, adjusted, _turnover(with_indicators(b, period), period).select(OUTPUT), [])


def page(series: Series, end: date | None = None, limit: int = 500) -> tuple[list[dict], bool]:
    """Rows with date <= end (all when None), newest `limit` of them, oldest first; and whether
    older rows exist (for the chart's left-edge loading). A minute series' times go out as the exchange's clock read as
    UTC (the chart's own zone) and as "YYYY-MM-DDTHH:MM"."""
    minute = isinstance(series.frame.schema["date"], pl.Datetime)
    day = pl.col("date").dt.date() if minute else pl.col("date")
    f = series.frame if end is None else series.frame.filter(day <= end)
    more = f.height > limit
    rows = f.tail(limit).with_columns(
        (pl.col("date").dt.replace_time_zone("UTC").dt.epoch("ms") if minute
         else pl.col("date").map_elements(to_epoch_ms, return_dtype=pl.Int64)).alias("t"),
        # JSON has no NaN/inf (Starlette refuses them): a non-finite value is unknown
        *[pl.when(pl.col(c).is_finite()).then(pl.col(c)).alias(c) for c, t in f.schema.items() if t == pl.Float64])
    return rows.with_columns(pl.col("date").dt.strftime("%Y-%m-%dT%H:%M") if minute else pl.col("date").cast(pl.Utf8)).to_dicts(), more
