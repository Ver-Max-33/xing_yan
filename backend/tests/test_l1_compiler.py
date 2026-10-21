from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l1.compiler import CompileError, Compiler
from wb.engine.l1.run import evaluate
from wb.engine.l1.units import U

D0 = date(2026, 1, 5)


def frame(**series):
    """One listing's bars (or several, with listing_id given); OHLC default to `close`."""
    n = len(series["close"])
    lid = series.pop("listing_id", ["CN:600519"] * n)
    base = {"listing_id": lid, "date": [D0 + timedelta(days=i) for i in range(n)]}
    c = series["close"]
    for k in ("open", "high", "low"):
        series.setdefault(k, c)
    for k in ("raw_open", "raw_high", "raw_low", "raw_close"):
        series.setdefault(k, series[k[4:]])
    series.setdefault("volume", [1000.0] * n)
    series.setdefault("raw_volume", series["volume"])
    series.setdefault("amount", [None] * n)
    return pl.DataFrame({**base, **series}, schema_overrides={"amount": pl.Float64})


def run(formula: str, df: pl.DataFrame, market="CN", params=None, **kw):
    comp = Compiler(market, params)
    comp.add("x", formula, **kw)
    return evaluate(df, comp.program())["x"].to_list()


def err(formula: str, market="CN", **kw) -> str:
    with pytest.raises(CompileError) as e:
        Compiler(market, kw.pop("params", None), **kw).add("x", formula)
    return str(e.value)


def test_units_are_checked():
    assert "不能比较" in err("C > V")
    assert "单位不一致" in err("C + V")
    assert "条件不能参与运算" in err("(C > O) + 1")
    assert "AND 两边都要是条件" in err("C AND V")
    assert Compiler("CN").add("x", "AMOUNT / RAW_V > RAW_L") == U.BOOL  # amount / volume is a raw price
    assert "原始价 和 价格" in err("AMOUNT / RAW_V > L")  # ... and cannot be compared with hfq prices


def test_absolute_price_thresholds_need_raw_prices():
    # hfq history changes with every ex-date, so "close above 5 yuan" must read the exchange price
    assert "RAW_C" in err("C > 5")
    assert Compiler("CN").add("x", "RAW_C > 5") == U.BOOL
    assert Compiler("CN").add("x", "C > REF(C, 1) * 1.098") == U.BOOL  # relative: fine on hfq
    assert Compiler("CN").add("x", "C > 0") == U.BOOL


def test_future_functions_and_negative_ref_are_rejected():
    assert "未来" in err("ZIG(3, 10) > 0")
    assert "未来" in err("BACKSET(C > O, 2)")
    assert "负数" in err("REF(C, -1) > C")


def test_helpful_errors():
    assert "不认识的函数 FOO" in err("FOO(C) > 1")
    assert "不认识的名字 XYZ" in err("XYZ > 1")
    assert "MACD 有多个输出" in err("MACD > 0")
    assert "MACD 没有 FOO" in err("MACD.FOO > 0")
    assert "参数 $tol 没有定义" in err("C > REF(C, 1) * $tol")
    assert "要是整数" in err("MA(C, 2.5) > C")
    assert "在 JP 没有数据" in err("TURNOVER > 3", market="JP")
    assert "INDEX_STATE：当前数据档没有指数日线" in err("INDEX_STATE > 0", available={"close"})
    assert "只能在规则的阶段条件里" in err("C > box.top")
    assert "规则" in err("BAND_VOL_RATIO(box) > 2")      # rule-only words get a hint, not 不认识
    assert "规则" in err("MEAN(V, of=box) > 1")
    assert "规则" in err("MA(V, 5, last=3) > 1")
    assert "要的是条件" in str(pytest.raises(CompileError, Compiler("CN").add, "x", "C + 1", want="cond").value)


def test_identical_subexpressions_are_computed_once():
    comp = Compiler("CN")
    comp.add("a", "MA(C, 20) > MA(C, 60)")
    comp.add("b", "C > MA(C, 20)")
    window_cols = [c for step in comp.program().steps for c in step if c.startswith("l1_")]
    assert len(window_cols) == 2  # MA(C,20) once, MA(C,60) once


def test_unknown_stays_unknown():
    df = frame(close=[10.0, 11.0, 12.0, 11.0])
    assert run("C > MA(C, 3)", df) == [None, None, True, False]  # MA needs 3 bars; 11 > 11.33 is False
    assert run("IF(C > MA(C, 3), 1, 0) > 0", df) == [None, None, True, False]
    assert run("C / (C - C) > 0", df) == [None] * 4  # x / 0 is unknown, not infinity
    assert run("C > MA(C, 3) OR C > 0", df) == [True] * 4  # three-valued OR
    assert run("C > MA(C, 3) AND C < 0", df) == [False] * 4


def test_windows_never_cross_listings():
    df = frame(close=[1.0, 2.0, 3.0, 100.0, 200.0, 300.0], listing_id=["A"] * 3 + ["B"] * 3)
    assert run("MA(C, 2) > 0", df) == [None, True, True, None, True, True]
    assert run("REF(C, 1) * 0 + 1 > 0", df) == [None, True, True, None, True, True]


def test_params_and_tf_compile():
    assert Compiler("CN", {"k": 1.098}).add("x", "C >= REF(C, 1) * $k") == U.BOOL
    assert Compiler("CN").add("x", 'TF("W", MA(C, 5)) > 0') == U.BOOL
    assert "周期只能是" in err('TF("D", C) > 0')
    assert "不能嵌套" in err('TF("W", TF("M", C)) > 0')


def test_all_functions_load_even_if_one_module_was_imported_first():
    import subprocess
    import sys

    code = ("import wb.engine.l1.functions.trend\n"
            "from wb.engine.l1.compiler import Compiler\n"
            "Compiler('CN').add('x', 'RANK_XS(C) > 0.5')\n")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def test_builder_errors_are_compile_errors():
    # 通达信 requires N > M for SMA; an alpha above 1 must read as a formula error, not a traceback
    assert "SMA" in err("SMA(C, 3, 5) > 0")


def test_every_valuation_column_of_the_catalog_is_a_formula_field():
    # spec §3.5 mechanism 2: a dataset's fields can be used in formulas without listing them by hand
    from wb.datasets import DATASETS
    from wb.engine.l1.fields import FIELDS

    cols = {c for c in DATASETS["valuation"].columns if c not in ("listing_id", "date", "source")}
    assert cols <= {f.column for f in FIELDS.values()}
    assert Compiler("CN").add("x", "PS_TTM > 1 AND DV_TTM > 2 AND RAW_V / FLOAT_SHARE > 0") == U.BOOL
    assert Compiler("JP").add("x", "RAW_C / EPS < 15 AND BPS > 0") == U.BOOL
    assert "不适用" in err("PS_TTM > 1", market="JP") or "没有" in err("PS_TTM > 1", market="JP")


def test_equal_numbers_written_differently_share_a_column():
    def columns(*formulas):
        comp = Compiler("CN")
        for k, f in enumerate(formulas):
            comp.add(f"x{k}", f)
        return sum(len(step) for step in comp.program().steps)

    assert columns("BOLL(20, 2).UPPER > C", "BOLL(20, 2.0).UPPER > C") == columns("BOLL(20, 2).UPPER > C") + 1


def test_an_output_name_is_used_once():
    comp = Compiler("CN")
    comp.add("x", "C > O")
    with pytest.raises(CompileError, match="x"):
        comp.add("x", "C < O")


def test_evaluate_refuses_a_frame_out_of_order():
    from wb.engine.l1.run import evaluate

    df = frame(close=[1.0, 2.0, 3.0]).reverse()
    comp = Compiler("CN")
    comp.add("x", "MA(C, 2)", want="value")
    with pytest.raises(ValueError, match="按日期排序"):
        evaluate(df, comp.program())


def test_a_comparison_takes_values_a_rounding_apart_as_equal():
    # 0.2 is exactly the mean of 0.1, 0.3 and 0.2, but the float sum gives 0.20000000000000004: not below, and at least
    df = frame(close=[0.1, 0.3, 0.2])
    assert run("C < MA(C, 3)", df)[-1] is False and run("C >= MA(C, 3)", df)[-1] is True
    assert run("C == MA(C, 3)", df)[-1] is True and run("C != MA(C, 3)", df)[-1] is False
    assert run("C > MA(C, 3) * 0.999", df)[-1] is True   # a real difference is untouched
