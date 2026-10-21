"""交易计划 (design 2026-10-09 §3): the plan section, `judge` read as a plan, the default, and the checks."""
import pytest

from wb.engine.l3.match import compile_errors
from wb.engine.l3.rule import Plan, RuleError, load_rule

BASE = """
id: p
name: 计划
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
"""


def test_a_plan_reads_with_its_defaults():
    r = load_rule(BASE + """plan:
  stop: [{line: "box.bottom * 0.985", label: 箱底}]
  take: [{line: "box.top * 1.2", share: 0.5}]
  exit: [{when: "C < MA(C, 5)", label: 跌破 5 日线}]
""")
    p = r.trade_plan
    assert p.entry == "close" and p.fills == "close" and p.days == 60 and p.stall is None
    assert [x.line for x in p.stop] == ["box.bottom * 0.985"] and p.take[0].share == 0.5 and p.exit[0].share == 1.0


def test_judge_is_read_as_a_plan_and_no_judge_is_twenty_days():
    p = load_rule(BASE + 'judge: {fail: "box.bottom * 0.985", target: "box.top * 1.3", days: 30}\n').trade_plan
    assert (p.entry, p.fills, p.days) == ("open", "open", 30)
    assert [x.line for x in p.stop] == ["box.bottom * 0.985"] and [(x.line, x.share) for x in p.take] == [("box.top * 1.3", 1.0)]
    assert load_rule(BASE).trade_plan == Plan(entry="open", days=20, fill="open")


def test_plan_and_judge_together_are_refused():
    with pytest.raises(RuleError, match="plan 和 judge"):
        load_rule(BASE + 'judge: {days: 20}\nplan: {days: 20}\n')


def test_an_exit_reads_the_stock_s_own_bars_only():
    with pytest.raises(RuleError, match="卖出条件"):
        load_rule(BASE + 'plan: {exit: [{when: "C < box.bottom"}]}\n')
    with pytest.raises(RuleError, match="卖出条件"):
        load_rule(BASE + 'plan: {exit: [{when: "C < pressure.bottom"}]}\n')


def test_plan_formulas_are_compiled_by_the_check():
    assert compile_errors(load_rule(BASE + 'plan: {exit: [{when: "C < MAA(C, 5)"}]}\n'))
    assert compile_errors(load_rule(BASE + 'plan: {stop: [{line: "MAA(C, 5)"}]}\n'))
    assert compile_errors(load_rule(BASE + 'plan: {stop: [{line: "box.bottom"}], exit: [{when: "C < MA(C, 5)"}]}\n')) == []


def test_the_plan_reads_in_words():
    from wb.engine.l3.understand import as_text, understand
    r = load_rule(BASE + """plan:
  stop: [{line: "box.bottom * 0.985", label: 箱底}]
  take: [{line: "box.top * 1.2", share: 0.5, label: 箱顶上方两成}]
  exit: [{when: "C < MA(C, 5)", label: 跌破 5 日线}]
  stall: 5
  days: 40
""")
    lines = next(s["lines"] for s in as_text(understand(r))["sections"] if s["title"] == "交易计划")
    text = "\n".join(lines)
    assert "信号那天尾盘" in text and "箱底" in text and "卖一半" in text and "跌破 5 日线" in text and "5 个交易日" in text and "40" in text


WATCHED = BASE + "watch_stage: box\n"


def test_a_cross_entry_buys_where_the_watch_list_waits_and_sells_at_the_close():
    # design 2026-10-11 §6: 盘中越线买 — the line is the one 盯盘 watches; sales are decided at the close (14:50)
    p = load_rule(WATCHED + "plan: {entry: cross, max_gap: 0.03, confirm_volume: 1.5}\n").trade_plan
    assert (p.entry, p.fills, p.max_gap, p.confirm_volume) == ("cross", "close", 0.03, 1.5)
    assert load_rule(WATCHED + "plan: {entry: cross}\n").trade_plan.max_gap is None


def test_a_cross_entry_needs_a_watch_list_whose_next_stage_breaks_up():
    with pytest.raises(RuleError, match="越线买"):
        load_rule(BASE + "plan: {entry: cross}\n")                        # no 潜伏 stage: no line to cross
    down = WATCHED.replace("box.break_up", "box.break_down")
    with pytest.raises(RuleError, match="越线买"):
        load_rule(down + "plan: {entry: cross}\n")                        # the next stage breaks down, not up
    with pytest.raises(RuleError):
        load_rule(WATCHED + "plan: {entry: cross, max_gap: -0.01}\n")


def test_a_cross_plan_reads_in_words():
    from wb.engine.l3.understand import as_text, understand
    r = load_rule(WATCHED + "plan: {entry: cross, max_gap: 0.03, confirm_volume: 1.5}\n")
    text = "\n".join(next(s["lines"] for s in as_text(understand(r))["sections"] if s["title"] == "交易计划"))
    assert "第一次碰到" in text and "3%" in text and "1.5" in text and "分钟" in text
