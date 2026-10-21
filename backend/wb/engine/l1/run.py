"""Evaluate a compiled formula program on a frame (spec §5.7: L1 over the whole market at once)."""
from datetime import date

import polars as pl

from wb.engine.l1.compiler import Program, TfProgram

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "amount": "sum",
        "raw_open": "first", "raw_high": "max", "raw_low": "min", "raw_close": "last", "raw_volume": "sum"}


def evaluate(frame: pl.DataFrame, program: Program, calendar_open: list[date] | None = None,
             keep: bool = False) -> pl.DataFrame:
    """listing_id, date and one column per program output (`keep`: and every column computed on the way). Each
    listing's rows must be in date order (windows read neighbouring rows of the same listing). A column the frame
    already has is not computed again: it is named by its formula (a cross-sectional rank computed on the whole
    market stays the whole market's on a part of it)."""
    if frame.height and not frame.select(
            (pl.col("date").diff().over("listing_id").dt.total_days().fill_null(1) > 0).all()).item():
        raise ValueError("公式的数据要按日期排序：每只股票的 K 线按日期先后排列（窗口读的是相邻的行）")
    df = frame
    for tf in program.tf:
        df = _join_tf(df, tf, calendar_open)
    lf = df.lazy()
    for step in program.steps:
        todo = {c: e for c, e in step.items() if not (c.startswith("l1_") and c in df.columns)}
        if todo:
            lf = lf.with_columns(**todo)
    if keep:
        return lf.collect()
    return lf.select("listing_id", "date",
                     *[pl.col(col).alias(name) for name, (col, _) in program.outputs.items()]).collect()


def period_bars(frame: pl.DataFrame, period: str, calendar_open: list[date]) -> pl.DataFrame:
    """Weekly ("W") or monthly ("M") bars per listing, dated by their last trading day, with
    `complete` = the exchange calendar has no trading day left in that period, and `visible` = the
    period's last exchange open day (a listing that did not trade that day still has a closed period)."""
    key = ([pl.col("date").dt.iso_year().alias("_y"), pl.col("date").dt.week().alias("_p")] if period == "W"
           else [pl.col("date").dt.year().alias("_y"), pl.col("date").dt.month().alias("_p")])
    cols = [c for c in _AGG if c in frame.columns]
    aggs = [getattr(pl.col(c).drop_nulls(), _AGG[c])().alias(c) if _AGG[c] in ("first", "last")
            else getattr(pl.col(c), _AGG[c])().alias(c) for c in cols]
    g = (frame.with_columns(key).group_by("listing_id", "_y", "_p")
         .agg(pl.col("date").max(), *aggs))
    cal = pl.DataFrame({"date": calendar_open}, schema={"date": pl.Date}).with_columns(key)
    last_open = cal.group_by("_y", "_p").agg(pl.col("date").max().alias("_last_open"))
    # complete only when the calendar reaches past the period's end (Sunday / month end): a calendar
    # that stops with the data cannot say whether trading days are still to come
    end = (pl.col("date") + pl.duration(days=7 - pl.col("date").dt.weekday()) if period == "W"
           else pl.col("date").dt.month_end())
    return (g.join(last_open, on=["_y", "_p"], how="left")
            .with_columns((pl.col("_last_open").is_not_null() & (pl.lit(max(calendar_open)) >= end)).alias("complete"),
                          pl.col("_last_open").alias("visible"))
            .drop("_y", "_p", "_last_open").sort("listing_id", "date"))


def _join_tf(df: pl.DataFrame, tf: TfProgram, calendar_open: list[date] | None) -> pl.DataFrame:
    if not calendar_open:
        raise ValueError("TF needs the exchange calendar to know which periods are complete")
    bars = period_bars(df, tf.period, calendar_open)
    res = evaluate(bars, tf.program).join(bars.select("listing_id", "date", "complete", "visible"),
                                          on=["listing_id", "date"])
    # a completed period's value is known from the exchange's last open day of that period; the running
    # period is not used
    done = (res.filter(pl.col("complete"))
            .select("listing_id", pl.col("visible").alias("date"), pl.col("v").alias(tf.column)).sort("date"))
    return (df.sort("date").join_asof(done, on="date", by="listing_id", strategy="backward", check_sortedness=False)
            .sort("listing_id", "date"))
