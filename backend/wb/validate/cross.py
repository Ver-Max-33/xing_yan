"""越线买的日线近似 (design 2026-10-11 §6, §7): a stock in the rule's 潜伏 list at a day's close is bought on the market's
next trading day the first time the price reaches the box top the list waits on — the top 盯盘 alerts at (its break line
÷ (1 + tol)), read that evening (a slanted small box's a bar further along, `rise`). Daily bars do not say when in the
day: a high reaching the top is a cross, filled at the top, or at the open when it opened over it (no lower than the
price got to); an open more than `max_gap` over the top is not chased. An evening whose close is already at or over the
top has crossed it (盯盘 does not watch it the next day, design 2026-10-10 §2): no cross from it. A stock that does not
trade that day is not bought on a later day from that evening's list: the next evening's list is what counts. A line is
crossed once — its first day, whatever came of it (a limit up, a gap too wide, a close back in the box), as a structure
gives one signal; a new box is a new line. The plan's lines (stop0…, take0…) are the 潜伏 row's, read the evening
before."""
from datetime import date

import polars as pl

from wb.engine.l3.rule import Plan

EPS = 1e-9


def cross_signals(watch: pl.DataFrame, bars: pl.DataFrame, plan: Plan, calendar: list[date]) -> pl.DataFrame:
    """`watch`: 潜伏 rows (listing_id, date, edge — the box top —, rise — how much it rises a day —, line and per_bar — the
    break line and its rise —, line_of — the box the line is the top of —, the plan's lines …, in adjusted prices); `bars`:
    listing_id, date, open, high, close (raw), adj (raw → adjusted); `calendar`: the market's trading days. One row a
    cross: the 潜伏 row's columns with signal_date (the cross day), edge and line (that day's top and break line,
    adjusted), entry_price (raw), gap (the open over the top) and closed_above (that day's close at or over the break
    line: the box broken at the close)."""
    days = pl.DataFrame({"date": sorted(calendar)}).with_columns(pl.col("date").shift(-1).alias("signal_date"))
    top = pl.col("edge") / pl.col("adj")
    evening = bars.select("listing_id", "date", (pl.col("close") * pl.col("adj")).alias("_ec"))
    out = (watch.join(evening, on=["listing_id", "date"], how="left")
           .filter(pl.col("_ec").is_null() | (pl.col("_ec") < pl.col("edge") * (1 - EPS)))   # crossed already: not watched
           .join(days, on="date").drop_nulls("signal_date")
           .join(bars.select("listing_id", pl.col("date").alias("signal_date"), pl.col("open").alias("_o"),
                             pl.col("high").alias("_h"), pl.col("close").alias("_c"), "adj"), on=["listing_id", "signal_date"])
           .with_columns(pl.col("edge") + pl.col("rise").fill_null(0.0), pl.col("line") + pl.col("per_bar").fill_null(0.0))
           .filter(pl.col("_h") >= top * (1 - EPS))
           .sort("listing_id", "signal_date").unique(["listing_id", "line_of"], keep="first", maintain_order=True)
           .with_columns(pl.max_horizontal("_o", top).alias("entry_price"), (pl.col("_o") / top - 1).alias("gap"),
                         (pl.col("_c") >= pl.col("line") / pl.col("adj") * (1 - EPS)).alias("closed_above")))
    if plan.max_gap is not None:
        out = out.filter(pl.col("gap") <= plan.max_gap + EPS)
    return out.drop("_o", "_h", "_c", "_ec", "adj", "date").sort("signal_date", "listing_id")
