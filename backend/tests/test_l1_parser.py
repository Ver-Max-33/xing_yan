import pytest

from wb.engine.l1.ast import At, Attr, Binary, Call, Name, Num, Param, Unary
from wb.engine.l1.parser import ParseError, parse


def test_precedence_and_associativity():
    # * binds tighter than +, comparisons below arithmetic, AND below comparisons, OR lowest
    t = parse("C > MA(C, 20) * 1.02 AND V > 0 OR NOT X")
    assert isinstance(t, Binary) and t.op == "OR"
    left = t.a
    assert left.op == "AND" and left.a.op == ">" and left.a.b.op == "*"
    assert isinstance(t.b, Unary) and t.b.op == "NOT"
    assert parse("1 - 2 - 3") == Binary("-", Binary("-", Num(1), Num(2)), Num(3))


def test_tdx_spellings():
    # 通达信 writes equality as "=" and inequality as "<>"; both are accepted
    assert parse("C = O").op == "==" and parse("C <> O").op == "!="
    assert parse("C == O").op == "==" and parse("C != O").op == "!="
    assert parse("a and not b").op == "AND"  # keywords are case-insensitive


def test_calls_attributes_params_and_stage_values():
    assert parse("MACD.DIF") == Attr(Name("MACD"), "DIF")
    assert parse("MACD(12, 26, 9).DEA") == Attr(Call("MACD", (Num(12), Num(26), Num(9)), ()), "DEA")
    assert parse("box.top") == Attr(Name("box"), "top")
    assert parse("$box_tol") == Param("box_tol")
    assert parse("MEAN(V, last=10)") == Call("MEAN", (Name("V"),), (("last", Num(10)),))
    assert parse("MEAN(V, of=rally)") == Call("MEAN", (Name("V"),), (("of", Name("rally")),))
    assert parse("BAND_VOL_RATIO(box)@breakout") == At(Call("BAND_VOL_RATIO", (Name("box"),), ()), "breakout")
    assert parse("MA_ALIGN([5, 10, 20, 60], up)") == Call("MA_ALIGN", (Num((5, 10, 20, 60)), Name("up")), ())
    assert parse("-C") == Unary("-", Name("C"))
    assert parse("1.5e-3") == Num(0.0015)


def test_errors_point_at_the_character():
    with pytest.raises(ParseError, match="第 10 个字符"):
        parse("C > MA(C 20)")
    with pytest.raises(ParseError, match="没有写完"):
        parse("C > ")
    with pytest.raises(ParseError, match="不认识的字符"):
        parse("C > 3 ; ")
    with pytest.raises(ParseError, match="括号"):
        parse("(C > 3")


def test_equals_inside_call_arguments_is_equality():
    # 通达信 writes equality as `=`, also as an argument: only a name that is not a field or a
    # function (last=, of=) is a keyword argument
    assert parse("IF(C = O, 1, 0)") == Call("IF", (Binary("==", Name("C"), Name("O")), Num(1), Num(0)), ())
    assert parse("COUNT(close = open, 5)").args[0] == Binary("==", Name("close"), Name("open"))
    assert parse("IF(IS_ST = 0, 1, 0)").args[0] == Binary("==", Name("IS_ST"), Num(0))
    assert parse("MEAN(V, last=10)").kwargs == (("last", Num(10)),)


def test_an_option_may_be_written_with_a_colon_and_a_stray_colon_says_how_to_write_one():
    c = parse("MEAN(V, of: rally, last: 10)")                       # what models (and people writing YAML) tend to write
    assert [k for k, _ in c.kwargs] == ["of", "last"] and c.kwargs[0][1].name == "rally"
    with pytest.raises(ParseError, match="名字=值"):
        parse("C : O")
