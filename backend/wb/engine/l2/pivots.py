"""PIVOTS: confirmed turning points (spec §5.4). Every pivot carries `known_at`, the first bar on
which it is certain, so nothing downstream can use it earlier (spec §5.8).

  fractal  a high that is the highest of the k bars on each side; known k bars after it.
           Ties: strictly above the left side, at least as high as the right side, so a flat top
           of equal highs yields one pivot (its first bar).
  pct      online zigzag: a high is confirmed when price falls `threshold` (e.g. 0.05 = 5%) below
           it; known on that bar. Never revised afterwards.
  atr      the same with the threshold = m × ATR(n) at the confirming bar.

One security per call; arrays are that security's bars in date order."""
import numpy as np
from numba import njit

HIGH, LOW = 1, -1


@njit(cache=True)
def fractal(high: np.ndarray, low: np.ndarray, k: int):
    """-> (idx, kind, price, known_at) arrays sorted by known_at, then idx."""
    n = len(high)
    out_i = np.empty(2 * n, np.int64)
    out_k = np.empty(2 * n, np.int8)
    out_p = np.empty(2 * n, np.float64)
    m = 0
    for i in range(k, n - k):
        h, lo = high[i], low[i]
        is_h = not np.isnan(h)
        is_l = not np.isnan(lo)
        for j in range(i - k, i + k + 1):
            if j == i:
                continue
            if is_h and (np.isnan(high[j]) or (j < i and high[j] >= h) or (j > i and high[j] > h)):
                is_h = False
            if is_l and (np.isnan(low[j]) or (j < i and low[j] <= lo) or (j > i and low[j] < lo)):
                is_l = False
        if is_h:
            out_i[m], out_k[m], out_p[m] = i, HIGH, h
            m += 1
        if is_l:
            out_i[m], out_k[m], out_p[m] = i, LOW, lo
            m += 1
    known = out_i[:m] + k
    return out_i[:m].copy(), out_k[:m].copy(), out_p[:m].copy(), known


@njit(cache=True)
def zigzag(high: np.ndarray, low: np.ndarray, thresh: np.ndarray):
    """Online zigzag with a per-bar threshold (a fraction of price). A high is confirmed on the first
    bar whose low is `thresh` below the running high since the last confirmed low, and vice versa.
    -> (idx, kind, price, known_at)."""
    n = len(high)
    out_i = np.empty(n, np.int64)
    out_k = np.empty(n, np.int8)
    out_p = np.empty(n, np.float64)
    out_known = np.empty(n, np.int64)
    m = 0
    trend = 0  # 0 = undecided, 1 = rising (tracking a high), -1 = falling (tracking a low)
    hi_i = lo_i = -1
    hi_p = -np.inf
    lo_p = np.inf
    for t in range(n):
        if np.isnan(high[t]) or np.isnan(low[t]) or np.isnan(thresh[t]):
            continue
        if high[t] > hi_p:
            hi_p, hi_i = high[t], t
        if low[t] < lo_p:
            lo_p, lo_i = low[t], t
        th = thresh[t]
        if trend >= 0 and hi_i >= 0 and low[t] <= hi_p * (1 - th) and hi_i < t:
            out_i[m], out_k[m], out_p[m], out_known[m] = hi_i, HIGH, hi_p, t
            m += 1
            trend = -1
            lo_p, lo_i = low[t], t
            hi_p, hi_i = -np.inf, -1
        elif trend <= 0 and lo_i >= 0 and high[t] >= lo_p * (1 + th) and lo_i < t:
            out_i[m], out_k[m], out_p[m], out_known[m] = lo_i, LOW, lo_p, t
            m += 1
            trend = 1
            hi_p, hi_i = high[t], t
            lo_p, lo_i = np.inf, -1
    return out_i[:m].copy(), out_k[:m].copy(), out_p[:m].copy(), out_known[:m].copy()


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """MA(true range, n) as a fraction of the close (null warm-up as NaN)."""
    prev = np.concatenate(([np.nan], close[:-1]))
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = np.nan
    out = np.full(len(close), np.nan)
    if len(close) > n:
        c = np.convolve(np.nan_to_num(tr), np.ones(n) / n, mode="valid")
        out[n - 1:] = c
        out[:n] = np.nan
    return out / close


def pivots(high, low, close=None, method: str = "fractal", k: int = 2, threshold: float = 0.05,
           atr_n: int = 14, atr_m: float = 2.0):
    high, low = np.asarray(high, float), np.asarray(low, float)
    if method == "fractal":
        return fractal(high, low, k)
    if method == "pct":
        return zigzag(high, low, np.full(len(high), threshold))
    if method == "atr":
        return zigzag(high, low, atr_m * atr(high, low, np.asarray(close, float), atr_n))
    raise ValueError(f"unknown pivot method {method}")
