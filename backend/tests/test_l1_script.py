"""指标库's formulas (design 2026-10-10 §3): 通达信 / 同花顺 scripts, the lenient (通达信) reading, the names this system
has no data for said at once, and the functions an indicator needs (arithmetic, AVEDEV, VALUEWHEN, BARSLASTCOUNT,
FILTER, REF with a varying N, SAR, SUPERTREND)."""
import math
import random
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from tests.test_l1_compiler import err, frame
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.run import evaluate
from wb.engine.l1.script import compile_script, statements
from wb.indicators import IndicatorError, load_indicator

# the 同花顺 Level-2 indicator you sent on 2026-10-08 (its second version, as pasted, shortened by nothing that matters here)
THS = """CLOSE30:=MA(CLOSE,30);
ZHANF:=IF((CODE>=688000AND CODE<788000) OR (CODE<400000AND CODE>200000),120,110);
涨停:=IF(ROUND(CLOSE*99.65/PRE)=ZHANF,1,0);
ZDMRT:=BIGBUYCOUNT1;
ZDMCT:=BIGSELLCOUNT1;
BDMRT:=WAITBUYCOUNT1;
VOL1:=VOL*100/ZYLTGB;
资金:=IF(涨停=1AND ZJLR=0,382000,ZJLR-ZJLC)*LOG(CLOSE*VOL1)/(ZJLC+100000);
明庄:=(IF(zDMrt>=ZDMct AND zDMrt>=bDMrt,zdmrt,bdmrt)-ZDMCt)/MAX(ZDMCt,1000);
动能a:=SUM((OUTVOL-INVOL)*LN(6.18*VOL1*CLOSE),3)/SUM(INVOL,3);
动能系数:IF(SUM(明庄,3)>10,10,IF(SUM(明庄,3)<-4,-4,SUM(明庄,3)));
FENX1:=SMA(MAX(CLOSE-PRE,0),3,1)/SMA(ABS(CLOSE-PRE),3,1)*100;
wave1:=SMA(100*(CLOSE-LLV(LOW,9))/(HHV(HIGH,8)-LLV(LOW,9)),3,1);
FENGX:=0.5*FENX1+0.5*wave1;
盘量:(ZDMRt+BDMRt)/VOL;
DRAWICON(CROSS(动能系数,FENGX)=1AND 动能系数>REF(动能系数,1),-1,1);
DRAWICON(FENGX>80AND 盘量<0AND (明庄<0 OR 资金<0),2,2);
STICKLINE(盘量>1 AND 资金>0.1,0,IF(盘量>8,8,盘量),10,0),coloryellow;"""


def series(text: str, df, params=None, market="CN"):
    c = compile_script(text, market, params)
    assert not c.errors and not c.unsupported, (c.errors, c.unsupported)
    out = evaluate(df, c.program)
    return c, {it.name: out[it.cols["value"]].to_list() for it in c.items if "value" in it.cols}, out


def close_enough(xs, ys, tol=1e-9):
    return all((x is None and y is None) or (x is not None and y is not None and abs(x - y) <= tol * max(1, abs(y)))
               for x, y in zip(xs, ys, strict=True))


def walk(n=300, seed=7):
    rnd = random.Random(seed)
    c, out = 10.0, []
    for _ in range(n):
        o = c * (1 + rnd.uniform(-0.02, 0.02))
        c = o * (1 + rnd.uniform(-0.03, 0.03))
        h, lo = max(o, c) * (1 + rnd.uniform(0, 0.02)), min(o, c) * (1 - rnd.uniform(0, 0.02))
        out.append((o, h, lo, c))
    o, h, lo, c = (list(x) for x in zip(*out, strict=True))
    return frame(open=o, high=h, low=lo, close=c, volume=[1000.0 + 37 * i % 500 for i in range(n)])


# ---- the script ---------------------------------------------------------------------------------------------
def test_tongdaxin_statements_comments_and_full_width_punctuation():
    stmts, errors = statements("{均线} MA5:=MA(C,5)；\n// 画两条\nM1:MA5,COLORRED,LINETHICK2;\nMA(C,10)，DOTLINE;\n"
                               "STICKLINE(C>O && V>MA(V,5),O,C,6,0),COLOR0000FF;\nX:,COLORRED;")
    assert [(s.kind, s.name, s.text, s.attrs, s.line) for s in stmts] == [
        ("var", "MA5", "MA(C,5)", [], 1), ("line", "M1", "MA5", ["COLORRED", "LINETHICK2"], 3),
        ("line", None, "MA(C,10)", ["DOTLINE"], 4), ("draw", None, "STICKLINE(C>O  AND  V>MA(V,5),O,C,6,0)", ["COLOR0000FF"], 5)]
    assert errors == [{"line": 6, "message": "X 后面没有公式"}]


def test_the_classic_formulas_read_as_written_and_give_the_systems_own_numbers():
    df = walk()
    c, v, _ = series("DIF:EMA(CLOSE,SHORT)-EMA(CLOSE,LONG);\nDEA:EMA(DIF,MID);\nMACD:(DIF-DEA)*2,COLORSTICK;", df,
                     {"SHORT": 12, "LONG": 26, "MID": 9})
    comp = Compiler("CN")
    for k in ("DIF", "DEA", "HIST"):
        comp.add(k, f"MACD(12, 26, 9).{k}")
    ref = evaluate(df, comp.program())
    assert close_enough(v["DIF"], ref["DIF"].to_list()) and close_enough(v["DEA"], ref["DEA"].to_list())
    assert close_enough(v["MACD"], ref["HIST"].to_list())
    assert [it.kind for it in c.items] == ["line", "line", "colorstick"] and not c.price_only   # MACD's 柱: a sub pane
    c, _, _ = series("MA1:MA(C,5);\nMA2:MA(C,10),COLORRED;", df)
    assert c.price_only and c.items[1].color == "#ff0000"                                     # prices: over the candles


def test_the_tongdaxin_way_a_condition_is_one_or_zero_and_a_number_is_a_condition():
    df = frame(close=[10.0, 11.0, 10.5, 12.0], open=[10.0, 10.5, 11.0, 11.0])
    _, v, _ = series("A:(C>O)*2;\nB:IF(C-O,1,0);\nX:CROSS(C,MA(C,2))=1;\nY:C>10.8 AND V;", df)
    assert v["A"] == [0.0, 2.0, 0.0, 2.0]
    assert v["B"] == [0.0, 1.0, 1.0, 1.0]
    assert v["X"] == [None, None, False, True]          # the bar before had no MA2 yet: unknown, not false
    assert v["Y"] == [False, True, False, True]
    assert "后复权价格不能和固定数字比较" in err("C > 10")   # a rule still may not: its prices are 后复权


def test_names_are_chinese_and_not_case_sensitive_and_parameters_are_bare_names():
    df = frame(close=[1.0, 2.0, 3.0, 4.0])
    _, v, _ = series("涨幅:=C/REF(C,1)-1;\nzdmrt:=涨幅*100;\nOUT:ZDMRT+N;", df, {"N": 1})
    assert close_enough(v["OUT"], [None, 101.0, 51.0, 100 / 3 + 1])


def test_what_is_wrong_is_said_by_line():
    c = compile_script("A:=C>O;\nB:A*2+FOO;\nSTICKLINE(C>O,O);\nDRAWTEXT(C>O,H,阳);\nX:MA(C,5),COLORBLAH;\nC:=O;\nA:=H;\nQ:MA(C,5)", "CN")
    assert c.program is None
    msgs = {e["line"]: e["message"] for e in c.errors}
    assert "不认识的名字 FOO" in msgs[2]
    assert "STICKLINE 写 3–5 个参数" in msgs[3]
    assert "文字要写在引号里" in msgs[4]
    assert "不认识的画线属性 COLORBLAH" in msgs[5]
    assert "是内置的字段" in msgs[6]
    assert "前面已经定义过" in msgs[7]
    assert 8 not in msgs   # the last statement needs no `;`
    assert "未来" in compile_script("A:ZIG(3,10);", "CN").errors[0]["message"]
    assert "不认识的名字 N" in compile_script("A:MA(C,N);", "CN").errors[0]["message"]   # a parameter not in the table yet


def test_your_level2_indicator_reads_and_says_at_once_what_this_system_has_no_data_for():
    c = compile_script(THS, "CN")
    assert c.errors == [] and c.program is None
    l2 = next(v for k, v in c.unsupported.items() if "Level-2" in k)
    assert l2 == ["BIGBUYCOUNT1", "BIGSELLCOUNT1", "WAITBUYCOUNT1", "ZJLR", "ZJLC"]
    assert next(v for k, v in c.unsupported.items() if "自由流通股本" in k) == ["ZYLTGB"]
    assert next(v for k, v in c.unsupported.items() if "内盘" in k) == ["OUTVOL", "INVOL"]
    assert c.reduced is None   # every line drawn needs them: nothing is left to draw
    part = compile_script(THS.replace("盘量:(ZDMRt+BDMRt)/VOL;", "FG:FENGX;"), "CN")
    assert part.reduced is not None and "FG:FENGX;" in part.reduced and "BIGBUYCOUNT1" not in part.reduced
    reduced = compile_script(part.reduced, "CN")
    assert reduced.errors == [] and reduced.unsupported == {} and [it.name for it in reduced.items] == ["FG"]


def test_the_limit_up_and_the_fengx_parts_compute():
    df = frame(close=[10.0, 11.0, 12.1, 12.0], listing_id=["CN:600519"] * 4)
    df = df.with_columns(pre=df["close"].shift(1))
    _, v, _ = series("ZHANF:=IF((CODE>=688000AND CODE<788000) OR (CODE<400000AND CODE>200000),120,110);\n"
                     "涨停:IF(ROUND(CLOSE*99.65/PRE)=ZHANF,1,0);", df)
    assert v["涨停"] == [None, 1.0, 1.0, 0.0]


# ---- the functions ------------------------------------------------------------------------------------------
def test_arithmetic_functions():
    df = frame(close=[-2.5, -0.5, 0.5, 2.5, 4.0])
    _, v, _ = series("A:ROUND(C);\nB:POW(C,2);\nD:SQRT(C);\nE:SIGN(C);\nF:FLOOR(C);\nG:CEILING(C);\nI:INTPART(C);\nM:MOD(C,2);"
                     "\nR:ROUND2(C/3,2);", df)
    assert v["A"] == [-3.0, -1.0, 1.0, 3.0, 4.0]                                 # .5 away from 0, as 通达信
    assert v["B"] == [6.25, 0.25, 0.25, 6.25, 16.0]
    assert v["D"][:2] == [None, None] and close_enough(v["D"][2:], [math.sqrt(0.5), math.sqrt(2.5), 2.0])
    assert v["E"] == [-1.0, -1.0, 1.0, 1.0, 1.0]
    assert v["F"] == [-3.0, -1.0, 0.0, 2.0, 4.0] and v["G"] == [-2.0, 0.0, 1.0, 3.0, 4.0] and v["I"] == [-2.0, 0.0, 0.0, 2.0, 4.0]
    assert v["M"] == [1.5, 1.5, 0.5, 0.5, 0.0]
    assert v["R"] == [-0.83, -0.17, 0.17, 0.83, 1.33]


def test_avedev_valuewhen_barslastcount_filter_cumulative_and_ref_by_a_varying_n():
    c = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    df = frame(close=c)
    _, v, _ = series("A:AVEDEV(C,3);\nW:VALUEWHEN(C>4,C);\nK:BARSLASTCOUNT(C>REF(C,1));\nF:FILTER(C>2,2);\nS:SUM(C,0);"
                     "\nHH:HHV(C,0);\nR:REF(C,BARSLAST(C<REF(C,1)));\nT:COUNT(C>3,0);", df)
    want = [None, None] + [float(np.abs(np.array(c[i - 2:i + 1]) - np.mean(c[i - 2:i + 1])).mean()) for i in range(2, 8)]
    assert close_enough(v["A"], want)
    assert v["W"] == [None, None, None, None, 5.0, 9.0, 9.0, 6.0]
    assert v["K"] == [None, 0, 1, 0, 1, 2, 0, 1]                              # the first bar has no bar before it
    assert v["F"] == [True, False, False, False, True, False, False, True]   # after a signal, the next 2 bars do not count
    assert v["S"] == [3.0, 4.0, 8.0, 9.0, 14.0, 23.0, 25.0, 31.0]
    assert v["HH"] == [3.0, 3.0, 4.0, 4.0, 5.0, 9.0, 9.0, 9.0]
    assert v["R"] == [None, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0]               # the close of the last down bar
    assert v["T"] == [0, 0, 1, 1, 2, 3, 3, 4]


def _sar_tv(h, lo, c, first, start, inc, mx):
    """TradingView's pine_sar, written again plainly (the witness for the compiled one)."""
    out = [None] * len(h)
    result = maxmin = acc = None
    below = None
    for i in range(first, len(h)):
        first_bar = False
        if i == first:
            below = c[i] > c[i - 1]
            maxmin, result = (h[i], lo[i - 1]) if below else (lo[i], h[i - 1])
            first_bar, acc = True, start
        result = result + acc * (maxmin - result)
        if below and result > lo[i]:
            first_bar, below, result, maxmin, acc = True, False, max(h[i], maxmin), lo[i], start
        elif not below and result < h[i]:
            first_bar, below, result, maxmin, acc = True, True, min(lo[i], maxmin), h[i], start
        if not first_bar:
            if below and h[i] > maxmin:
                maxmin, acc = h[i], min(acc + inc, mx)
            elif not below and lo[i] < maxmin:
                maxmin, acc = lo[i], min(acc + inc, mx)
        if below:
            result = min(result, lo[i - 1], *([lo[i - 2]] if i > first else []))
        else:
            result = max(result, h[i - 1], *([h[i - 2]] if i > first else []))
        out[i] = result
    return out


def _supertrend_tv(h, lo, c, n, factor):
    """TradingView's pine_supertrend with ta.atr (Wilder's RMA), plainly; direction as ours: 1 up, −1 down."""
    line, dirs, atr, trs, up_b, lo_b, st = [None] * len(h), [None] * len(h), None, [], None, None, None
    for i in range(len(h)):
        tr = h[i] - lo[i] if i == 0 else max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        prev_atr = atr
        trs.append(tr)
        if len(trs) == n:
            atr = sum(trs) / n
        elif len(trs) > n:
            atr = (atr * (n - 1) + tr) / n
        if atr is None:
            continue
        src = (h[i] + lo[i]) / 2
        upper, lower = src + factor * atr, src - factor * atr
        prev_lower, prev_upper = lo_b or 0.0, up_b or 0.0
        lower = lower if lower > prev_lower or c[i - 1] < prev_lower else prev_lower
        upper = upper if upper < prev_upper or c[i - 1] > prev_upper else prev_upper
        if prev_atr is None:
            d = 1
        elif st == prev_upper:
            d = -1 if c[i] > upper else 1
        else:
            d = 1 if c[i] < lower else -1
        st = lower if d == -1 else upper
        lo_b, up_b = lower, upper
        line[i], dirs[i] = st, float(-d)
    return line, dirs


def test_sar_and_supertrend_follow_tradingviews_published_algorithms():
    df = walk(400, seed=11)
    h, lo, c = (df[k].to_list() for k in ("high", "low", "close"))
    _, v, _ = series("S:SAR(10,2,20);\nST:SUPERTREND(10,3).LINE;\nD:SUPERTREND(10,3).DIR;", df)
    assert close_enough(v["S"], _sar_tv(h, lo, c, 10, 0.02, 0.02, 0.2))
    line, dirs = _supertrend_tv(h, lo, c, 10, 3.0)
    assert close_enough(v["ST"], line) and v["D"] == dirs
    assert {d for d in v["D"] if d is not None} == {1.0, -1.0}                  # it does turn, both ways, on this walk
    up = [i for i, d in enumerate(v["D"]) if d == 1.0]
    assert all(v["ST"][i] <= c[i] for i in up)                                   # up: the line is under the close


def test_a_rule_reads_the_new_functions_too_and_pre_is_the_close_before():
    df = frame(close=[10.0, 10.5, 10.2]).with_columns(pre=frame(close=[10.0, 10.5, 10.2])["close"].shift(1))
    comp = Compiler("CN")
    comp.add("x", "ROUND(C / PRE * 100)")
    assert evaluate(df, comp.program())["x"].to_list() == [None, 105.0, 97.0]
    with pytest.raises(Exception, match="要是数值"):
        Compiler("CN").add("x", "REF(C, C > O)")


# review 10-13 (indicators)
def test_a_parameter_named_like_a_field_is_refused():
    """A parameter C silently made every C in the formula the number (B:C; drew 5, not the close)."""
    c = compile_script("B:C;\nA:MA(CLOSE,N);", "CN", {"C": 5, "N": 5})
    assert c.program is None and any("参数 C" in e["message"] for e in c.errors)
    text = "name: x\npane: sub\nparams:\n  - {id: CLOSE, value: 5}\nformula: |2\n  A:MA(CLOSE,20);\n"
    try:
        load_indicator(text)
        raise AssertionError("a parameter named CLOSE was accepted")
    except IndicatorError as e:
        assert "参数 CLOSE" in str(e)


# review 10-13 (indicators)
def test_tdx_s_own_sar_formula_is_dots_over_the_candles():
    """通达信's SAR text: SAR(N,S,M),CIRCLEDOT,COLORSTICK; — drawn as red / green columns from 0 in a pane of its own."""
    c = compile_script("SAR(N,S,M),CIRCLEDOT,COLORSTICK;", "CN", {"N": 10, "S": 2, "M": 20})
    assert c.program is not None and c.items[0].kind == "dots" and c.price_only


# x / 0 in an indicator is 0 as 通达信 draws it, the user chose (10-13): a one-price day (H = L) blanked an N-bar SUM over
# (C − O) / (H − L) — 通达信's WVAD on 920139 was blank on 518 of ~2,270 days. Rules (strict) keep it unknown.
def one_price_bars():
    n = 40
    c = [10 + 0.1 * i for i in range(n)]
    h = [x * 1.02 for x in c]
    lo = [x * 0.98 for x in c]
    h[20] = lo[20] = c[20]                      # a one-price day
    return pl.DataFrame({"listing_id": "CN:000001", "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
                         "open": [x * 0.99 for x in c], "high": h, "low": lo, "close": c, "volume": [1e6] * n})


def test_an_indicator_s_division_by_zero_is_zero_as_tdx_draws_it():
    c = compile_script("W:SUM((CLOSE-OPEN)/(HIGH-LOW)*VOL,24)/10000;", "CN", {})
    out = evaluate(one_price_bars(), c.program)
    assert out["o0"][23:].null_count() == 0


def test_a_rule_s_division_by_zero_stays_unknown():
    comp = Compiler("CN")
    comp.add("x", "(C - O) / (H - L)")
    out = evaluate(one_price_bars(), comp.program())
    assert out["x"][20] is None

