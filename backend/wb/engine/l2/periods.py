"""Each period's structure settings (design 2026-10-12-structures-every-period: the same structures hold on every
period, only bigger or smaller): the day's algorithm on the period's own bars. Numbers were given for days only, so:
bar counts stay (a small box is 3–20 bars on any period); a price-relative setting is scaled by the period's typical bar
against a day's, a box at most 60% wide; the year is the period's bars in a year; on minute bars a band is never under
two ticks (narrower, one tick is a break) — and when two ticks are more, every price setting rises with the band, so a
minute box keeps the day's proportions (an edge moving a tick is no new version)."""
from datetime import date, datetime, timedelta

import polars as pl

from wb.engine.l1.functions.trend import MACD
from wb.registry import PRIMITIVES, autoload

# a bar's typical range against a day's — the median of (high − low) ÷ the close before, A shares, measured 2026-10-12
# on the user's data: days 3.40%, weeks 7.94%, months 17.78% (every stock, two years); the 722 stocks with kept minutes
# over 20 days against their own days (2.76%): 1 min 0.138%, 5 min 0.308%, 15 min 0.520%, 30 min 0.729%, 60 min 1.049%.
# Japanese stocks use the same ratios (no kept minutes to measure).
RANGE = {"day": 1.0, "week": 2.34, "month": 5.23, "m1": 0.050, "m5": 0.111, "m15": 0.188, "m30": 0.264, "m60": 0.380}
YEAR = {"day": 250, "week": 52, "month": 12, "m1": 250 * 240, "m5": 250 * 48, "m15": 250 * 16, "m30": 250 * 8, "m60": 250 * 4}
PRICE = {"BOX": ("tol", "touch_tol", "rev", "gapf", "swing", "wave", "short.max_width", "mid.max_width", "long.max_width"),
         "BOX_DERIVED": ("tol", "sky_near"),
         "SLANT_BOX": ("tol", "min_slope", "slope_tol", "max_width"),
         "SWING": ("min_change",),
         "PIVOTS": ("threshold",)}
WIDEST = 0.6
TICKS = 2
DAY_TOL = 0.015   # BOX's band on a day (Shared.tol): the setting the two ticks are held to


def minute(period: str) -> bool:
    return period.startswith("m") and period != "month"


def tick_of(market: str, price: float) -> float:
    """The smallest price step: A shares 0.01 yuan; the Tokyo exchange's table (its ordinary stocks') in yen."""
    if market == "CN":
        return 0.01
    for upto, step in ((3_000, 1), (5_000, 5), (30_000, 10), (50_000, 50), (300_000, 100), (500_000, 500)):
        if price <= upto:
            return step
    return 1_000


def settings(period: str, price: float | None = None, tick: float = 0.01) -> dict[str, dict]:
    """compute()'s `params` for `period` (a day: none, the defaults); `price`: a stock's typical price, for the minute
    bands' two-tick floor."""
    if period == "day":
        return {}
    autoload("wb.engine.l2")
    k = RANGE[period]
    if minute(period) and price:
        k = max(k, TICKS * tick / price / DAY_TOL)
    out = {}
    for name, keys in PRICE.items():
        default = {p.name: p.default for p in PRIMITIVES[name].params}
        out[name] = {key: min(default[key] * k, WIDEST) if key.endswith("max_width") else default[key] * k for key in keys}
    out["BOX"]["year"] = YEAR[period]
    return out


def bars_of(daily: pl.DataFrame, period: str) -> pl.DataFrame:
    """One listing's L2 daily bars (l2_bars: OHLCV backward-adjusted, limit_up_close, dif) as weekly or monthly bars —
    ISO weeks and calendar months as the chart groups them, each dated by its last trading day; dif the MACD's on their
    closes (MACD_DIV reads it); no 撞顶涨停 (a day's limit)."""
    key = ([pl.col("date").dt.iso_year().alias("_y"), pl.col("date").dt.week().alias("_p")] if period == "week"
           else [pl.col("date").dt.year().alias("_y"), pl.col("date").dt.month().alias("_p")])
    g = (daily.sort("date").with_columns(key).group_by("listing_id", "_y", "_p", maintain_order=True)
         .agg(pl.col("date").last(), pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
              pl.col("close").last(), pl.col("volume").sum())
         .drop("_y", "_p").sort("listing_id", "date"))
    return g.with_columns(pl.lit(False).alias("limit_up_close"), MACD(pl.col("close"))["dif"].alias("dif"))


def on_bars(frame: pl.DataFrame, listing_id: str) -> tuple[pl.DataFrame, list[date]]:
    """A chart's minute bars (`date` their end, a datetime) as L2 bars the primitives count — one made-up day per bar, as
    the 分时小箱 do — and those days, oldest first, to give each table's date back its bar."""
    days = [date(2000, 1, 1) + timedelta(days=k) for k in range(frame.height)]
    bars = frame.select("open", "high", "low", "close", "volume").with_columns(
        pl.lit(listing_id).alias("listing_id"), pl.Series("date", days, dtype=pl.Date),
        pl.lit(False).alias("limit_up_close"), MACD(pl.col("close"))["dif"].alias("dif"))
    return bars.select("listing_id", "date", "open", "high", "low", "close", "volume", "limit_up_close", "dif"), days


def restamp(v, back: dict):
    """Every made-up day in a structure (on_bars) back to its bar's own time (`back`)."""
    if isinstance(v, dict):
        return {k: restamp(x, back) for k, x in v.items()}
    if isinstance(v, list):
        return [restamp(x, back) for x in v]
    if isinstance(v, date) and not isinstance(v, datetime):
        return back.get(v)
    return v
