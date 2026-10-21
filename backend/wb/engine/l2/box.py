"""BOX — the box (spec §5.4; checked against the reference case set, its ledgers 2026-10-06 and 2026-10-07).

Edges are drawn where the trading was, not at the extremes (an edge is a line through the dense trading and the
repeated resistance or support, not through the highest or lowest price): count, at every price, how many of the box's bars traded there (low ≤ price ≤ high, a market profile's
time count); the box is the run of prices around the most-traded one that at least `dense` × as many bars traded at
(dips narrower than 1% of the price bridged). A price only one or two bars reached — a spike, a dip — falls outside (枭首斩
尾). On the reference drawings' own spans this puts both edges within 2% of theirs for ~55% of the big boxes and ~55% of
the small ones (the highest and lowest prices: ~1%); two drawings of one box there agree within 2% about two times in three.

Drawn top-down (先画大箱，再在大箱里分中箱、小箱). Three machines walk the bars of a security, the big one first:

  big (中箱 under `long.min_len` bars, 大箱 from then)
    find    on every bar without a pending break: the earliest start s (the outermost box) such that [s, t] is a box —
            s a turning point at an edge of the band of [s, t] that price came to from beyond the far edge within `runin`
            bars (the end of a run-in: the top after a rise, the bottom after a fall, 极限点); the band no wider than the
            scale allows; at least `inside` of the closes inside it; no run of closes outside longer than `stray_run`
            bars nor deeper than `stray_depth` × its height (a box does not reach back across a leg or a plunge); the
            top and the bottom reached and held at least min_top / min_bottom times, in turn at least `alternate`
            times (反复受阻、反复受撑); and no more than `spike_return` closes outside at t. s is at most `long.max_len`
            bars back and never strictly inside a box that ended (between its start and its break bar): a box found
            over the bars of ended ones holds them whole and replaces them from t, as it does a box standing inside it
            (they were drawn too narrow; one that left and came back late is 破箱回箱). Boxes of one level never
            overlap. Once found, a big box stands until it breaks, however long (three months, six months,
            even a year).
  inner (大箱里的中箱)
    find    while a 大箱 stands (no break pending on it): as the big machine, over windows starting after the 大箱's start,
            at most `rel` × its width — a tighter consolidation inside a long range (002478 in 2016-12: the last two and
            a half months, 12% wide, at the top of an 18-month box 34% wide). Always 中箱; it never overlaps another 中箱
            of this machine, and ends with its 大箱 (大箱结束: at that one's break bar when it left). Its breakouts did as
            well as the 大箱's on the A-share market 2016–2026 and far better than a 中箱 standing alone (ledger 2026-10-07).
  small (小箱)
    find    on every bar without an active small box: the longest window of short.min_len…short.max_len bars ending at
            t whose band (short.dense) is at most short.max_width wide and at most `rel` × the big box's width as known
            then, with `inside` of its closes inside and t's close inside (very small and flat).

  revise  every bar while a box stands and no break is pending: the band of [s, t]; an edge moving more than `rev`, or
          a big box turning 60 bars (中箱 → 大箱), is a new version of the box, known from that bar; the version before
          is `superseded_at` it. A revision never widens a box past its scale's max_width, nor leaves fewer than
          `inside` of the closes inside (unless the band before held fewer still). Rows are versions: a rule,
          the chart and the derived structures read the version known on their day (spec §5.8).
  events  spike (beyond the band, close back inside), break (a close beyond the band of width tol: 越线, not 贴线), 回箱
          (back inside within R bars, the box goes on), 离箱 (no return within R: the box ends at the break bar),
          撞顶涨停 (a limit-up close reaching the top band without closing beyond it), touches (a bar reaching an edge
          within touch_tol and held, `gap` bars apart), 曲转平, 超长 (a small box longer than short.max_len: ended)."""
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import numpy as np
import polars as pl
from numba import njit

# event kinds
SPIKE_UP, SPIKE_DOWN, BREAK_UP, BREAK_DOWN, RETURN, LEAVE_UP, LEAVE_DOWN, LIMIT_HIT_TOP, TOO_LONG, \
    TOUCH_TOP, TOUCH_BOTTOM, CURVE_TO_FLAT = range(1, 13)
EVENT_NAMES = {SPIKE_UP: "spike_up", SPIKE_DOWN: "spike_down", BREAK_UP: "break_up", BREAK_DOWN: "break_down",
               RETURN: "return", LEAVE_UP: "leave_up", LEAVE_DOWN: "leave_down", LIMIT_HIT_TOP: "limit_hit_top",
               TOO_LONG: "too_long", TOUCH_TOP: "touch_top", TOUCH_BOTTOM: "touch_bottom", CURVE_TO_FLAT: "curve_to_flat"}
SHAPES = {0: None, 1: "平箱", 2: "曲箱"}
REASONS = {0: None, 1: "上破离箱", 2: "下破离箱", 3: "超长", 4: "大箱结束"}


@dataclass(frozen=True)
class BoxParams:
    """One scale's own settings."""
    min_len: int
    max_len: int
    min_top: int
    min_bottom: int
    max_width: float
    ret: int                 # R: bars a break has to come back (回箱)
    dense: float             # the band: prices at least this share of the most-traded price's bars traded at


@dataclass(frozen=True)
class Shared:
    """Settings every scale shares."""
    tol: float = 0.015        # a break is a close this far beyond an edge (越线)
    touch_tol: float = 0.025  # a touch comes this near an edge: a hand-drawn line is a band (his two drawings of one box
                              # differ by up to 2.5%)
    inside: float = 0.8       # closes inside the band, at least
    gap: int = 3              # touches of one edge at least this many bars apart
    spike_return: int = 3     # a big box can be found with at most this many closes outside at t
    alternate: int = 2        # the top and the bottom reached in turn at least this often (反复受阻、反复受撑)
    rev: float = 0.01         # an edge moving more than this makes a new version
    runin: int = 20           # a big box's start: price came from beyond the far edge within this many bars
    stray_run: int = 14       # no run of closes outside a big box longer than this ...
    stray_depth: float = 1.2  # ... nor deeper than this many of its heights (his boxes: 90% within 14 bars and 1.1)
    rel: float = 0.6          # a small box at most this share of the big box's width (his: median 0.39, 90% 0.63)
    flat: float = 0.25        # 平箱: MA5 range inside the box at most this share of the height
    curve_k: int = 8          # 曲转平: the last k bars of a 曲箱 with MA5 that flat
    limit_hit: int = 0        # 撞顶涨停: 0 = a limit-up close reaching the top band and staying in it; 1 = also one that
                              # closes beyond it (then it is also the break)
    gapf: float = 0.01        # dips in the count narrower than this share of the price are bridged (a price setting: another
                              # period's is scaled with its bar, periods.py)


# defaults: no numbers were given for these; each was chosen on the tune two thirds of the reference drawings and
# checked once on the held-out third (ledger 2026-10-07)
SCALES = {
    # a small box: a few bars bunched together, very small and flat (the reference ones: 8 bars median, 6% wide)
    "short": BoxParams(min_len=3, max_len=20, min_top=0, min_bottom=0, max_width=0.12, ret=5, dense=0.5),
    # a big box under 60 bars is 中箱, from 60 on 大箱 (a long box: three to six months and more)
    "mid": BoxParams(min_len=15, max_len=59, min_top=2, min_bottom=2, max_width=0.35, ret=5, dense=0.3),
    # a long box gets longer to come back: a reference 破箱回箱 (惠威科技 2018) stays under a five-month box for six
    # closes before returning. max_len: a big box is found over at most this many bars; it is not ended by its length
    "long": BoxParams(min_len=60, max_len=250, min_top=2, min_bottom=2, max_width=0.35, ret=10, dense=0.3),
}
SHARED = Shared()
BIG_BINS = 200      # price steps of a big box's count (a 30% range: 0.15% a step; 100 moves an edge enough to miss a marked break)
OUTER_EVERY = 5     # a standing big box is checked for an outer one every this many bars
GAPF = SHARED.gapf  # a day's (band's default)


@njit(cache=True)
def band(lo, hi, a, b, dense, bins, gapf=0.01):
    """(L, U): around the most-traded price of bars a..b, the run of prices at least `dense` × as many bars traded at."""
    pmin, pmax = np.inf, -np.inf
    for y in range(a, b + 1):
        if not np.isnan(lo[y]):
            pmin = min(pmin, lo[y])
        if not np.isnan(hi[y]):
            pmax = max(pmax, hi[y])
    if not pmax > pmin:
        return pmin, pmax
    step = (pmax - pmin) / bins
    cnt = np.zeros(bins + 1)
    for y in range(a, b + 1):
        if np.isnan(lo[y]) or np.isnan(hi[y]):
            continue
        i0 = min(max(int((lo[y] - pmin) / step), 0), bins - 1)
        i1 = min(max(int((hi[y] - pmin) / step), 0), bins - 1)
        cnt[i0] += 1
        cnt[i1 + 1] -= 1
    k = np.cumsum(cnt)[:bins]
    p = int(np.argmax(k))
    thr = dense * k[p]
    g = max(1, int(round(gapf * (pmin + pmax) / 2 / step)))
    i = p
    while True:
        j = i - 1
        while j >= 0 and k[j] < thr and i - j <= g:
            j -= 1
        if j >= 0 and k[j] >= thr:
            i = j
        else:
            break
    low = i
    i = p
    while True:
        j = i + 1
        while j < bins and k[j] < thr and j - i <= g:
            j += 1
        if j < bins and k[j] >= thr:
            i = j
        else:
            break
    return pmin + low * step, pmin + (i + 1) * step


@njit(cache=True)
def _touches(h, lo, c, a, b, U, L, tol, ttol, gap):
    """(top touches, bottom touches, switches between them): bars reaching an edge within ttol and held there (the close
    stays inside), `gap` bars apart per side."""
    nt = nb = sw = 0
    lt = lb = -(1 << 40)
    prev = 0
    for y in range(a, b + 1):
        if np.isnan(c[y]):
            continue
        if h[y] >= U * (1 - ttol) and c[y] <= U * (1 + tol) and y - lt >= gap:
            nt += 1
            lt = y
            sw += prev == -1
            prev = 1
        elif lo[y] <= L * (1 + ttol) and c[y] >= L * (1 - tol) and y - lb >= gap:
            nb += 1
            lb = y
            sw += prev == 1
            prev = -1
    return nt, nb, sw


@njit(cache=True)
def _inside(c, a, b, U, L, tol):
    """(share of the closes inside the band, closes outside in a row at b)."""
    ins = n = run = 0
    for y in range(a, b + 1):
        if np.isnan(c[y]):
            continue
        n += 1
        if L * (1 - tol) <= c[y] <= U * (1 + tol):
            ins += 1
            run = 0
        else:
            run += 1
    return (ins / n if n else 0.0), run


@njit(cache=True)
def _strays(c, a, b, U, L, tol):
    """(the longest run of closes beyond the band, the deepest close beyond it in band heights)."""
    run = longest = 0
    deep = 0.0
    hgt = U - L
    for y in range(a, b + 1):
        if np.isnan(c[y]):
            continue
        if c[y] > U * (1 + tol) or c[y] < L * (1 - tol):
            run += 1
            longest = max(longest, run)
            deep = max(deep, (c[y] - U) / hgt if c[y] > U else (L - c[y]) / hgt)
        else:
            run = 0
    return longest, deep


@njit(cache=True)
def _vol_ratio(v, s, t):
    """Volume on bar t over the mean volume of [s, t) (破箱当天是否放量); NaN when unknown."""
    total = 0.0
    cnt = 0
    for y in range(s, t):
        if not np.isnan(v[y]):
            total += v[y]
            cnt += 1
    return v[t] / (total / cnt) if cnt > 0 and total > 0 else np.nan


@njit(cache=True)
def _shape(ma5, a, b, height, flat):
    """Range of the MA5 values computed only from closes in [a, b] (a run-in does not count) as a share of the height,
    and their troughs. (-1, -1) with fewer than 3 such values."""
    first = max(a + 4, 4)
    lo_v, hi_v = np.inf, -np.inf
    cnt = 0
    troughs = 0
    for y in range(first, b + 1):
        if np.isnan(ma5[y]):
            continue
        cnt += 1
        lo_v = min(lo_v, ma5[y])
        hi_v = max(hi_v, ma5[y])
        if y - 1 >= first and y + 1 <= b and ma5[y] < ma5[y - 1] and ma5[y] <= ma5[y + 1]:
            troughs += 1
    if cnt < 3 or height <= 0:
        return -1.0, -1
    return (hi_v - lo_v) / height, troughs


@njit(cache=True)
def _grow(a, need):
    """`a` with room for at least `need` rows (doubled)."""
    if len(a) >= need:
        return a
    out = np.zeros(max(need, 2 * len(a)), a.dtype)
    out[:len(a)] = a
    return out


@njit(cache=True)
def _scale_of(big, length, long_len):
    """0 short, 1 mid, 2 long."""
    if not big:
        return 0
    return 2 if length >= long_len else 1


@njit(cache=True)
def scan(o, h, lo, c, v, limit_close, ma5, big, prm, big_u, big_l, inner, cont_s, cont_lg, cont_pend, cont_left, state, keep):
    """One security, one machine (big: mid / long; else short; inner: the 中箱 inside a standing 大箱). prm: see _params.
    big_u, big_l: the big box's band as known at the end of the previous bar (NaN: none), for the small and inner machines;
    cont_s, cont_lg, cont_pend, cont_left: the big box's start, whether it is a 大箱 and has a break pending, and on the bar
    it left its break bar (−1 / False: none), each as the big machine returned them. Returns the versions, touches, events
    and the band, start, 大箱 flag, pending break and left break bar as known before each bar, then where the machine
    stands after the last bar (`keep`; else nothing). `state`: where it stood after the first bars (EMPTY: none) — the
    scan goes on from the next, the bars before as they were (the band, start … before each of them are not returned)."""
    (s_min, s_max, s_top, s_bot, s_width, s_ret, s_dense, m_min, m_top, m_bot, m_width, m_ret, m_dense,
     l_min, l_max, l_top, l_bot, l_width, l_ret, l_dense,
     tol, ttol, inside_min, gap_, spike_ret_, alternate_, rev, runin_, stray_run_, stray_depth, rel, flat, curve_k_, limit_hit_,
     gapf) = prm
    gap, spike_ret, alternate, runin, stray_run, curve_k, limit_hit = \
        int(gap_), int(spike_ret_), int(alternate_), int(runin_), int(stray_run_), int(curve_k_), int(limit_hit_)
    if big:
        min_len, max_len, long_len, bins = int(m_min), int(l_max), (1 << 40) if inner else int(l_min), BIG_BINS   # inner: 中箱
    else:
        min_len, max_len, long_len, bins = int(s_min), int(s_max), 1 << 40, 100
    (k_ints, k_lin, k_start, k_known, k_super, k_end, k_ended, k_reason, k_u, k_l, k_scale, k_ins, k_shape, k_ma5r, k_troughs,
     k_ever, k_ebar, k_eknown, k_ekind, k_eval, k_tver, k_tbar, k_tside, k_tknown, k_inside) = state
    t0, nv, ne, nt = len(k_inside), k_ints[8], k_ints[9], k_ints[10]
    n = len(c)
    cap = 2 * n + 8
    v_lin = np.zeros(cap, np.int64)
    v_start = np.zeros(cap, np.int64)
    v_known = np.zeros(cap, np.int64)
    v_super = np.full(cap, -1, np.int64)
    v_end = np.full(cap, -1, np.int64)
    v_ended = np.full(cap, -1, np.int64)
    v_reason = np.zeros(cap, np.int8)
    v_u = np.zeros(cap)
    v_l = np.zeros(cap)
    v_scale = np.zeros(cap, np.int8)
    v_ins = np.zeros(cap)
    v_shape = np.zeros(cap, np.int8)
    v_ma5r = np.full(cap, np.nan)
    v_troughs = np.full(cap, -1, np.int64)
    v_lin[:nv], v_start[:nv], v_known[:nv], v_super[:nv], v_end[:nv], v_ended[:nv] = k_lin, k_start, k_known, k_super, k_end, k_ended
    v_reason[:nv], v_u[:nv], v_l[:nv], v_scale[:nv], v_ins[:nv], v_shape[:nv] = k_reason, k_u, k_l, k_scale, k_ins, k_shape
    v_ma5r[:nv], v_troughs[:nv] = k_ma5r, k_troughs
    e_ver = np.zeros(max(4 * n + 16, ne), np.int64)
    e_bar = np.zeros(max(4 * n + 16, ne), np.int64)
    e_known = np.zeros(max(4 * n + 16, ne), np.int64)
    e_kind = np.zeros(max(4 * n + 16, ne), np.int8)
    e_val = np.full(max(4 * n + 16, ne), np.nan)
    e_ver[:ne], e_bar[:ne], e_known[:ne], e_kind[:ne], e_val[:ne] = k_ever, k_ebar, k_eknown, k_ekind, k_eval
    t_ver = np.zeros(max(2 * n + 8, nt), np.int64)
    t_bar = np.zeros(max(2 * n + 8, nt), np.int64)
    t_side = np.zeros(max(2 * n + 8, nt), np.int8)
    t_known = np.zeros(max(2 * n + 8, nt), np.int64)
    t_ver[:nt], t_bar[:nt], t_side[:nt], t_known[:nt] = k_tver, k_tbar, k_tside, k_tknown
    out_u = np.full(n, np.nan)
    out_l = np.full(n, np.nan)
    out_s = np.full(n, -1, np.int64)
    out_lg = np.zeros(n, np.bool_)
    out_pend = np.zeros(n, np.bool_)
    out_left = np.full(n, -1, np.int64)   # on the bar a box was seen to leave: its break bar
    room = 2 * (max_len if max_len < n else n) + 16   # rows one bar can add: a window's spikes and touches, then its own
    cur, lineage, pending, pend_bar, earliest = k_ints[0], k_ints[1], k_ints[2], k_ints[3], k_ints[4]
    inside_ended = np.zeros(n, np.bool_)   # bars strictly inside a big box that ended: no big box starts there
    inside_ended[:t0] = k_inside
    last_top, last_bot = k_ints[5], k_ints[6]
    flat_seen = k_ints[7] != 0
    for t in range(t0, n):
        if cur >= 0:
            out_u[t], out_l[t] = v_u[cur], v_l[cur]
            out_s[t], out_lg[t], out_pend[t] = v_start[cur], v_scale[cur] == 2, pending != 0
        if ne + room > len(e_ver):
            e_ver, e_bar, e_known = _grow(e_ver, ne + room), _grow(e_bar, ne + room), _grow(e_known, ne + room)
            e_kind = _grow(e_kind, ne + room)
            ev2 = np.full(len(e_ver), np.nan)
            ev2[:ne] = e_val[:ne]
            e_val = ev2
        if nt + room > len(t_ver):
            t_ver, t_bar, t_side, t_known = _grow(t_ver, nt + room), _grow(t_bar, nt + room), _grow(t_side, nt + room), _grow(t_known, nt + room)
        if np.isnan(c[t]) or np.isnan(h[t]) or np.isnan(lo[t]):
            continue
        new_version = False
        if inner and cur >= 0 and not (cont_lg[t] and cont_s[t] >= 0 and cont_s[t] < v_start[cur]):
            # its 大箱 is gone: the 中箱 ends with it — at that one's break bar when it left, so the box drawn after the
            # 大箱 does not run over it (a 大箱 replaced by an outer one still holds it)
            e_end = t - 1
            if t >= 1 and cont_left[t - 1] >= v_start[cur]:
                e_end = min(e_end, cont_left[t - 1])
            for k in range(nv):
                if v_lin[k] == v_lin[cur]:
                    v_end[k], v_ended[k], v_reason[k] = e_end, t, 4
            inside_ended[v_start[cur] + 1:e_end] = True
            earliest = e_end
            pending = 0
            cur = -1
        if cur >= 0:
            s = v_start[cur]
            U, L = v_u[cur], v_l[cur]
            top_b, bot_b = U * (1 + tol), L * (1 - tol)
            length = t - s + 1
            sc = _scale_of(big, length, long_len)
            ret = int(s_ret) if sc == 0 else (int(m_ret) if sc == 1 else int(l_ret))
            if pending != 0:
                if bot_b <= c[t] <= top_b:
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, RETURN
                    ne += 1
                    pending = 0
                elif t - pend_bar >= ret:
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, LEAVE_UP if pending > 0 else LEAVE_DOWN
                    ne += 1
                    for k in range(nv):
                        if v_lin[k] == v_lin[cur]:
                            v_end[k], v_ended[k], v_reason[k] = pend_bar, t, 1 if pending > 0 else 2
                    earliest = pend_bar
                    out_left[t] = pend_bar
                    if big:
                        inside_ended[v_start[cur] + 1:pend_bar] = True
                    pending = 0
                    cur = -1
            elif not big and length > max_len:   # a small box is a few bars; a big one stands until it breaks
                e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, TOO_LONG
                ne += 1
                for k in range(nv):
                    if v_lin[k] == v_lin[cur]:
                        v_end[k], v_ended[k], v_reason[k] = t, t, 3
                earliest = t + 1
                cur = -1
            elif c[t] > top_b or c[t] < bot_b:
                pending, pend_bar = (1 if c[t] > top_b else -1), t
                e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, BREAK_UP if pending > 0 else BREAK_DOWN
                e_val[ne] = _vol_ratio(v, s, t)
                ne += 1
                if limit_hit == 1 and pending > 0 and limit_close[t]:   # a limit-up through the top is 撞顶涨停 too
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, LIMIT_HIT_TOP
                    ne += 1
            else:
                if h[t] > top_b:   # the extreme left out: recorded, the edge does not move
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = cur, t, t, SPIKE_UP, h[t]
                    ne += 1
                if lo[t] < bot_b:
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = cur, t, t, SPIKE_DOWN, lo[t]
                    ne += 1
                if limit_close[t] and h[t] >= U * (1 - tol):
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = cur, t, t, LIMIT_HIT_TOP
                    ne += 1
                if h[t] >= U * (1 - ttol) and c[t] <= top_b and t - last_top >= gap:
                    t_ver[nt], t_bar[nt], t_side[nt], t_known[nt] = cur, t, 1, t
                    nt += 1
                    last_top = t
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = cur, t, t, TOUCH_TOP, h[t]
                    ne += 1
                elif lo[t] <= L * (1 + ttol) and c[t] >= bot_b and t - last_bot >= gap:
                    t_ver[nt], t_bar[nt], t_side[nt], t_known[nt] = cur, t, -1, t
                    nt += 1
                    last_bot = t
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = cur, t, t, TOUCH_BOTTOM, lo[t]
                    ne += 1
                if v_shape[cur] == 2 and not flat_seen and t - curve_k + 1 > v_known[cur]:
                    r_tail, _tr = _shape(ma5, t - curve_k + 1, t, U - L, flat)
                    if 0 <= r_tail <= flat:
                        e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = cur, t, t, CURVE_TO_FLAT, r_tail
                        ne += 1
                        flat_seen = True
                # revise: the band of everything so far
                dense = s_dense if sc == 0 else (m_dense if sc == 1 else l_dense)
                wmax = s_width if sc == 0 else (m_width if sc == 1 else l_width)
                if (sc == 0 or inner) and rel > 0 and not np.isnan(big_u[t]):   # inside a big box: narrower than it
                    wmax = min(wmax, rel * (big_u[t] / big_l[t] - 1))
                L2, U2 = band(lo, h, s, t, dense, bins, gapf)
                if U2 / L2 - 1 > wmax:   # never wider than the scale allows: it breaks out instead
                    U2, L2 = U, L
                elif (abs(U2 / U - 1) > rev or abs(L2 / L - 1) > rev) and \
                        _inside(c, s, t, U2, L2, tol)[0] < min(inside_min, _inside(c, s, t, U, L, tol)[0]):
                    U2, L2 = U, L        # nor so narrow most of its closes fall outside (a tight cluster became the most traded)
                if abs(U2 / U - 1) > rev or abs(L2 / L - 1) > rev or sc != v_scale[cur]:
                    k = nv
                    nv += 1
                    ins, _r = _inside(c, s, t, U2, L2, tol)
                    v_lin[k], v_start[k], v_known[k], v_u[k], v_l[k], v_scale[k], v_ins[k] = v_lin[cur], s, t, U2, L2, sc, ins
                    v_shape[k], v_ma5r[k], v_troughs[k] = v_shape[cur], v_ma5r[cur], v_troughs[cur]
                    rng, trg = _shape(ma5, s, t, U2 - L2, flat)
                    if rng >= 0:
                        v_shape[k] = 1 if rng <= flat else 2
                        v_ma5r[k], v_troughs[k] = rng, trg
                    v_super[cur] = t
                    cur = k
                    new_version = True
        # find: no box (big and small), or an outer one (big, no break pending)
        outer = big and cur >= 0 and pending == 0 and not new_version and (t - v_known[cur]) % OUTER_EVERY == 0
        if (cur < 0 or outer) and nv < cap - 2 and (not inner or (cont_lg[t] and cont_s[t] >= 0 and not cont_pend[t])):
            best = -1
            bU = bL = 0.0
            if big:
                lo_i, hi_i = max(0, t - max_len + 1), t - min_len + 1
                if inner:   # inside the standing 大箱: after its start
                    lo_i = max(lo_i, cont_s[t] + 1)
                if outer:
                    hi_i = v_start[cur] - 1
                for j in range(lo_i, hi_i + 1):
                    if best >= 0:
                        break
                    if inside_ended[j] or np.isnan(h[j]) or np.isnan(lo[j]) or j == 0 or np.isnan(h[j - 1]) or np.isnan(lo[j - 1]):
                        continue
                    top_c = h[j] >= h[j - 1] and h[j] >= h[j + 1]    # a turning point: its edge is decided by the band
                    bot_c = lo[j] <= lo[j - 1] and lo[j] <= lo[j + 1]
                    if not (top_c or bot_c):
                        continue
                    length = t - j + 1
                    sc = _scale_of(big, length, long_len)
                    dense = m_dense if sc == 1 else l_dense
                    L, U = band(lo, h, j, t, dense, bins, gapf)
                    if not (U > L) or U / L - 1 > (m_width if sc == 1 else l_width):
                        continue
                    if inner and U / L - 1 > rel * (big_u[t] / big_l[t] - 1):   # narrower than its 大箱
                        continue
                    for side in range(2):
                        if best >= 0:
                            break
                        if side == 0 and not (top_c and h[j] >= U * (1 - ttol)):
                            continue
                        if side == 1 and not (bot_c and lo[j] <= L * (1 + ttol)):
                            continue
                        far = False   # it came from beyond the far edge: the end of a run-in, not a swing inside
                        for y in range(max(0, j - runin), j):
                            if (side == 0 and lo[y] < L) or (side == 1 and h[y] > U):
                                far = True
                                break
                        if not far:
                            continue

                        ins, run = _inside(c, j, t, U, L, tol)
                        if ins < inside_min or run > spike_ret:
                            continue
                        longest, deep = _strays(c, j, t, U, L, tol)
                        if longest > stray_run or deep > stray_depth:
                            continue
                        ntop, nbot, sw = _touches(h, lo, c, j, t, U, L, tol, ttol, gap)
                        if ntop < (m_top if sc == 1 else l_top) or nbot < (m_bot if sc == 1 else l_bot) or sw < alternate:
                            continue
                        best, bU, bL = j, U, L
            else:
                wmax = s_width
                if rel > 0 and not np.isnan(big_u[t]):   # inside a big box: narrower than it (先画大箱，再在大箱里分小箱)
                    wmax = min(wmax, rel * (big_u[t] / big_l[t] - 1))
                for m in range(max_len, min_len - 1, -1):
                    j = t - m + 1
                    if j < earliest or j < 0:
                        continue
                    L, U = band(lo, h, j, t, s_dense, bins, gapf)
                    if not (U > L) or U / L - 1 > wmax:
                        continue
                    ins, run = _inside(c, j, t, U, L, tol)
                    if ins < inside_min or run > 0:
                        continue
                    ntop, nbot, _sw = _touches(h, lo, c, j, t, U, L, tol, ttol, 1)
                    if ntop < s_top or nbot < s_bot:
                        continue
                    best, bU, bL = j, U, L
                    break
            if best >= 0:
                if big:   # the outer box is the box from now on: the ones inside it — standing or ended (a late 回箱) — were
                    for k in range(nv):   # drawn too narrow
                        if v_super[k] < 0 and v_start[k] >= best:
                            v_super[k] = t
                k = nv
                nv += 1
                lineage += 1
                ins, _r = _inside(c, best, t, bU, bL, tol)
                sc = _scale_of(big, t - best + 1, long_len)
                v_lin[k], v_start[k], v_known[k], v_u[k], v_l[k], v_scale[k], v_ins[k] = lineage, best, t, bU, bL, sc, ins
                rng, trg = _shape(ma5, best, t, bU - bL, flat)
                if rng >= 0:
                    v_shape[k] = 1 if rng <= flat else 2
                    v_ma5r[k], v_troughs[k] = rng, trg
                cur = k
                pending = 0
                flat_seen = False
                last_top = last_bot = -(1 << 40)
                top_b, bot_b = bU * (1 + tol), bL * (1 - tol)
                gap_k = gap if big else 1
                for y in range(best, t + 1):   # the touches and the extremes left out on the way, known from t
                    if np.isnan(c[y]):
                        continue
                    if y < t or not (c[t] > top_b or c[t] < bot_b):
                        if h[y] > top_b and c[y] <= top_b:
                            e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = k, y, t, SPIKE_UP, h[y]
                            ne += 1
                        if lo[y] < bot_b and c[y] >= bot_b:
                            e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne], e_val[ne] = k, y, t, SPIKE_DOWN, lo[y]
                            ne += 1
                    if h[y] >= bU * (1 - ttol) and c[y] <= top_b and y - last_top >= gap_k:
                        t_ver[nt], t_bar[nt], t_side[nt], t_known[nt] = k, y, 1, t
                        nt += 1
                        last_top = y
                    elif lo[y] <= bL * (1 + ttol) and c[y] >= bot_b and y - last_bot >= gap_k:
                        t_ver[nt], t_bar[nt], t_side[nt], t_known[nt] = k, y, -1, t
                        nt += 1
                        last_bot = y
                if c[t] > top_b or c[t] < bot_b:   # the bar it was found on may already close beyond it
                    pending, pend_bar = (1 if c[t] > top_b else -1), t
                    e_ver[ne], e_bar[ne], e_known[ne], e_kind[ne] = k, t, t, BREAK_UP if pending > 0 else BREAK_DOWN
                    e_val[ne] = _vol_ratio(v, best, t)
                    ne += 1
    # where it stands: copies of what is in use, not of the room made
    m, kv, ke, kt = (n, nv, ne, nt) if keep else (0, 0, 0, 0)
    ints = np.array([cur, lineage, pending, pend_bar, earliest, last_top, last_bot, 1 if flat_seen else 0, nv, ne, nt], np.int64)
    kept = (ints, v_lin[:kv].copy(), v_start[:kv].copy(), v_known[:kv].copy(), v_super[:kv].copy(), v_end[:kv].copy(),
            v_ended[:kv].copy(), v_reason[:kv].copy(), v_u[:kv].copy(), v_l[:kv].copy(), v_scale[:kv].copy(), v_ins[:kv].copy(),
            v_shape[:kv].copy(), v_ma5r[:kv].copy(), v_troughs[:kv].copy(), e_ver[:ke].copy(), e_bar[:ke].copy(),
            e_known[:ke].copy(), e_kind[:ke].copy(), e_val[:ke].copy(), t_ver[:kt].copy(), t_bar[:kt].copy(),
            t_side[:kt].copy(), t_known[:kt].copy(), inside_ended[:m].copy())
    return (v_lin[:nv], v_start[:nv], v_known[:nv], v_super[:nv], v_end[:nv], v_ended[:nv], v_reason[:nv], v_u[:nv],
            v_l[:nv], v_scale[:nv], v_ins[:nv], v_shape[:nv], v_ma5r[:nv], v_troughs[:nv],
            t_ver[:nt], t_bar[:nt], t_side[:nt], t_known[:nt], e_ver[:ne], e_bar[:ne], e_kind[:ne], e_val[:ne], e_known[:ne],
            out_u, out_l, out_s, out_lg, out_pend, out_left, kept)


def _params(scales: dict[str, BoxParams], sh: Shared) -> tuple:
    s, m, lg = scales["short"], scales["mid"], scales["long"]
    return tuple(float(x) for x in (
        s.min_len, s.max_len, s.min_top, s.min_bottom, s.max_width, s.ret, s.dense,
        m.min_len, m.min_top, m.min_bottom, m.max_width, m.ret, m.dense,
        lg.min_len, lg.max_len, lg.min_top, lg.min_bottom, lg.max_width, lg.ret, lg.dense,
        sh.tol, sh.touch_tol, sh.inside, sh.gap, sh.spike_return, sh.alternate, sh.rev, sh.runin, sh.stray_run,
        sh.stray_depth, sh.rel, sh.flat, sh.curve_k, sh.limit_hit, sh.gapf))


SCALE_NAMES = ("short", "mid", "long")
MACHINES = (("big", True, False), ("inner", True, True), ("small", False, False))   # 先画大箱，再在大箱里分中箱、小箱
_I, _F, _B = np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int8)
# where a machine stands before the first bar: no box, nothing seen (scan's `state`)
EMPTY = (np.array([-1, 0, 0, -1, 0, -(1 << 40), -(1 << 40), 0, 0, 0, 0], np.int64), _I, _I, _I, _I, _I, _I, _B, _F, _F, _B, _F, _B,
         _F, _I, _I, _I, _I, _B, _F, _I, _I, _B, _I, np.zeros(0, np.bool_))
_KEPT: ContextVar[tuple[dict, set] | None] = ContextVar("box_scans_kept", default=None)


@contextmanager
def resumed(states: dict):
    """Inside, detect() keeps in `states` where each listing's machines stood after all its bars but the last, and goes
    on from there while those bars stay the same: 盯盘's polls, whose last bar is the day's forming one, scan that bar
    alone. A listing not seen inside is let go on the way out (what a poll keeps is its own listings')."""
    seen: set = set()
    token = _KEPT.set((states, seen))
    try:
        yield
    finally:
        _KEPT.reset(token)
        for k in [k for k in states if k not in seen]:
            del states[k]


def _digest(k: int, *arrays: np.ndarray) -> bytes:
    """The first k values of every array, as bytes hashed: the bars a kept scan stood on, the same or not."""
    h = hashlib.blake2b(digest_size=16)
    for a in arrays:
        h.update(np.ascontiguousarray(a[:k]))
    return h.digest()


def _scans(o, h, lo, c, v, lim, ma5, prm, start: dict, left: int, keep: bool) -> dict:
    """The machines over one listing's bars, the big one first, each going on from `start` (where it stood after the
    bars before; EMPTY: from the first bar) — `left`: the big one's left break bar on the last of those. -> name: scan's."""
    n = len(c)
    bu = bl = np.full(n, np.nan)
    bs, blg, bp, bleft = np.full(n, -1, np.int64), np.zeros(n, np.bool_), np.zeros(n, np.bool_), np.full(n, -1, np.int64)
    out = {}
    for name, is_big, is_inner in MACHINES:
        out[name] = r = scan(o, h, lo, c, v, lim, ma5, is_big, prm, bu, bl, is_inner, bs, blg, bp, bleft, start[name], keep)
        if name == "big":
            bu, bl, bs, blg, bp, bleft = r[23:29]
            t0 = len(start[name][-1])
            if t0:   # the inner machine reads the bar before its first as the big one left it
                bleft[t0 - 1] = left
    return out


def detect(bars: pl.DataFrame, scales: dict[str, BoxParams] | None = None, shared: Shared | None = None):
    """bars: one or more listings (listing_id, date, open, high, low, close, volume, limit_up_close), hfq prices, sorted
    by listing then date. -> (boxes, touches, events) keyed by box_id: one row per version of a box (`lineage` names the
    box, `superseded_at` the bar its next version was known). Inside `resumed`, a listing's bars but the last are
    scanned once and kept (the scan goes on bar by bar: going on from where it stood is the same scan)."""
    scales, shared = {**SCALES, **(scales or {})}, shared or SHARED
    prm = _params(scales, shared)
    kept = _KEPT.get()
    blocks, days, off = [], [], 0
    for (lid,), g in bars.group_by("listing_id", maintain_order=True):
        days.append(g["date"].to_numpy())
        o, h, lo, c, v = (g[x].cast(pl.Float64).fill_null(np.nan).to_numpy() for x in ("open", "high", "low", "close", "volume"))
        lim = g["limit_up_close"].fill_null(False).to_numpy() if "limit_up_close" in g.columns else np.zeros(len(g), bool)
        ma5 = np.full(len(c), np.nan)
        if len(c) >= 5:
            ma5[4:] = np.convolve(c, np.ones(5) / 5, mode="valid")
        start, left = {name: EMPTY for name, _, _ in MACHINES}, -1
        if kept is not None and len(c) > 1:
            states, seen = kept
            key, before = (lid, prm), _digest(len(c) - 1, o, h, lo, c, v, lim)
            if key not in states or states[key][0] != before:   # the bars before the last: scanned once
                was = _scans(*(x[:-1] for x in (o, h, lo, c, v, lim, ma5)), prm, start, left, True)
                states[key] = (before, {name: r[29] for name, r in was.items()}, int(was["big"][28][-1]))
            seen.add(key)
            _, start, left = states[key]
        for name, r in _scans(o, h, lo, c, v, lim, ma5, prm, start, left, False).items():
            blocks.append((lid, name, off, r[:23], h, lo))
        off += len(c)
    return _tables(blocks, days)


_OUT = ("lin", "s", "kn", "sup", "e", "ed", "rs", "u", "lw", "sc", "ins", "shp", "m5r", "trg", "tv", "tb", "ts", "tk", "ev", "eb", "ek",
        "eval", "eknown")   # what scan returns first, in its order


def _named(codes: np.ndarray, names: dict) -> pl.Series:
    return pl.Series(codes, dtype=pl.Int64).replace_strict(names, return_dtype=pl.Utf8)


def _tables(blocks: list[tuple], days: list[np.ndarray]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """(boxes, touches, events) of every listing's machines in their order, built at once — `blocks`: (listing, machine,
    where its dates start in `days`, scan's, high, low). A bar number is the listing's date (−1: none); a version's box id
    is listing:machine:lineage.version (its number in its machine)."""
    if not blocks:
        return pl.DataFrame(schema=BOX_SCHEMA), pl.DataFrame(schema=TOUCH_SCHEMA), pl.DataFrame(schema=EVENT_SCHEMA)
    col: dict[str, list] = {k: [] for k in (*_OUT, "pre", "lid", "vi", "voff", "toff", "eoff", "price")}
    v0 = 0
    for lid, name, off, r, h, lo in blocks:
        for k, x in zip(_OUT, r, strict=True):
            col[k].append(x)
        nv, nt, ne = len(r[0]), len(r[14]), len(r[18])
        col["tv"][-1], col["ev"][-1] = r[14] + v0, r[18] + v0   # a version by its row among every listing's
        col["pre"].append(np.full(nv, f"{lid}:{name}:", dtype=object))
        col["lid"].append(np.full(nv, lid, dtype=object))
        col["vi"].append(np.arange(nv))
        col["voff"].append(np.full(nv, off))
        col["toff"].append(np.full(nt, off))
        col["eoff"].append(np.full(ne, off))
        col["price"].append(np.where(r[16] == 1, h[r[15]], lo[r[15]]))
        v0 += nv
    c = {k: np.concatenate(x) for k, x in col.items()}
    every, none = np.concatenate(days), np.datetime64("NaT", "D")

    def date(k: str, off: str) -> pl.Series:
        return pl.Series(np.where(c[k] >= 0, every[np.where(c[k] >= 0, c[k] + c[off], 0)], none))

    b = pl.DataFrame({
        "_pre": pl.Series(c["pre"], dtype=pl.Utf8), "_lin": pl.Series(c["lin"]).cast(pl.Utf8), "_vi": c["vi"], "_s": c["s"], "_e": c["e"],
        "_trg": c["trg"], "listing_id": pl.Series(c["lid"], dtype=pl.Utf8), "scale": _named(c["sc"], dict(enumerate(SCALE_NAMES))),
        "start": date("s", "voff"), "known_at": date("kn", "voff"), "end": date("e", "voff"), "ended_at": date("ed", "voff"),
        "end_reason": _named(c["rs"], REASONS), "top": c["u"], "bottom": c["lw"], "width": (c["u"] - c["lw"]) / c["lw"],
        "touches_top": np.bincount(c["tv"][c["ts"] == 1], minlength=v0)[:v0].astype(np.int64),
        "touches_bottom": np.bincount(c["tv"][c["ts"] == -1], minlength=v0)[:v0].astype(np.int64), "inside_ratio": c["ins"],
        "superseded_at": date("sup", "voff"), "shape": _named(c["shp"], SHAPES),
        "ma5_range": pl.Series(c["m5r"], dtype=pl.Float64).fill_nan(None), "known_i": c["kn"]})
    boxes = b.with_columns(
        pl.concat_str("_pre", "_lin", pl.lit("."), pl.col("_vi").cast(pl.Utf8)).alias("box_id"), pl.lit("established").alias("status"),
        pl.when(pl.col("_e") >= 0).then(pl.col("_e") - pl.col("_s") + 1).alias("length"), pl.lit(None, pl.Date).alias("excluded_before"),
        pl.when(pl.col("_trg") >= 0).then(pl.col("_trg")).alias("ma5_troughs"), pl.col("_s").alias("start_i"),
        pl.when(pl.col("_e") >= 0).then(pl.col("_e")).alias("end_i"), pl.concat_str("_pre", "_lin").alias("lineage"),
    ).select(list(BOX_SCHEMA)).cast(BOX_SCHEMA)
    ids = boxes["box_id"]
    touches = pl.DataFrame({"box_id": ids.gather(c["tv"]), "date": date("tb", "toff"), "side": np.where(c["ts"] == 1, "top", "bottom"),
                            "price": c["price"], "known_at": date("tk", "toff")}).select(list(TOUCH_SCHEMA)).cast(TOUCH_SCHEMA)
    events = pl.DataFrame({"box_id": ids.gather(c["ev"]), "date": date("eb", "eoff"), "event": _named(c["ek"], EVENT_NAMES),
                           "value": c["eval"], "known_at": date("eknown", "eoff")}).select(list(EVENT_SCHEMA)).cast(EVENT_SCHEMA)
    return boxes, touches, events


BOX_SCHEMA = {"box_id": pl.Utf8, "listing_id": pl.Utf8, "scale": pl.Utf8, "status": pl.Utf8, "start": pl.Date,
              "known_at": pl.Date, "end": pl.Date, "ended_at": pl.Date, "end_reason": pl.Utf8, "top": pl.Float64,
              "bottom": pl.Float64, "width": pl.Float64, "length": pl.Int64, "touches_top": pl.Int64,
              "touches_bottom": pl.Int64, "inside_ratio": pl.Float64, "excluded_before": pl.Date,
              "superseded_at": pl.Date, "shape": pl.Utf8, "ma5_range": pl.Float64, "ma5_troughs": pl.Int64,
              "start_i": pl.Int64, "known_i": pl.Int64, "end_i": pl.Int64, "lineage": pl.Utf8}
TOUCH_SCHEMA = {"box_id": pl.Utf8, "date": pl.Date, "side": pl.Utf8, "price": pl.Float64, "known_at": pl.Date}
# known_at: the bar an event is known on — its own bar, except a spike inside the window a box was found over, which
# is known once the box is
EVENT_SCHEMA = {"box_id": pl.Utf8, "date": pl.Date, "event": pl.Utf8, "value": pl.Float64, "known_at": pl.Date}
