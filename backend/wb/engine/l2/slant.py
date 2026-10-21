"""SLANT_BOX — a sloping box (spec §5.4). Two nearly parallel lines: the top through two
confirmed pivot highs, the bottom through two confirmed pivot lows; both slopes at least `min_slope`
per bar (flatter is a horizontal box) and within `slope_tol` of each other; the channel tight
(a slant box is pressed tight too; a wide channel is not one), closes inside it as for BOX.

A break is judged against the line's value on that bar, U(t) = a + b·t, never against an older
horizontal high (a slant box is bought where it breaks its sloping top). Lifecycle and events as for BOX. Rising slant
boxes make 变异 gold; falling ones are the 长颈鹿 "向下找线" pullback box."""
from dataclasses import dataclass

import numpy as np
import polars as pl
from numba import njit

from wb.engine.l2.box import (
    BREAK_DOWN,
    BREAK_UP,
    EVENT_NAMES,
    LEAVE_DOWN,
    LEAVE_UP,
    REASONS,
    RETURN,
    TOO_LONG,
)


@dataclass(frozen=True)
class SlantParams:
    min_len: int = 10
    max_len: int = 60
    tol: float = 0.015
    gap: int = 3
    min_slope: float = 0.002   # per bar, as a share of price (0.2%/bar ≈ 4% a month)
    slope_tol: float = 0.003   # the two lines' slopes may differ by this much per bar
    max_width: float = 0.15
    inside: float = 0.8
    spike_max: int = 2
    ret: int = 5


@njit(cache=True)
def _line(i0, p0, i1, p1, x):
    return p0 + (p1 - p0) * (x - i0) / (i1 - i0)


@njit(cache=True)
def _search(t, earliest, o, h, lo, c, p_idx, p_kind, p_end, prm):
    min_len, max_len, tol, gap, min_slope, slope_tol, max_width, inside_min, spike_max = prm
    w0 = max(earliest, t - max_len + 1)
    hi = np.empty(p_end, np.int64)
    lw = np.empty(p_end, np.int64)
    nh = nl = 0
    for q in range(p_end):
        if p_idx[q] < w0 or p_idx[q] > t:
            continue
        if p_kind[q] == 1:
            hi[nh] = p_idx[q]
            nh += 1
        else:
            lw[nl] = p_idx[q]
            nl += 1
    hi = np.sort(hi[:nh])
    lw = np.sort(lw[:nl])
    best = (0, 0, 0, 0, 0, 0.0, 0.0, 0)  # found, hi0, hi1, lo0, lo1, ratio, width, s
    b_touch, b_ratio, b_width, b_last = -1, -1.0, 1e18, -1
    for a in range(nh):
        for b in range(a + 1, nh):
            i0, i1 = hi[a], hi[b]
            if i1 - i0 < gap:
                continue
            rb = (h[i1] - h[i0]) / (i1 - i0) / h[i1]
            if abs(rb) < min_slope:
                continue
            for x in range(nl):
                for y in range(x + 1, nl):
                    k0, k1 = lw[x], lw[y]
                    if k1 - k0 < gap:
                        continue
                    rd = (lo[k1] - lo[k0]) / (k1 - k0) / lo[k1]
                    if abs(rd) < min_slope or rb * rd <= 0 or abs(rb - rd) > slope_tol:
                        continue
                    ut = _line(i0, h[i0], i1, h[i1], t)
                    lt = _line(k0, lo[k0], k1, lo[k1], t)
                    # a lower line at or below zero on bar t (a steep fall extrapolated) is no channel
                    if not (lt > 0) or not (ut > lt) or ut * (1 - tol) <= lt * (1 + tol) or (ut - lt) / lt > max_width:
                        continue
                    first = min(i0, k0)
                    s = first
                    length = t - s + 1
                    if length < min_len or length > max_len:
                        continue
                    inside = 0
                    run = 0
                    bad = False
                    touches = 0
                    last = max(i1, k1)
                    for z in range(nh):
                        j = hi[z]
                        if j >= s and abs(h[j] - _line(i0, h[i0], i1, h[i1], j)) <= tol * h[j]:
                            touches += 1
                    for z in range(nl):
                        j = lw[z]
                        if j >= s and abs(lo[j] - _line(k0, lo[k0], k1, lo[k1], j)) <= tol * lo[j]:
                            touches += 1
                    for yy in range(s, t + 1):
                        u = _line(i0, h[i0], i1, h[i1], yy)
                        d = _line(k0, lo[k0], k1, lo[k1], yy)
                        if d * (1 - tol) <= c[yy] <= u * (1 + tol):
                            if run > spike_max:
                                bad = True
                                break
                            run = 0
                            inside += 1
                        else:
                            run += 1
                    if bad or run > spike_max:
                        continue
                    ratio = inside / length
                    if ratio < inside_min:
                        continue
                    width = (ut - lt) / lt
                    better = False
                    if touches != b_touch:
                        better = touches > b_touch
                    elif ratio != b_ratio:
                        better = ratio > b_ratio
                    elif width != b_width:
                        better = width < b_width
                    else:
                        better = last > b_last
                    if better:
                        b_touch, b_ratio, b_width, b_last = touches, ratio, width, last
                        best = (1, i0, i1, k0, k1, ratio, width, s)
    return best


@njit(cache=True)
def scan(o, h, lo, c, p_idx, p_kind, p_known, prm, ret):
    n = len(c)
    tol = prm[2]
    max_len = prm[1]
    cap = n // 2 + 2
    out = np.full((cap, 9), -1.0)  # start, known, end, ended, reason, hi0, hi1, lo0, lo1
    nb = 0
    e_box = np.zeros(2 * n + 4, np.int64)
    e_bar = np.zeros(2 * n + 4, np.int64)
    e_kind = np.zeros(2 * n + 4, np.int8)
    ne = 0
    active = -1
    pending = 0
    pend_bar = -1
    earliest = 0
    q = 0
    for t in range(n):
        new_q = q
        while new_q < len(p_known) and p_known[new_q] <= t:
            new_q += 1
        confirmed = new_q > q
        q = new_q
        if active >= 0 and not np.isnan(c[t]):
            i0, i1, k0, k1 = int(out[active, 5]), int(out[active, 6]), int(out[active, 7]), int(out[active, 8])
            u = _line(i0, h[i0], i1, h[i1], t) * (1 + tol)
            d = _line(k0, lo[k0], k1, lo[k1], t) * (1 - tol)
            if pending != 0:
                if d <= c[t] <= u:
                    e_box[ne], e_bar[ne], e_kind[ne] = active, t, RETURN
                    ne += 1
                    pending = 0
                elif t - pend_bar >= ret:
                    e_box[ne], e_bar[ne], e_kind[ne] = active, t, LEAVE_UP if pending > 0 else LEAVE_DOWN
                    ne += 1
                    out[active, 2], out[active, 3], out[active, 4] = pend_bar, t, 1 if pending > 0 else 2
                    earliest = pend_bar
                    active = -1
                    pending = 0
            elif c[t] > u or c[t] < d:
                pending, pend_bar = (1 if c[t] > u else -1), t
                e_box[ne], e_bar[ne], e_kind[ne] = active, t, BREAK_UP if pending > 0 else BREAK_DOWN
                ne += 1
            elif t - out[active, 0] + 1 > max_len:
                e_box[ne], e_bar[ne], e_kind[ne] = active, t, TOO_LONG
                ne += 1
                out[active, 2], out[active, 3], out[active, 4] = t, t, 3
                earliest = t + 1
                active = -1
        if active < 0 and confirmed and nb < cap:
            r = _search(t, earliest, o, h, lo, c, p_idx, p_kind, q, prm)
            if r[0] == 1:
                out[nb, 0], out[nb, 1] = r[7], t
                out[nb, 5], out[nb, 6], out[nb, 7], out[nb, 8] = r[1], r[2], r[3], r[4]
                out[nb, 4] = 0
                active = nb
                nb += 1
                pending = 0
                i0, i1, k0, k1 = r[1], r[2], r[3], r[4]
                u = _line(i0, h[i0], i1, h[i1], t) * (1 + tol)
                d = _line(k0, lo[k0], k1, lo[k1], t) * (1 - tol)
                if c[t] > u or c[t] < d:
                    pending, pend_bar = (1 if c[t] > u else -1), t
                    e_box[ne], e_bar[ne], e_kind[ne] = active, t, BREAK_UP if pending > 0 else BREAK_DOWN
                    ne += 1
    return out[:nb], e_box[:ne], e_bar[:ne], e_kind[:ne]


def detect_slant(bars: pl.DataFrame, p: SlantParams | None = None, pivot_k: int = 2):
    """-> (slant boxes, events). Rows use the box table's columns where they apply: top / bottom are
    the lines' values on the confirming bar, top_slope / bottom_slope are per bar as a share of price."""
    from wb.engine.l2.pivots import fractal

    p = p or SlantParams()
    prm = (p.min_len, p.max_len, p.tol, p.gap, p.min_slope, p.slope_tol, p.max_width, p.inside, p.spike_max)
    rows, events = [], []
    for (lid,), g in bars.group_by("listing_id", maintain_order=True):
        dates = g["date"].to_list()
        o, h, lo, c = (g[x].cast(pl.Float64).fill_null(np.nan).to_numpy() for x in ("open", "high", "low", "close"))
        p_idx, p_kind, _, p_known = fractal(h, lo, pivot_k)
        out, eb, ebar, ek = scan(o, h, lo, c, p_idx, p_kind, p_known, prm, p.ret)
        for k, r in enumerate(out):
            s, kn, e, ed, reason, i0, i1, k0, k1 = (int(x) for x in r)
            # the lines' values on the confirming bar (plain arithmetic: calling the compiled _line from
            # Python here made numba build a throwaway ctypes type per box)
            top = h[i0] + (h[i1] - h[i0]) * (kn - i0) / (i1 - i0)
            bottom = lo[k0] + (lo[k1] - lo[k0]) * (kn - k0) / (k1 - k0)
            # the lines are given by their anchor pivots: a chart extends them bar by bar
            rows.append({"box_id": f"{lid}:slant:{k}", "listing_id": lid, "scale": "slant", "status": "established",
                         "start": dates[s], "known_at": dates[kn], "end": dates[e] if e >= 0 else None,
                         "ended_at": dates[ed] if ed >= 0 else None, "end_reason": REASONS[reason],
                         "top": top, "bottom": bottom, "width": top / bottom - 1,
                         "length": e - s + 1 if e >= 0 else None,
                         "top_slope": (h[i1] - h[i0]) / (i1 - i0) / h[i1], "bottom_slope": (lo[k1] - lo[k0]) / (k1 - k0) / lo[k1],
                         "top_a": dates[i0], "top_a_price": h[i0], "top_b": dates[i1], "top_b_price": h[i1],
                         "bottom_a": dates[k0], "bottom_a_price": lo[k0], "bottom_b": dates[k1], "bottom_b_price": lo[k1],
                         "slope": (h[i1] - h[i0]) / (i1 - i0) / h[i1]})
        for b, d, kind in zip(eb, ebar, ek, strict=True):
            events.append({"box_id": f"{lid}:slant:{b}", "date": dates[d], "event": EVENT_NAMES[int(kind)], "value": None,
                           "known_at": dates[d]})
    schema = {"box_id": pl.Utf8, "listing_id": pl.Utf8, "scale": pl.Utf8, "status": pl.Utf8, "start": pl.Date,
              "known_at": pl.Date, "end": pl.Date, "ended_at": pl.Date, "end_reason": pl.Utf8, "top": pl.Float64,
              "bottom": pl.Float64, "width": pl.Float64, "length": pl.Int64, "top_slope": pl.Float64, "bottom_slope": pl.Float64,
              "top_a": pl.Date, "top_a_price": pl.Float64, "top_b": pl.Date, "top_b_price": pl.Float64,
              "bottom_a": pl.Date, "bottom_a_price": pl.Float64, "bottom_b": pl.Date, "bottom_b_price": pl.Float64,
              "slope": pl.Float64}
    return (pl.DataFrame(rows, schema=schema),
            pl.DataFrame(events, schema={"box_id": pl.Utf8, "date": pl.Date, "event": pl.Utf8, "value": pl.Float64,
                                          "known_at": pl.Date}))
