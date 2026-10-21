"""SWING, MACD_DIV and the left-side pressure (spec §5.4).

  SWING     the legs between alternating confirmed pivots (zigzag, so highs and lows alternate):
            direction, change, bars, slope per bar; known when the end pivot is confirmed.
  MACD_DIV  bull: two confirmed lows where the second is lower in price but higher in DIF; bear the
            mirror image; known when the second pivot is confirmed. (Divergences inside
            a box are not used as a box condition; they are a general template.)
  PRESSURE  at a date t: the nearest box above the close that has ended by t and ended within the
            look-back (its bottom, top, thickness, heavy down-volume inside), the space to it from the
            close (measured from that day's close), and the half-year / one-year highs before t (年内向左).
            Closer pressure matters more, so the nearest one is reported (of
            equally near ones the longest). Pressure is trapped supply: a box wholly above the close,
            or one the price fell out of (下破离箱) and is back inside — no space then (0: buying
            there, everything in that box is trapped supply). Boxes that hold the close but ended by length or upward
            are the current range, not pressure. No box above at all is +inf.
"""
from itertools import pairwise

import numpy as np
import polars as pl

from wb.engine.l2.pivots import zigzag


def swings(bars: pl.DataFrame, threshold: float = 0.05) -> pl.DataFrame:
    rows = []
    for (lid,), g in bars.group_by("listing_id", maintain_order=True):
        dates = g["date"].to_list()
        h, lo = (g[x].cast(pl.Float64).fill_null(np.nan).to_numpy() for x in ("high", "low"))
        idx, kind, price, known = zigzag(h, lo, np.full(len(h), threshold))
        for a in range(len(idx) - 1):
            i0, i1 = int(idx[a]), int(idx[a + 1])
            change = price[a + 1] / price[a] - 1
            rows.append({"listing_id": lid, "direction": "up" if kind[a] < 0 else "down", "start": dates[i0],
                         "end": dates[i1], "start_price": float(price[a]), "end_price": float(price[a + 1]),
                         "change": float(change), "bars": i1 - i0, "slope": float(change / max(i1 - i0, 1)),
                         "known_at": dates[int(known[a + 1])]})
    return pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "direction": pl.Utf8, "start": pl.Date, "end": pl.Date,
                                      "start_price": pl.Float64, "end_price": pl.Float64, "change": pl.Float64,
                                      "bars": pl.Int64, "slope": pl.Float64, "known_at": pl.Date})


def macd_divergences(bars: pl.DataFrame, pivots: pl.DataFrame) -> pl.DataFrame:
    """bars must carry `dif`; pivots: listing_id, idx, date, kind, price, known_at (from PIVOTS)."""
    rows = []
    by_listing = pivots.partition_by("listing_id", as_dict=True)
    for (lid,), g in bars.group_by("listing_id", maintain_order=True):
        dif = g["dif"].to_numpy()
        piv = by_listing.get((lid,))
        if piv is None:
            continue
        piv = piv.sort("known_at", "date")
        for kind, name in (("low", "bull"), ("high", "bear")):
            pk = piv.filter(pl.col("kind") == kind).to_dicts()
            for a, b in pairwise(pk):
                d0, d1 = dif[a["idx"]], dif[b["idx"]]
                if np.isnan(d0) or np.isnan(d1):
                    continue
                lower_price = b["price"] < a["price"] if kind == "low" else b["price"] > a["price"]
                higher_dif = d1 > d0 if kind == "low" else d1 < d0
                if lower_price and higher_dif:
                    rows.append({"listing_id": lid, "kind": name, "first": a["date"], "second": b["date"],
                                 "price_change": b["price"] / a["price"] - 1, "dif_change": float(d1 - d0),
                                 "known_at": b["known_at"]})
    return pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "kind": pl.Utf8, "first": pl.Date, "second": pl.Date,
                                      "price_change": pl.Float64, "dif_change": pl.Float64, "known_at": pl.Date})


def left_pressure(boxes: pl.DataFrame, bars: pl.DataFrame, at: pl.DataFrame, lookback: int = 500) -> pl.DataFrame:
    """One row per (listing_id, date) of `at` that has a bar — per (listing_id, date, price) when `at` gives a `price`:
    the pressure above that price instead of the day's close (a box's break line: its room before the break, when the
    close is still inside it). Unknown stays unknown: a blanked close gives no space, a blanked bar inside the pressure
    box no heavy-volume figure, and the highs need 120 / 250 bars before the day."""
    bars = bars.sort("listing_id", "date").with_columns(pl.int_range(pl.len()).over("listing_id").alias("i"))
    prev = pl.col("high").shift(1)
    bars = bars.with_columns(prev.rolling_max(120, min_samples=120).over("listing_id").alias("half_year_high"),
                             prev.rolling_max(250, min_samples=250).over("listing_id").alias("one_year_high"))
    by = ["price"] if "price" in at.columns else []
    key = ["listing_id", "date", *by]
    pts = at.select(*key).unique().join(
        bars.select("listing_id", "date", "i", "close", "half_year_high", "one_year_high"), on=["listing_id", "date"])
    if by:
        pts = pts.with_columns(pl.col("price").alias("close"))
    idx = bars.select("listing_id", "date", "i")
    ended = boxes.filter((pl.col("status") == "established") & pl.col("ended_at").is_not_null())
    if "superseded_at" not in ended.columns:
        ended = ended.with_columns(pl.lit(None, pl.Date).alias("superseded_at"))
    for c in ("start", "end", "ended_at", "superseded_at"):
        ended = ended.join(idx.rename({"date": c, "i": f"{c}_i"}), on=["listing_id", c], how="left")
    trapped = (pl.col("bottom") > pl.col("close")) | ((pl.col("end_reason") == "下破离箱") & (pl.col("top") > pl.col("close")))
    near = (pts.select(*key, "i", "close")
            .join(ended.select("listing_id", "box_id", "start_i", "end_i", "ended_at_i", "superseded_at_i", "end_reason", "top", "bottom"),
                  on="listing_id")
            # a box as it stood that day (spec §5.8): the version not yet redrawn nor taken in by an outer box — each version
            # carries the end, and the one standing when the box ended is superseded only if an outer box takes it in later
            .filter((pl.col("ended_at_i") <= pl.col("i")) & (pl.col("end_i") >= pl.col("i") - lookback) & trapped
                    & (pl.col("superseded_at_i").is_null() | (pl.col("superseded_at_i") > pl.col("i"))))
            .with_columns(pl.max_horizontal(pl.col("bottom") / pl.col("close") - 1, pl.lit(0.0)).alias("space"))
            .sort("space", "end_i", "start_i", "box_id", descending=[False, True, False, False])
            .unique(key, keep="first", maintain_order=True))
    inside = (near.select("listing_id", "box_id", "start_i", "end_i").unique()
              .join(bars.select("listing_id", "i", "open", "close", "volume"), on="listing_id")
              .filter(pl.col("i").is_between(pl.col("start_i"), pl.col("end_i"))))
    down, v = pl.col("close") < pl.col("open"), pl.col("volume")
    known = pl.all_horizontal(pl.col("open", "close", "volume").is_not_null()).all()
    stats = inside.group_by("listing_id", "box_id").agg(
        pl.len().cast(pl.Int64).alias("pressure_bars"),
        pl.when(known & down.any() & (v.mean() > 0)).then(v.filter(down).max() / v.mean()).alias("pressure_heavy_down"))
    out = (pts.join(near.select(*key, "box_id", "top", "bottom", "space"), on=key, how="left")
           .join(stats, on=["listing_id", "box_id"], how="left"))
    none_above = pl.col("box_id").is_null() & pl.col("close").is_not_null()
    return out.select(
        *key, "close", "half_year_high", "one_year_high",
        (pl.col("half_year_high") / pl.col("close") - 1).alias("space_half_year"),
        (pl.col("one_year_high") / pl.col("close") - 1).alias("space_one_year"),
        pl.col("box_id").alias("pressure_box_id"), pl.col("bottom").alias("pressure_bottom"),
        pl.col("top").alias("pressure_top"), "pressure_bars", "pressure_heavy_down",
        pl.when(none_above).then(float("inf")).otherwise(pl.col("space")).alias("space"))
