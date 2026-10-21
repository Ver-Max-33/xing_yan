"""通达信 functions an indicator commonly uses (指标库, design 2026-10-10 §3.4): arithmetic, AVEDEV, VALUEWHEN,
BARSLASTCOUNT, FILTER, CODE, and the recursive SAR and SUPERTREND. Like the rest, each reads one security's rows in
date order (the compiler adds `.over("listing_id")`); the recursive ones run a small compiled loop per security."""
import numpy as np
import polars as pl
from numba import njit

from wb.engine.l1.functions.base import _IDX
from wb.engine.l1.spec import B, F, N, S, register, same
from wb.engine.l1.units import U

# ---- arithmetic (element-wise) -------------------------------------------------------------------


@register("POW", [S("X"), S("Y")], U.NUMBER, "X 的 Y 次方；负数开非整数次方为未知", "基础", scope="el")
def _pow(ctx, x, y):
    v = x.cast(pl.Float64).pow(y)
    return pl.when(v.is_nan() | v.is_infinite()).then(None).otherwise(v)


@register("SQRT", [S("X")], U.NUMBER, "平方根；X < 0 为未知", "基础", scope="el")
def _sqrt(ctx, x):
    return pl.when(x >= 0).then(x.sqrt())


@register("ROUND", [S("X")], same, "四舍五入取整（.5 进位，负数同样远离 0）", "基础", scope="el")
def _round(ctx, x):
    return x.cast(pl.Float64).round(0, mode="half_away_from_zero")


@register("ROUND2", [S("X"), N("N", 2, min=0)], same, "四舍五入到 N 位小数", "基础", scope="el")
def _round2(ctx, x, n):
    return x.cast(pl.Float64).round(n, mode="half_away_from_zero")


register("SIGN", [S("X")], U.NUMBER, "符号：正数 1、负数 −1、0 为 0", "基础", scope="el")(lambda ctx, x: x.sign())
register("FLOOR", [S("X")], same, "向下取整（不大于 X 的最大整数）", "基础", scope="el")(lambda ctx, x: x.cast(pl.Float64).floor())
register("CEILING", [S("X")], same, "向上取整（不小于 X 的最小整数）", "基础", scope="el")(lambda ctx, x: x.cast(pl.Float64).ceil())


@register("INTPART", [S("X")], same, "取整数部分（向 0 取整）", "基础", scope="el")
def _intpart(ctx, x):
    x = x.cast(pl.Float64)
    return pl.when(x >= 0).then(x.floor()).otherwise(x.ceil())


@register("MOD", [S("A"), S("B")], U.NUMBER, "A 除以 B 的余数（B 为 0 时未知）", "基础", scope="el")
def _mod(ctx, a, b):
    return pl.when(b != 0).then(a - b * (a / b).floor())


register("DRAWNULL", [], U.NUMBER, "无效值：IF(条件, X, DRAWNULL) 在条件不成立的根上不画（也不参与之后的计算）", "基础", scope="el")(
    lambda ctx: pl.lit(None, pl.Float64))


@register("CODE", [], U.NUMBER, "股票代码的数字（600519、7203）；带字母的代码为未知", "基础", scope="el")
def _code(ctx):
    return ctx.column("listing_id").str.split(":").list.get(1, null_on_oob=True).cast(pl.Float64, strict=False)


# ---- over time -------------------------------------------------------------------------------------


def _avedev(n: int):
    def f(s: pl.Series) -> pl.Series:
        a = s.cast(pl.Float64).to_numpy().astype(float)   # nulls become NaN: a window with one is unknown
        out = np.full(a.shape, np.nan)
        if len(a) >= n:
            w = np.lib.stride_tricks.sliding_window_view(a, n)
            out[n - 1:] = np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1)
        return pl.Series(s.name, out).fill_nan(None)
    return f


@register("AVEDEV", [S("X"), N("N")], same, "N 根内的平均绝对偏差：Σ|X − 均值| ÷ N（CCI 用它）", "基础")
def _avedev_fn(ctx, x, n):
    return x.map_batches(_avedev(n), return_dtype=pl.Float64)


@register("VALUEWHEN", [B("COND"), S("X")], lambda u: u[0], "条件成立时取 X 的值，不成立时沿用上一次的值；第一次成立之前未知", "基础")
def _valuewhen(ctx, cond, x):
    return pl.when(cond.fill_null(False)).then(x).forward_fill()


@register("BARSLASTCOUNT", [B("COND")], U.COUNT, "到这一根为止条件连续成立了几根（这一根不成立为 0；不知道是否成立为未知）", "基础")
def _barslastcount(ctx, cond):
    c = cond.fill_null(False)
    last_not = pl.when(~c).then(_IDX).forward_fill().fill_null(-1)
    return pl.when(cond.is_null()).then(None).when(c).then(_IDX - last_not).otherwise(0)


@njit(cache=True)
def _filter_kernel(c, n):
    out = np.zeros(c.shape[0], dtype=np.float64)
    last = -n - 1
    for i in range(c.shape[0]):
        if np.isnan(c[i]):
            out[i] = np.nan
        elif c[i] > 0 and i - last > n:
            out[i] = 1.0
            last = i
    return out


def _filter(n: int):
    def f(s: pl.Series) -> pl.Series:
        a = s.cast(pl.Float64).to_numpy().astype(float)
        return pl.Series(s.name, _filter_kernel(a, n)).fill_nan(None).cast(pl.Boolean)
    return f


@register("FILTER", [B("COND"), N("N")], U.BOOL, "信号过滤：条件成立以后，之后 N 根里再成立的不算（通达信的 FILTER）", "基础")
def _filter_fn(ctx, cond, n):
    return cond.cast(pl.Float64).map_batches(_filter(n), return_dtype=pl.Boolean)


# ---- recursive indicators (TradingView's published algorithms) --------------------------------------


@njit(cache=True)
def _sar_kernel(h, lo, c, first, start, inc, mx):
    """TradingView's ta.sar, begun at bar `first` (its direction from that bar's close against the one before)."""
    n = h.shape[0]
    out = np.full(n, np.nan)
    if n <= first or first < 1:
        return out
    result = 0.0
    maxmin = 0.0
    acc = start
    below = True
    for i in range(first, n):
        if np.isnan(h[i]) or np.isnan(lo[i]) or np.isnan(c[i]):
            return out   # a bar without prices: the recursion cannot go on (unknown from here)
        first_bar = False
        if i == first:
            if c[i] > c[i - 1]:
                below, maxmin, result = True, h[i], lo[i - 1]
            else:
                below, maxmin, result = False, lo[i], h[i - 1]
            first_bar = True
            acc = start
        result = result + acc * (maxmin - result)
        if below:
            if result > lo[i]:
                first_bar, below = True, False
                result = max(h[i], maxmin)
                maxmin, acc = lo[i], start
        else:
            if result < h[i]:
                first_bar, below = True, True
                result = min(lo[i], maxmin)
                maxmin, acc = h[i], start
        if not first_bar:
            if below:
                if h[i] > maxmin:
                    maxmin, acc = h[i], min(acc + inc, mx)
            else:
                if lo[i] < maxmin:
                    maxmin, acc = lo[i], min(acc + inc, mx)
        if below:
            result = min(result, lo[i - 1])
            if i > first:
                result = min(result, lo[i - 2])
        else:
            result = max(result, h[i - 1])
            if i > first:
                result = max(result, h[i - 2])
        out[i] = result
    return out


def _arrays(s: pl.Series, *names):
    return [s.struct.field(k).cast(pl.Float64).to_numpy().astype(float) for k in names]


def _sar(n: int, step: float, mx: float):
    def f(s: pl.Series) -> pl.Series:
        h, lo, c = _arrays(s, "h", "l", "c")
        return pl.Series(s.name, _sar_kernel(h, lo, c, n, step / 100, step / 100, mx / 100)).fill_nan(None)
    return f


@register("SAR", [N("N", 10), F("STEP", 2.0), F("MAX", 20.0)], U.PRICE,
          "抛物线转向：从第 N 根开始，加速因子从 STEP% 起每创新高（低）加 STEP%，最多 MAX%（TradingView ta.sar 的算法）", "指标")
def _sar_fn(ctx, n, step, mx):
    if step <= 0 or mx < step:
        raise ValueError("STEP 要大于 0，MAX 不能小于 STEP")
    hlc = pl.struct(h=ctx.field("H"), l=ctx.field("L"), c=ctx.field("C"))
    return hlc.map_batches(_sar(n, step, mx), return_dtype=pl.Float64)


@njit(cache=True)
def _supertrend_kernel(h, lo, c, n, factor):
    """TradingView's ta.supertrend(factor, n): bands hl2 ± factor × ATR (Wilder's RMA of the true range), each band
    holding until the close breaks it. dir: 1 up (the line under the price), −1 down."""
    m = h.shape[0]
    line = np.full(m, np.nan)
    dirs = np.full(m, np.nan)
    atr = np.nan
    trs = 0.0
    k = 0
    up_b = np.nan
    lo_b = np.nan
    st = np.nan
    for i in range(m):
        if np.isnan(h[i]) or np.isnan(lo[i]) or np.isnan(c[i]):
            return line, dirs
        tr = h[i] - lo[i] if i == 0 else max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        prev_atr = atr
        if k < n:
            trs += tr
            k += 1
            if k == n:
                atr = trs / n   # Wilder's RMA starts from the simple mean of the first n
        else:
            atr = (atr * (n - 1) + tr) / n
        if np.isnan(atr):
            continue
        src = (h[i] + lo[i]) / 2
        upper = src + factor * atr
        lower = src - factor * atr
        prev_lower, prev_upper = lo_b, up_b
        if not np.isnan(prev_lower) and not (lower > prev_lower or c[i - 1] < prev_lower):
            lower = prev_lower
        if not np.isnan(prev_upper) and not (upper < prev_upper or c[i - 1] > prev_upper):
            upper = prev_upper
        if np.isnan(prev_atr):
            d = 1.0                                  # TradingView: down (its 1) at the first bar with an ATR
        elif st == prev_upper:
            d = -1.0 if c[i] > upper else 1.0
        else:
            d = 1.0 if c[i] < lower else -1.0
        st = lower if d == -1.0 else upper
        lo_b, up_b = lower, upper
        line[i] = st
        dirs[i] = -d                                 # ours: 1 up, −1 down
    return line, dirs


def _supertrend(n: int, factor: float, part: str):
    def f(s: pl.Series) -> pl.Series:
        h, lo, c = _arrays(s, "h", "l", "c")
        line, d = _supertrend_kernel(h, lo, c, n, factor)
        return pl.Series(s.name, line if part == "line" else d).fill_nan(None)
    return f


@register("SUPERTREND", [N("N", 10), F("MULT", 3.0)], U.PRICE,
          "超级趋势：(H+L)/2 ± MULT × ATR(N)（Wilder 平滑）的上下轨，收盘突破才换边；LINE 是线，DIR 是方向（1 向上、−1 向下）"
          "（TradingView ta.supertrend 的算法）", "指标", outputs=(("LINE", U.PRICE), ("DIR", U.NUMBER)))
def _supertrend_fn(ctx, n, mult):
    if mult <= 0:
        raise ValueError("MULT 要大于 0")
    hlc = pl.struct(h=ctx.field("H"), l=ctx.field("L"), c=ctx.field("C"))
    return {k: hlc.map_batches(_supertrend(n, mult, k.lower()), return_dtype=pl.Float64) for k in ("LINE", "DIR")}
