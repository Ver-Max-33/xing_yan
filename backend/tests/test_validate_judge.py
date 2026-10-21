"""A rule's `judge` (spec §9.1, read as a trade plan since 2026-10-11 — the matching is tested in test_validate_trades):
its lines are checked like any expression when the rule loads."""
import pytest

from wb.engine.l3.rule import RuleError, load_rule


def test_a_rule_says_how_its_signals_are_judged():
    text = """
id: z
name: 下品拐点
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
judge: {fail: "g.small_bottom", target: "g.big_top", days: 60}
"""
    r = load_rule(text)
    assert (r.judge.fail, r.judge.target, r.judge.days) == ("g.small_bottom", "g.big_top", 60)
    with pytest.raises(RuleError, match="没有叫 nope 的阶段"):
        load_rule(text.replace("g.small_bottom", "nope.bottom"))
