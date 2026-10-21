from datetime import date, timedelta

import polars as pl
import pytest

from tests.test_l1_compiler import D0, frame, run
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.run import evaluate
from wb.registry import FUNCTIONS


def approx(xs, ys, tol=1e-9):
    return all((x is None and y is None) or (x is not None and y is not None and abs(x - y) <= tol) for x, y in zip(xs, ys, strict=True))


def values(expr: str, df, **kw):
    comp = Compiler(kw.pop("market", "CN"), kw.pop("params", None))
    comp.add("x", expr, want="value")
    return evaluate(df, comp.program(), **kw)["x"].to_list()


def test_counting_and_bars_since():
    c = [1.0, 3.0, 2.0, 5.0, 4.0, 6.0]
    df = frame(close=c)
    assert values("COUNT(RAW_C > 2.5, 3)", df) == [None, None, 1, 2, 2, 3]
    assert run("EVERY(C > REF(C, 1), 2)", df) == [None, None, False, False, False, False]
    assert run("EXIST(RAW_C > 5, 2)", df) == [None, False, False, False, False, True]
    assert values("BARSLAST(RAW_C > 2.5)", df) == [None, 0, 1, 0, 0, 0]
    assert values("BARSSINCE(RAW_C > 2.5)", df) == [None, 0, 1, 2, 3, 4]
    assert values("BARSCOUNT(C)", df) == [1, 2, 3, 4, 5, 6]


def test_extreme_positions_slope_and_weighted_average():
    df = frame(close=[1.0, 3.0, 2.0, 3.0, 1.0])
    assert values("HHVBARS(C, 3)", df) == [None, None, 1, 0, 1]  # equal highs: the nearer one
    assert values("LLVBARS(C, 3)", df) == [None, None, 2, 1, 0]
    assert approx(values("SLOPE(C, 3)", frame(close=[1.0, 2.0, 3.0, 5.0])), [None, None, 1.0, 1.5])
    assert approx(values("WMA(C, 3)", frame(close=[1.0, 2.0, 3.0, 6.0])), [None, None, 14 / 6, 26 / 6])


def test_tdx_recursions_match_the_definitions():
    c = [10.0, 11.0, 12.0, 11.5, 12.5]
    df = frame(close=c)
    ema, y = [], None
    for x in c:
        y = x if y is None else (2 * x + 2 * y) / 4  # EMA(C,3): Y = (2X + (N-1)Y') / (N+1)
        ema.append(y)
    assert approx(values("EMA(C, 3)", df), ema)
    sma, y = [], None
    for x in c:
        y = x if y is None else (1 * x + 2 * y) / 3  # SMA(C,3,1): Y = (M X + (N-M) Y') / N
        sma.append(y)
    assert approx(values("SMA(C, 3, 1)", df), sma)


def test_multi_output_indicators_match_p1_functions():
    from wb.engine.l1.functions.trend import KDJ, MACD

    c = [10 + (i % 7) * 0.3 + i * 0.05 for i in range(80)]
    df = frame(close=c, high=[x + 0.2 for x in c], low=[x - 0.2 for x in c])
    direct = df.select(**MACD(pl.col("close")), **KDJ(pl.col("close"), pl.col("high"), pl.col("low")))
    assert approx(values("MACD.DIF", df), direct["dif"].to_list())
    assert approx(values("MACD(12, 26, 9).HIST", df), direct["macd"].to_list())
    assert approx(values("MACD.MACD", df), direct["macd"].to_list())  # 通达信 name for the bar
    assert approx(values("KDJ.J", df), direct["j"].to_list())


def test_element_wise_helpers_keep_unknown():
    df = frame(close=[1.0, 2.0, 3.0])
    assert values("MAX(C, REF(C, 1))", df) == [None, 2.0, 3.0]
    assert values("ABS(RAW_C - 2)", df) == [1.0, 0.0, 1.0]
    assert approx(values("LOG(C * 10)", df), [1.0, 1.30103, 1.4771213], 1e-6)


def test_box_method_indicators():
    df = frame(close=[10.0, 10.5, 11.0, 11.2, 11.4], open=[9.9, 10.2, 10.8, 11.1, 11.0],
               high=[10.2, 10.8, 11.2, 11.5, 11.6], low=[9.8, 10.1, 10.7, 11.0, 10.9],
               volume=[100.0, 250.0, 100.0, 60.0, 50.0])
    assert approx(values("AMPLITUDE(3)", df), [None, None, 11.2 / 9.8 - 1, 11.5 / 10.1 - 1, 11.6 / 10.7 - 1])
    assert approx(values("VOL_MULT", df), [None, 2.5, 0.4, 0.6, 50 / 60])
    assert run("GROUND_VOL(3)", df) == [None, None, True, True, True]  # ties count: lowest of the window
    assert run("SMALL_YANG_RUN(2, 0.06)", df) == [None, None, True, True, True]
    assert approx(values("MA_DIST(2)", df), [None, 10.5 / 10.25 - 1, 11 / 10.75 - 1, 11.2 / 11.1 - 1, 11.4 / 11.3 - 1])


def test_ma_alignment_spread_and_relative_performance():
    c = [float(i) for i in range(1, 31)]
    df = frame(close=c, idx_close=[100.0] * 30)
    assert run("MA_ALIGN([5, 10, 20], up)", df)[-1] is True
    assert run("MA_ALIGN([5, 10, 20], down)", df)[-1] is False
    assert approx(values("MA_SPREAD([5, 10])", df)[-1:], [(28.0 - 25.5) / 30.0])  # ÷ close (box_metrics 口径)
    assert approx(values("REL_PERF(5)", df)[-1:], [30 / 25 - 1])  # flat index: relative = own return
    assert values("IDX_C", df)[-1] == 100.0                           # the reference index close, as a field


def test_a_bar_s_length_is_its_range_over_the_close_before():
    # K 线长度 (箱内 K 线平均长度 is its MEAN over a box stage): (high − low) ÷ the previous close
    df = frame(close=[10.0, 10.0, 10.2], high=[10.1, 10.3, 10.4], low=[9.9, 9.9, 10.0])
    assert approx(values("BAR_LEN", df), [None, 0.04, 0.04])


def test_a_flat_line_is_not_aligned_whichever_way_rounding_tips_it():
    # MA5 and MA10 of a flat close are equal, but polars' sliding sums put them an ulp apart (0.3: MA5 above by 5.6e-17,
    # 1.3: below by 2.2e-16), so a flat line read as 多头 or 空头排列 at random (tpl_ma_bunch picked five such days on
    # CN 2026-04..09); numbers within EQ are equal inside a function as in a formula
    for v in (0.3, 1.3, 23.45):
        df = frame(close=[v] * 25)
        assert run("MA_ALIGN([5, 10], up)", df)[-1] is False, v
        assert run("MA_ALIGN([5, 10], down)", df)[-1] is False, v


def test_limits_cn_and_jp():
    cn = frame(close=[10.0, 11.0, 11.0], high=[10.0, 11.0, 11.5], up_limit=[11.0, 11.0, 12.1],
               down_limit=[9.0, 9.9, 9.9], pre_close=[10.0, 10.0, 11.0])
    assert run("LIMIT_UP_CLOSE", cn) == [False, True, False]
    assert run("LIMIT_UP_TOUCH", cn) == [False, True, False]
    assert approx(values("LIMIT_PCT", cn), [0.1, 0.1, 0.1])
    assert values("LIMIT_UP_STREAK", frame(close=[11.0, 12.1, 12.0, 13.2], up_limit=[11.0, 12.1, 13.3, 13.2])) == [1, 2, 0, 1]
    jp = frame(close=[500.0, 580.0, 580.0], high=[500.0, 580.0, 600.0], up_limit=[None, 580.0, 660.0],
               down_limit=[None, 420.0, 500.0], ul=[None, True, True], ll=[None, False, False])
    # J-Quants UL marks a touch; a close at the limit must also close at the high
    assert run("LIMIT_UP_CLOSE", jp, market="JP") == [None, True, False]  # no previous close: unknown


def test_cross_sectional_rank_per_date():
    df = pl.DataFrame({"listing_id": ["A", "B", "C"] * 2, "date": [D0] * 3 + [D0 + timedelta(days=1)] * 3,
                       "close": [3.0, 1.0, 2.0, 1.0, 2.0, 3.0]}).sort("listing_id", "date")
    out = evaluate(df, _prog("RANK_XS(C)")).sort("date", "listing_id")["x"].to_list()
    assert out == [1.0, 0.0, 0.5, 0.0, 0.5, 1.0]


def _prog(expr):
    comp = Compiler("CN")
    comp.add("x", expr)
    return comp.program()


def test_weekly_value_is_known_only_after_the_week_closes():
    days = [D0 + timedelta(days=i) for i in range(12) if (D0 + timedelta(days=i)).weekday() < 5]  # Mon 5 Jan ..
    df = frame(close=[float(i + 1) for i in range(len(days))]).with_columns(pl.Series("date", days))
    # the exchange calendar is known ahead (here: through the next Monday), so both weeks can close
    out = evaluate(df, _prog('TF("W", C)'), calendar_open=days + [date(2026, 1, 19)])
    v = dict(zip(out["date"], out["x"], strict=True))
    assert v[date(2026, 1, 8)] is None          # the first week is still running on Thursday
    assert v[date(2026, 1, 9)] == 5.0           # Friday close completes it
    assert v[date(2026, 1, 13)] == 5.0          # next week: last week's value until this one completes
    assert v[date(2026, 1, 16)] == 10.0


def test_long13_formula_counterexample():
    # the formula's own check: 60 closes of 10 then 12 pass every price condition
    # although the close is 19.6% above MA60: the code has no "within one limit-up of MA60" rule.
    close = [10.0] * 60 + [12.0]
    df = frame(close=close)
    xg = ("COUNT(C >= REF(C, 1) * 1.098, 60) == 1 AND (HHV(H, 40) / LLV(L, 40) - 1) * 100 < 30 "
          "AND MA(C, 60) >= REF(MA(C, 60), 1) AND C > MA(C, 60)")
    assert run(xg, df)[-1] is True
    assert approx(values("MA_DIST(60)", df)[-1:], [12 / ((59 * 10 + 12) / 60) - 1])


def test_every_registered_function_has_a_doc_and_category():
    from wb.registry import autoload
    autoload("wb.engine.l1.functions")
    assert len(FUNCTIONS) >= 59
    for fn in FUNCTIONS.values():
        assert fn.doc and fn.category, fn.name


def test_relative_strength_patterns_in_their_order():
    # index falls 5% over the window while the stock rises 5%: case 1 (大盘跌个股涨)
    n = 6
    idx = [100.0, 99, 98, 97, 96, 95.0]
    up = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5]
    flat = [10.0, 10.0, 10.01, 9.99, 10.0, 10.0]
    assert values("REL_PATTERN(5)", frame(close=up, idx_close=idx))[-1] == 1
    assert values("REL_PATTERN(5)", frame(close=flat, idx_close=idx))[-1] == 2  # 大盘跌个股横
    # flat index, stock up: case 7; everything rising together: none of the seven
    assert values("REL_PATTERN(5)", frame(close=up, idx_close=[100.0] * n))[-1] == 7
    assert values("REL_PATTERN(5)", frame(close=up, idx_close=[100.0, 101, 102, 103, 104, 105]))[-1] == 0
    # index slips to a marginal new low (no big fall, so not cases 1-3) while the stock holds: case 4
    dip = [100.0, 99.2, 100.5, 100.0, 99.5, 99.1]
    hold = [10.0, 9.9, 10.1, 10.05, 10.0, 10.0]
    assert values("REL_PATTERN(5)", frame(close=hold, idx_close=dip))[-1] == 4
    # index climbs back to its earlier peak while the stock is already well above its own: case 6
    idx6 = [100.0, 104, 102, 98, 96, 97, 98, 99, 100, 101, 104.5]
    stk6 = [10.0, 10.3, 10.2, 10.0, 10.1, 10.3, 10.5, 10.6, 10.8, 10.9, 11.2]
    assert values("REL_PATTERN(5)", frame(close=stk6, idx_close=idx6))[-1] == 6


def test_a_week_the_calendar_cannot_close_is_not_used():
    # the calendar stops on Wednesday with the data: that week may still trade, so no weekly value
    days = [D0 + timedelta(days=i) for i in range(10) if (D0 + timedelta(days=i)).weekday() < 5]  # Mon 5 .. Wed 14
    df = frame(close=[float(i + 1) for i in range(len(days))]).with_columns(pl.Series("date", days))
    out = evaluate(df, _prog('TF("W", C)'), calendar_open=days)
    assert dict(zip(out["date"], out["x"], strict=True))[date(2026, 1, 14)] == 5.0  # still last week's


def test_cross_and_windows_accept_a_number():
    # CROSS(RSI(6), 30) is the commonest 通达信 idiom: a number argument is a constant series, not
    # a single value that shifts to unknown
    df = frame(close=[1.0, 3.0, 2.0, 5.0])
    assert run("CROSS(RAW_C, 2.5)", df) == [False, True, False, True]
    assert values("REF(5, 1)", df) == [None, 5.0, 5.0, 5.0]
    assert values("MA(5, 3)", df) == [None, None, 5.0, 5.0]


def test_weighted_average_tolerates_unknown_inputs():
    # leading unknowns (an inner MA's warm-up) must not crash the weighted window
    out = values("WMA(MA(C, 2), 3)", frame(close=[1.0, 2.0, 3.0, 4.0, 5.0]))
    assert approx(out, [None, None, None, 17 / 6, 23 / 6])


def test_weekly_value_appears_when_the_exchange_closes_the_week():
    # B has no bar on Friday 9 Jan: its Mon–Thu week is complete once the exchange's week closes,
    # so on its next bar (Monday) it sees that week's close, not the week before or nothing
    days = [D0 + timedelta(days=i) for i in range(12) if (D0 + timedelta(days=i)).weekday() < 5]  # Mon 5 .. Fri 16
    a = frame(close=[float(i + 1) for i in range(len(days))]).with_columns(pl.Series("date", days))
    b_days = [d for d in days if d != date(2026, 1, 9)]
    b = frame(close=[100.0 + i for i in range(len(b_days))], listing_id=["CN:000001"] * len(b_days)).with_columns(
        pl.Series("date", b_days))
    out = evaluate(pl.concat([a, b]), _prog('TF("W", C)'), calendar_open=days + [date(2026, 1, 19)])
    v = {(r["listing_id"], r["date"]): r["x"] for r in out.iter_rows(named=True)}
    assert v[("CN:600519", date(2026, 1, 9))] == 5.0
    assert v[("CN:000001", date(2026, 1, 12))] == 103.0  # Thursday's close
    assert v[("CN:000001", date(2026, 1, 16))] == 108.0


def test_a_streak_through_an_unknown_day_is_unknown_not_broken():
    # day 2's limit price is missing: whether it closed limit-up is unknown, so the streak after it
    # is unknown too (not restarted at 0) — "N 天连板" must not answer False on missing data
    cn = frame(close=[10.0, 11.0, 12.1, 13.31, 13.0], up_limit=[11.0, 11.0, None, 13.31, 14.64],
               down_limit=[9.0, 9.9, 10.89, 10.89, 11.98], pre_close=[10.0, 10.0, 11.0, 12.1, 13.31])
    assert values("LIMIT_UP_STREAK", cn) == [0, 1, None, None, 0]
    assert run("EXIST(LIMIT_UP_STREAK >= 2, 2)", cn)[3] is None


def test_exist_and_every_settle_when_a_known_bar_decides():
    # Kleene: one true bar makes EXIST true and one false bar makes EVERY false, whatever is unknown
    df = frame(close=[1.0, 2.0, 3.0, 4.0], up_limit=[None, 2.0, None, 9.0], down_limit=0.5, pre_close=1.0)
    assert run("EXIST(LIMIT_UP_CLOSE, 2)", df) == [None, True, True, None]
    assert run("EVERY(LIMIT_UP_CLOSE, 2)", df) == [None, None, None, False]


def test_bars_since_with_an_unknown_bar_in_between_is_unknown():
    # day 3 unknown after the last true on day 1: it may have been true, so BARSLAST is unknown; the
    # leading warm-up of a formula is not such a gap (BARSSINCE over an average must stay usable)
    df = frame(close=[1.0, 2.0, 3.0, 4.0, 5.0], up_limit=[1.0, 2.0, 9.0, None, 9.0], down_limit=0.5, pre_close=1.0)
    assert values("BARSLAST(LIMIT_UP_CLOSE)", df) == [0, 0, 1, None, None]
    assert values("BARSSINCE(C > MA(C, 2))", frame(close=[1.0, 2.0, 3.0, 2.0])) == [None, 0, 1, 2]


def test_rank_within_an_unknown_industry_is_unknown():
    df = frame(close=[1.0, 2.0, 3.0, 4.0], listing_id=["CN:1", "CN:2", "CN:3", "CN:4"],
               industry=["银行", "银行", None, None]).with_columns(pl.lit(date(2026, 1, 5)).alias("date"))
    out = values("RANK_XS(C, industry)", df)
    assert out[:2] == [0.0, 1.0] and out[2:] == [None, None]


def test_a_flat_line_s_slope_and_distance_are_exactly_zero():
    # a 60-day line flat to the cent comes out ±2.2e-16 off in floating point (603042 on 2026-06-01: −4.4e-17), so
    # "走平" (slope ≥ 0) passed or failed at random; values a rounding apart (EQ) are equal, their change is 0
    import random
    rnd = random.Random(1)
    block = [round(rnd.uniform(20, 25), 2) for _ in range(60)]
    df = frame(close=block * 4)   # every 60-bar window holds the same prices: MA60 is flat from bar 60 on
    slope = values("MA_SLOPE_PCT(60, 5)", df)[65:]
    assert set(slope) == {0.0}
    assert set(run("MA_SLOPE_PCT(60, 5) > 0", df)[65:]) == {False}
    assert set(values("MA_DIST(60)", frame(close=[22.63] * 70))[59:]) == {0.0}


def test_a_difference_of_equal_numbers_is_zero():
    # 3.00 after a 10% factor equals the 3.30 five bars before it, yet 3 × 1.1 is 3.3000000000000003 in floating
    # point: the change is 0, not ±1e-16 — "< 0" / "> 0" must not see it
    df = frame(close=[3.3, 3.3, 3.3, 3.3, 3.3, 3 * 1.1])
    assert values("REF(C, 5) / C - 1", df)[5] == 0.0
    assert run("REF(C, 5) - C < 0", df)[5] is False
