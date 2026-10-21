"""Box context and position (spec §5.4 属性). Computed only from bars and pivots known by
the box's start, so they are as known as the box itself (spec §5.8). Shape (平箱 / 曲箱) and 曲转平
live in the box state machine. The thresholds were not given: they are parameters, and the
measured numbers are kept so the user can calibrate them.

  context   上横 / 下横: the run into the box over the `lookback` bars before its start — did price
            come up from the lowest low more than it came down from the highest high? (A last
            confirmed swing would lag: in a box narrower than the swing threshold the run-in's top
            is not confirmed yet, and the swing before the run-in would decide.)
  position  低位 / 中继 / 高位 by the start's gain over the lowest low of the previous year and by
            the waves already run since that low: up legs of at least `wave` between pivots of a
            `swing` zigzag that set a higher high (legs inside a range are not an advance), confirmed
            by the start (the same box means different things low, mid-way and high; a pause after
            several waves is not 长颈鹿的腰)."""
from dataclasses import dataclass

import numpy as np
import polars as pl
from numba import njit

from wb.engine.l2.pivots import zigzag


@dataclass(frozen=True)
class AttrParams:
    lookback: int = 60      # bars before the start that decide 上横 / 下横
    year: int = 250         # bars for the one-year low (position)
    mid_gain: float = 0.5   # gain over the one-year low from which a box counts as 中继
    high_gain: float = 1.0  # ... and as 高位
    swing: float = 0.15     # zigzag threshold of the pivots that delimit waves
    wave: float = 0.30      # an up leg of at least this much is a wave (300 CN listings: 3 waves ≈ +64%,
                            # 4 ≈ +90% from the low; finer settings count the chop of one advance)
    mid_waves: int = 2      # waves since the low from which a box counts as 中继
    high_waves: int = 3     # ... and as 高位


SCHEMA = {"box_id": pl.Utf8, "context": pl.Utf8, "rise_before": pl.Float64, "fall_before": pl.Float64,
          "position": pl.Utf8, "gain_1y": pl.Float64, "waves": pl.Int64}


@njit(cache=True)
def _waves(lo, p_idx, p_kind, p_price, p_known, starts, year, wave):
    """Per start s: up legs >= wave that make a new high after the lowest low of [s − year, s],
    confirmed by s."""
    out = np.zeros(len(starts), np.int64)
    for b in range(len(starts)):
        s = starts[b]
        a = max(0, s - year)
        for y in range(a, s + 1):
            if lo[y] <= lo[a]:
                a = y
        n = 0
        top = -np.inf
        for q in range(1, len(p_idx)):
            if p_kind[q] == 1 and a < p_idx[q] < s and p_known[q] <= s and p_price[q] > top:
                if p_price[q] / p_price[q - 1] - 1 >= wave:
                    n += 1
                top = p_price[q]
        out[b] = n
    return out


def box_attrs(boxes: pl.DataFrame, bars: pl.DataFrame, p: AttrParams | None = None) -> pl.DataFrame:
    """One pass over the bars (trailing windows ending at each bar), joined on the boxes' start bar;
    waves per listing from its zigzag pivots."""
    p = p or AttrParams()
    lid = "listing_id"
    at = bars.select(lid, "close", pl.int_range(pl.len()).over(lid).alias("start_i"),
                     pl.col("low").rolling_min(p.lookback + 1, min_samples=1).over(lid).alias("_lo"),
                     pl.col("high").rolling_max(p.lookback + 1, min_samples=1).over(lid).alias("_hi"),
                     pl.col("low").rolling_min(p.year + 1, min_samples=1).over(lid).alias("_lo_y"))
    waves = []
    starts = boxes.select("box_id", lid, pl.col("start_i").cast(pl.Int64))
    by_listing = starts.partition_by(lid, as_dict=True)
    for (listing,), g in bars.group_by(lid, maintain_order=True):
        mine = by_listing.get((listing,))
        if mine is None:
            continue
        h, lo = (g[x].cast(pl.Float64).to_numpy() for x in ("high", "low"))
        idx, kind, price, known = zigzag(h, lo, np.full(len(h), p.swing))
        waves.append(mine.select("box_id", pl.Series("waves", _waves(lo, idx, kind, price, known,
                                                                      mine["start_i"].to_numpy(), p.year, p.wave))))
    j = starts.join(at, on=[lid, "start_i"], how="left")
    if waves:
        j = j.join(pl.concat(waves), on="box_id", how="left")
    else:
        j = j.with_columns(pl.lit(None, pl.Int64).alias("waves"))
    rise, fall = pl.col("close") / pl.col("_lo") - 1, 1 - pl.col("close") / pl.col("_hi")
    gain, w = pl.col("close") / pl.col("_lo_y") - 1, pl.col("waves")
    has, has_year = pl.col("start_i") >= 1, pl.col("start_i") >= p.year // 2  # most of a year to say where the low was
    out = j.select("box_id",
                   pl.when(has).then(pl.when(rise >= fall).then(pl.lit("上横")).otherwise(pl.lit("下横"))).alias("context"),
                   pl.when(has).then(rise).alias("rise_before"), pl.when(has).then(fall).alias("fall_before"),
                   pl.when(has_year).then(pl.when((gain >= p.high_gain) | (w >= p.high_waves)).then(pl.lit("高位"))
                                          .when((gain >= p.mid_gain) | (w >= p.mid_waves)).then(pl.lit("中继"))
                                          .otherwise(pl.lit("低位"))).alias("position"),
                   pl.when(has_year).then(gain).alias("gain_1y"), pl.when(has_year).then(w).alias("waves")).cast(SCHEMA)
    return boxes.join(out, on="box_id", how="left", maintain_order="left")
