"""筹码分布 as rule fields (spec §8.6): 东方财富's chips (the chart draws the same, frontend chart/cyq.ts) for every bar —
获利比例 at its close, 平均成本, 90% / 70% 集中度 — over the 210 bars up to it, in 150 price steps from their lowest low
to their highest high. The chips are each day's turnover laid on [low, high] as a triangle peaking at (O+H+L+C)/4 and
decayed by every later day's turnover: a day's share is its turnover × Π(1 − turnover) of the days after it, the same
sum as decaying day by day. Prices are the frame's (后复权), each day's chips worked out in that day's own prices (÷ its
factor), as on 东方财富's chart that day: a later dividend changes neither its 0.01 step floor nor how much a one-price
day weighs, so an earlier day's chips stay as they were, and the chart's panel (frontend, on the day's own prices too)
shows the same numbers in every basis. Computed once per snapshot."""

import numpy as np
import polars as pl
from numba import njit, prange

from wb.data.store import Snapshot, Store
from wb.files import move_into_place, tmp_for

LOOKBACK, STEPS = 210, 150
COLUMNS = ("chip_profit", "chip_cost", "chip_conc90", "chip_conc70")


@njit(cache=True)
def _cost(w, chip, lo, acc):
    s = 0.0
    for j in range(w.shape[0]):
        if s + w[j] > chip:
            return lo + j * acc
        s += w[j]
    return 0.0


@njit(cache=True, parallel=True)
def _kernel(starts, o, h, lo_, c, scale, t, out):
    for g in prange(starts.shape[0] - 1):
        a, b = starts[g], starts[g + 1]
        w = np.zeros(STEPS)
        for i in range(a, b):
            if np.isnan(c[i]) or np.isnan(scale[i]):
                continue
            # the window in the day's own prices (后复权 ÷ the day's factor), as 东方财富's chart had them that day: both
            # the 0.01 step floor and a one-price day's weight, (STEPS − 1)·t / 2 whatever the prices, depend on them
            f = scale[i]
            j0 = max(a, i - LOOKBACK + 1)
            hi, lo, known = -np.inf, np.inf, False
            for d in range(j0, i + 1):
                if not (np.isnan(h[d]) or np.isnan(lo_[d])):
                    hi = max(hi, h[d] / f)
                    lo = min(lo, lo_[d] / f)
                if not np.isnan(t[d]):
                    known = True
            if not known or hi < lo:
                continue
            acc = max(0.01, (hi - lo) / (STEPS - 1))
            w[:] = 0.0
            dec = 1.0
            for d in range(i, j0 - 1, -1):
                td = 0.0 if np.isnan(t[d]) else min(1.0, t[d] / 100)
                coef = td * dec
                dec *= 1 - td
                if coef <= 0 or np.isnan(o[d]) or np.isnan(h[d]) or np.isnan(lo_[d]) or np.isnan(c[d]):
                    continue
                hd, ld = h[d] / f, lo_[d] / f
                avg = (o[d] + c[d] + h[d] + lo_[d]) / 4 / f
                if hd == ld:   # a one-price day (一字板)
                    w[int(np.floor((avg - lo) / acc))] += (STEPS - 1) * coef / 2
                    continue
                g2 = 2 / (hd - ld)
                for j in range(int(np.ceil((ld - lo) / acc)), int(np.floor((hd - lo) / acc)) + 1):
                    p = lo + acc * j
                    if p <= avg:
                        w[j] += g2 * coef if abs(avg - ld) < 1e-8 else (p - ld) / (avg - ld) * g2 * coef
                    else:
                        w[j] += g2 * coef if abs(hd - avg) < 1e-8 else (hd - p) / (hd - avg) * g2 * coef
            total = w.sum()
            if total <= 0:
                continue
            below = 0.0
            for j in range(STEPS):
                if c[i] / f >= lo + j * acc:
                    below += w[j]
            out[i, 0] = below / total
            out[i, 1] = _cost(w, total * 0.5, lo, acc) * f   # back in 后复权
            for k, p in ((2, 0.9), (3, 0.7)):
                x, y = _cost(w, total * (1 - p) / 2, lo, acc), _cost(w, total * (1 + p) / 2, lo, acc)
                out[i, k] = (y - x) / (x + y) if x + y != 0 else 0.0


def chip_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """listing_id, date and the chip fields for a frame (listing_id, date, open, high, low, close in 后复权, raw_close,
    turnover_rate %) sorted by listing then date; unknown where no turnover is known in the 210 bars."""
    f = frame.select("listing_id", "date", "open", "high", "low", "close", "raw_close", "turnover_rate")
    arr = {k: f[k].cast(pl.Float64).fill_null(np.nan).to_numpy() for k in ("open", "high", "low", "close", "raw_close", "turnover_rate")}
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = arr["close"] / arr["raw_close"]   # the day's factor: 后复权 ÷ the exchange's price
    ids = f["listing_id"].to_numpy()
    starts = np.flatnonzero(np.r_[True, ids[1:] != ids[:-1], True]).astype(np.int64) if f.height else np.zeros(1, np.int64)
    out = np.full((f.height, 4), np.nan)
    if f.height:
        _kernel(starts, arr["open"], arr["high"], arr["low"], arr["close"], scale, arr["turnover_rate"], out)
    return f.select("listing_id", "date").with_columns(
        [pl.Series(name, out[:, k]).fill_nan(None) for k, name in enumerate(COLUMNS)])


def chip_rows(store: Store, market: str, profile: str, snap: Snapshot | None = None) -> pl.DataFrame:
    """The snapshot's chip fields for every bar, computed once and kept with its derived results."""
    from wb.engine.l1.frame import build_frame

    snap = snap or store.latest()
    path = store.derived / snap.id / "chips.parquet"
    if path.exists():
        return pl.read_parquet(path)
    f = build_frame(store, market, profile, {"open", "high", "low", "close", "raw_close", "turnover_rate"}, snap=snap)
    rows = chip_columns(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_for(path)
    rows.write_parquet(tmp)
    try:
        move_into_place(tmp, path)
    except OSError:   # Windows: the other writer's file is open; it holds the same rows
        tmp.unlink(missing_ok=True)
    return rows
