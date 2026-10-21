"""Basic time-series functions with 通达信 semantics (spec §5.3).

Every function takes and returns Polars expressions for ONE security's rows sorted by date
(callers add `.over("listing_id")` when evaluating many securities at once).

  MA, HHV, LLV, STD, SUM  need a full window: null until N valid values exist (spec §5.3:
                          "窗口内有效数据不足 N 个时结果为 unknown"); 通达信 shows partial windows,
                          so only values after the warm-up are comparable.
  EMA, SMA                recursive from the first value, exactly as 通达信:
                          EMA: Y = (2X + (N-1)Y') / (N+1);  SMA(X,N,M): Y = (M X + (N-M) Y') / N
  REF(X, N)               N >= 0 only (a negative REF would look into the future, spec §5.8).
"""
import polars as pl


def MA(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_mean(window_size=n, min_samples=n)


def SUM(x: pl.Expr, n: int) -> pl.Expr:
    """N = 0: from the first bar on (通达信)."""
    return x.cum_sum() if n == 0 else x.rolling_sum(window_size=n, min_samples=n)


def STD(x: pl.Expr, n: int) -> pl.Expr:
    """Sample standard deviation (n-1), as 通达信's STD."""
    return x.rolling_std(window_size=n, min_samples=n, ddof=1)


def HHV(x: pl.Expr, n: int) -> pl.Expr:
    return x.cum_max() if n == 0 else x.rolling_max(window_size=n, min_samples=n)


def LLV(x: pl.Expr, n: int) -> pl.Expr:
    return x.cum_min() if n == 0 else x.rolling_min(window_size=n, min_samples=n)


def EMA(x: pl.Expr, n: int) -> pl.Expr:
    return x.ewm_mean(alpha=2 / (n + 1), adjust=False)


def SMA(x: pl.Expr, n: int, m: int) -> pl.Expr:
    if m > n:
        raise ValueError("M 不能大于 N")
    return x.ewm_mean(alpha=m / n, adjust=False)


def REF(x: pl.Expr, n: int) -> pl.Expr:
    if n < 0:
        raise ValueError("REF with a negative offset reads the future")
    return x.shift(n)


# values this close (relative) are equal: floating-point sums round a bit or two apart (~1e-16), a price tick is ~1e-4.
# Without it an exact tie (MA5 = MA20 to the cent) falls on either side at random — a cross a day early
EQ = 1e-9


def tie(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return (a - b).abs() <= EQ * (a.abs() + b.abs())


def rel(x: pl.Expr, y: pl.Expr) -> pl.Expr:
    """x ÷ y − 1, exactly 0 when the two are a rounding apart: a flat line's change is 0, not ±1e-16 at random."""
    eq = tie(x, y)   # the change first: the result keeps x's name; unknown stays unknown
    return pl.when(~eq).then(x / y - 1).when(eq).then(0.0)


def above(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """a > b with a tie (within EQ) not above — the formula's `>`, for the comparisons functions make inside: two means
    equal to the cent come out an ulp apart in floating point, and which side they fall on must not be rounding's."""
    return (a > b) & ~tie(a, b)


def CROSS(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """True on the bar where `a` moves from <= b to > b (a tie, within EQ, is not above)."""
    return above(a, b) & ~above(a.shift(1), b.shift(1))


# ---- formula registrations (spec §5.3 基础) --------------------------------------------------
import numpy as np  # noqa: E402

from wb.engine.l1.spec import B, N, S, register, same  # noqa: E402
from wb.engine.l1.units import U  # noqa: E402

_IDX = pl.int_range(pl.len())  # bar number within one security (the compiler adds .over)


register("REF", [S("X"), N("N", min=0)], same, "N 根之前的值（N ≥ 0）", "基础")(lambda ctx, x, n: REF(x, n))
register("MA", [S("X"), N("N")], same, "简单移动平均；不足 N 根为未知", "基础")(lambda ctx, x, n: MA(x, n))
register("EMA", [S("X"), N("N")], same, "指数移动平均（通达信递推，从第一根起算）", "基础")(lambda ctx, x, n: EMA(x, n))
register("SMA", [S("X"), N("N"), N("M")], same, "通达信 SMA：Y = (M·X + (N−M)·Y') / N", "基础")(
    lambda ctx, x, n, m: SMA(x, n, m))
register("SUM", [S("X"), N("N", min=0)], same, "N 根求和（N 写 0 是从第一根累加到现在，通达信的写法）", "基础")(
    lambda ctx, x, n: SUM(x, n))
register("STD", [S("X"), N("N", min=2)], same, "N 根样本标准差", "基础")(lambda ctx, x, n: STD(x, n))
register("HHV", [S("X"), N("N", min=0)], same, "N 根内最高值（含当根；N 写 0 是从第一根起）", "基础")(lambda ctx, x, n: HHV(x, n))
register("LLV", [S("X"), N("N", min=0)], same, "N 根内最低值（含当根；N 写 0 是从第一根起）", "基础")(lambda ctx, x, n: LLV(x, n))
register("CROSS", [S("A"), S("B")], U.BOOL, "A 在这一根上穿 B", "基础")(lambda ctx, a, b: CROSS(a, b))


@register("WMA", [S("X"), N("N")], same, "加权移动平均（越近权重越大，通达信 WMA）", "基础")
def _wma(ctx, x, n):
    # Σ (j − (t − n))·x_j over the window ending at t; Polars' weighted rolling_sum panics on nulls
    t = _IDX.cast(pl.Float64)
    return ((t * x).rolling_sum(n, min_samples=n) - (t - n) * x.rolling_sum(n, min_samples=n)) / (n * (n + 1) / 2)


@register("COUNT", [B("COND"), N("N", min=0)], U.COUNT, "N 根内条件成立的次数（N 写 0 是从第一根起）", "基础")
def _count(ctx, cond, n):
    return cond.cast(pl.Int32).cum_sum() if n == 0 else cond.cast(pl.Int32).rolling_sum(window_size=n, min_samples=n)


def _window(cond, n):
    """(true bars, known bars) in the N-bar window; null while fewer than N bars exist."""
    count = lambda e: e.cast(pl.Int32).rolling_sum(window_size=n, min_samples=n)  # noqa: E731
    return count(cond.fill_null(False)), count(cond.is_not_null())


@register("EVERY", [B("COND"), N("N")], U.BOOL, "N 根内每一根都成立（有一根不成立就不成立，哪怕别的根未知）", "基础")
def _every(ctx, cond, n):
    true, known = _window(cond, n)
    return pl.when(known - true > 0).then(False).when(known == n).then(true == n)


@register("EXIST", [B("COND"), N("N")], U.BOOL, "N 根内至少一根成立（有一根成立就成立，哪怕别的根未知）", "基础")
def _exist(ctx, cond, n):
    true, known = _window(cond, n)
    return pl.when(true > 0).then(True).when(known == n).then(False)


@register("BARSLAST", [B("COND")], U.COUNT,
          "距上一次条件成立过了几根（当根成立为 0；从未成立、或在那之后有不知道是否成立的根，为未知）", "基础")
def _barslast(ctx, cond):
    last_true = pl.when(cond.fill_null(False)).then(_IDX).forward_fill()
    last_unknown = pl.when(cond.is_null() & (_IDX > _first_known_idx(cond))).then(_IDX).forward_fill()
    return pl.when(cond.is_null() | (last_unknown > last_true)).then(None).otherwise(_IDX - last_true)


@register("BARSSINCE", [B("COND")], U.COUNT,
          "距第一次条件成立过了几根（之前有不知道是否成立的根为未知；公式开头的预热期不算）", "基础")
def _barssince(ctx, cond):
    first_true = pl.when(cond.fill_null(False)).then(_IDX).cum_min().forward_fill()
    first_unknown = pl.when(cond.is_null() & (_IDX > _first_known_idx(cond))).then(_IDX).cum_min().forward_fill()
    return pl.when(first_unknown < first_true).then(None).otherwise(_IDX - first_true)


def _first_known_idx(cond):
    """Bar number of the first bar where the condition is known: nulls before it are the formula's
    warm-up (no data yet), not an unknown answer."""
    return pl.when(cond.is_not_null()).then(_IDX).min()


@register("BARSCOUNT", [S("X")], U.COUNT, "到这一根为止有数据的根数（上市以来的根数）", "基础")
def _barscount(ctx, x):
    return x.is_not_null().cast(pl.Int32).cum_sum()


RESUME_GAP = 30   # calendar days without a bar: a long suspension (no holiday is as long — 春节 about 10, 国庆 9)


@register("BARSRESUMED", [], U.COUNT, "长期停牌（30 天以上没有一根 K 线）后复牌以来的根数，复牌那根是 0；没停过这么久的，"
          "是上市以来的根数减一", "基础")
def _barsresumed(ctx):
    d = ctx.column("date")
    start = pl.when(d.diff().dt.total_days().fill_null(RESUME_GAP) >= RESUME_GAP).then(_IDX).forward_fill()
    return _IDX - start


def _bars_to_extreme(n: int, highest: bool):
    def f(s: pl.Series) -> pl.Series:
        a = s.to_numpy().astype(float)
        out = np.full(a.shape, np.nan)
        if len(a) >= n:
            win = np.lib.stride_tricks.sliding_window_view(a, n)[:, ::-1]  # newest first
            ok = ~np.isnan(win).any(axis=1)
            pos = (np.argmax(win, axis=1) if highest else np.argmin(win, axis=1)).astype(float)
            pos[~ok] = np.nan
            out[n - 1:] = pos
        return pl.Series(s.name, out).fill_nan(None)
    return f


@register("HHVBARS", [S("X"), N("N")], U.COUNT, "N 根内最高值出现在几根之前（同值取最近的一根）", "基础")
def _hhvbars(ctx, x, n):
    return x.map_batches(_bars_to_extreme(n, True), return_dtype=pl.Float64)


@register("LLVBARS", [S("X"), N("N")], U.COUNT, "N 根内最低值出现在几根之前（同值取最近的一根）", "基础")
def _llvbars(ctx, x, n):
    return x.map_batches(_bars_to_extreme(n, False), return_dtype=pl.Float64)


@register("SLOPE", [S("X"), N("N", min=2)], same, "最近 N 根的线性回归斜率（每根的变化量）", "基础")
def _slope(ctx, x, n):
    t = _IDX.cast(pl.Float64)
    stx = (t * x).rolling_sum(n, min_samples=n) - t.rolling_sum(n, min_samples=n) * x.rolling_sum(n, min_samples=n) / n
    return stx / (n * (n * n - 1) / 12)


def _known(*xs: pl.Expr) -> pl.Expr:
    out = xs[0].is_not_null()
    for x in xs[1:]:
        out = out & x.is_not_null()
    return out


register("ABS", [S("X")], same, "绝对值", "基础", scope="el")(lambda ctx, x: x.abs())


@register("MAX", [S("A"), S("B")], lambda u: _unify(u), "两者中较大的", "基础", scope="el")
def _max(ctx, a, b):
    return pl.when(_known(a, b)).then(pl.max_horizontal(a, b))


@register("MIN", [S("A"), S("B")], lambda u: _unify(u), "两者中较小的", "基础", scope="el")
def _min(ctx, a, b):
    return pl.when(_known(a, b)).then(pl.min_horizontal(a, b))


@register("LOG", [S("X")], U.NUMBER, "常用对数（以 10 为底）；X ≤ 0 为未知", "基础", scope="el")
def _log(ctx, x):
    return pl.when(x > 0).then(x.log10())


@register("LN", [S("X")], U.NUMBER, "自然对数；X ≤ 0 为未知", "基础", scope="el")
def _ln(ctx, x):
    return pl.when(x > 0).then(x.log())


def _unify(units):
    from wb.engine.l1.units import unify
    return unify(units[0], units[1])


@register("IF", [B("COND"), S("A"), S("B")], lambda u: _unify(u), "条件成立取 A，否则取 B；条件未知时结果未知", "基础", scope="el")
def _if(ctx, cond, a, b):
    return pl.when(cond.is_null()).then(None).when(cond).then(a).otherwise(b)
