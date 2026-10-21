"""Adjustment-factor arithmetic (spec §4.4).

We store raw prices and raw share volume. Two cumulative factors make history comparable:
  adj_factor    hfq_price  = raw_price  * adj_factor
  share_factor  hfq_volume = raw_volume / share_factor
Both start at 1.0 on the first known event-free day and only change on ex-dates, so storing
them never rewrites earlier values (backward adjustment, spec §4.4).

Where each market's factors come from:
  CN  adj_factor   = Tushare `adj_factor` (already cumulative, includes cash dividends)
      share_factor = product of (1 + bonus/transfer shares per share) from Tushare `dividend`
  JP  adj_factor = share_factor = product of split ratios (J-Quants AdjFactor, or Yahoo splits)
"""
import polars as pl


def _cumulative(actions: pl.DataFrame) -> pl.DataFrame:
    """corp_actions -> (listing_id, date, cum) with cum = product of ratios up to that ex_date."""
    return (
        actions.select("listing_id", "ex_date", "ratio")
        .group_by("listing_id", "ex_date")
        .agg(pl.col("ratio").product())
        .sort("listing_id", "ex_date")
        .with_columns(pl.col("ratio").cum_prod().over("listing_id").alias("cum"))
        .select("listing_id", pl.col("ex_date").alias("date"), "cum")
        .sort("date")
    )


def apply_factors(bars: pl.DataFrame, actions: pl.DataFrame, *, set_price: bool) -> pl.DataFrame:
    """Fill share_factor (and adj_factor when set_price) from the full corp_actions history.

    `actions` must hold every known event of the listings in `bars`, not just new ones.
    Rows on or after an ex_date get that event's ratio.
    """
    cum = _cumulative(actions)
    out = (
        bars.sort("date")
        .join_asof(cum, on="date", by="listing_id", strategy="backward", check_sortedness=False)
        .with_columns(pl.col("cum").fill_null(1.0))
    )
    cols = [pl.col("cum").alias("share_factor")]
    if set_price:
        cols.append(pl.col("cum").alias("adj_factor"))
    return out.with_columns(cols).drop("cum").select(bars.columns).sort("listing_id", "date")


def yahoo_to_raw(bars: pl.DataFrame, splits: pl.DataFrame) -> pl.DataFrame:
    """Undo Yahoo's split adjustment for one ticker.

    Yahoo's Open/High/Low/Close are divided, and Volume multiplied, by every split that happened
    *after* the bar (verified on 7203.T around the 1:5 split of 2021-09-29). `splits` has columns
    (ex_date, ratio) and must cover every split from the first bar up to the fetch date.
    """
    if splits.is_empty():
        return bars
    s = splits.sort("ex_date")
    total = s["ratio"].product()
    cum = s.with_columns(pl.col("ratio").cum_prod().alias("cum")).select(
        pl.col("ex_date").alias("date"), "cum"
    )
    after = pl.lit(total) / pl.col("cum")
    return (
        bars.sort("date")
        .join_asof(cum, on="date", strategy="backward")
        .with_columns(pl.col("cum").fill_null(1.0))
        .with_columns(
            [(pl.col(c) * after).alias(c) for c in ("open", "high", "low", "close")]
            + [(pl.col("volume") / after).alias("volume")]
        )
        .drop("cum")
    )


def hfq(bars: pl.DataFrame) -> pl.DataFrame:
    """Backward-adjusted OHLC and volume, for checks and later computation."""
    return bars.with_columns(
        [(pl.col(c) * pl.col("adj_factor")).alias(f"{c}_hfq") for c in ("open", "high", "low", "close")]
        + [(pl.col("volume") / pl.col("share_factor")).alias("volume_hfq")]
    )
