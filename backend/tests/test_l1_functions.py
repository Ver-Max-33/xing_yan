import math

import polars as pl
import pytest

from wb.engine.l1.functions.base import CROSS, EMA, HHV, MA, REF, SMA, STD
from wb.engine.l1.functions.trend import BOLL, KDJ, MACD


def ev(values, expr):
    return pl.DataFrame({"x": values}).select(expr.alias("y"))["y"].to_list()


def test_ma_needs_full_window():
    assert ev([1.0, 2.0, 3.0, 4.0], MA(pl.col("x"), 3)) == [None, None, 2.0, 3.0]


def test_ema_is_tdx_recursion_from_first_value():
    # Y = (2X + (N-1)Y')/(N+1), Y0 = X0
    xs = [10.0, 11.0, 12.0, 11.0]
    y, out = xs[0], []
    for x in xs:
        y = (2 * x + 2 * y) / 4 if out else x
        out.append(y)
    assert ev(xs, EMA(pl.col("x"), 3)) == pytest.approx(out)


def test_sma_is_tdx_weighted_recursion():
    xs = [50.0, 80.0, 20.0]
    out = [50.0, (1 * 80 + 2 * 50) / 3]
    out.append((1 * 20 + 2 * out[1]) / 3)
    assert ev(xs, SMA(pl.col("x"), 3, 1)) == pytest.approx(out)


def test_std_is_sample_std():
    got = ev([1.0, 2.0, 3.0, 4.0], STD(pl.col("x"), 4))[-1]
    assert got == pytest.approx(math.sqrt(sum((v - 2.5) ** 2 for v in [1, 2, 3, 4]) / 3))


def test_ref_and_cross():
    assert ev([1.0, 2.0, 3.0], REF(pl.col("x"), 1)) == [None, 1.0, 2.0]
    with pytest.raises(ValueError):
        REF(pl.col("x"), -1)
    df = pl.DataFrame({"a": [1.0, 2.0, 3.0, 2.0], "b": [2.0, 2.0, 2.0, 2.5]})
    assert df.select(CROSS(pl.col("a"), pl.col("b")))["a"].to_list() == [False, False, True, False]


def test_hhv_window():
    assert ev([3.0, 1.0, 2.0, 5.0], HHV(pl.col("x"), 2)) == [None, 3.0, 2.0, 5.0]


def test_macd_matches_reference_formula():
    closes = [10.0 + math.sin(i / 3) + i * 0.05 for i in range(80)]
    df = pl.DataFrame({"c": closes}).with_columns(**MACD(pl.col("c")))
    # reference implementation in plain Python
    def ema(xs, n):
        out, y = [], None
        for x in xs:
            y = x if y is None else (2 * x + (n - 1) * y) / (n + 1)
            out.append(y)
        return out

    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26), strict=True)]
    dea = ema(dif, 9)
    assert df["dif"].to_list() == pytest.approx(dif)
    assert df["macd"].to_list() == pytest.approx([2 * (a - b) for a, b in zip(dif, dea, strict=True)])


def test_kdj_flat_window_and_values():
    n = 12
    df = pl.DataFrame({"c": [10.0] * n, "h": [10.0] * n, "l": [10.0] * n}).with_columns(
        **KDJ(pl.col("c"), pl.col("h"), pl.col("l")))
    assert df["k"][:8].to_list() == [None] * 8 and df["k"][8] == 0.0  # flat window -> RSV 0
    rising = pl.DataFrame({"c": [float(i) for i in range(1, 21)], "h": [i + 0.5 for i in range(1, 21)],
                           "l": [i - 0.5 for i in range(1, 21)]}).with_columns(**KDJ(pl.col("c"), pl.col("h"), pl.col("l")))
    assert 50 < rising["k"][-1] < 100 and rising["j"][-1] == pytest.approx(3 * rising["k"][-1] - 2 * rising["d"][-1])


def test_boll():
    df = pl.DataFrame({"c": [float(i) for i in range(1, 21)]}).with_columns(**BOLL(pl.col("c")))
    mid, sd = 10.5, math.sqrt(sum((i - 10.5) ** 2 for i in range(1, 21)) / 19)
    assert df["boll_mid"][-1] == pytest.approx(mid) and df["boll_up"][-1] == pytest.approx(mid + 2 * sd)


def test_a_tie_that_rounding_tips_is_still_a_tie():
    # MA5 and MA20 that are equal to the cent come out a bit apart in floating point (002419 on 2024-01-02: +3.6e-15);
    # the bar where they are equal is not yet "above", so the cross is the next bar, as exact arithmetic has it
    df = pl.DataFrame({"a": [1.0, 2.0 + 4e-15, 3.0], "b": [1.5, 2.0, 2.5]})
    assert df.select(CROSS(pl.col("a"), pl.col("b")))["a"].to_list() == [False, False, True]
