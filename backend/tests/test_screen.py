"""The 选股 funnel's stage labels (spec §7.1): each stage by its name, as everywhere (names())."""
from wb.engine.l3.rule import load_rule
from wb.screening.screen import stage_labels
from wb.workspace.rules import builtin_rules


def test_a_stage_reads_by_its_name_in_the_funnel_and_a_formula_stage_too():
    # the funnel names a stage as everything else does (names(): its label, or what it is) — never by its formula
    rule, _ = builtin_rules()["box_breakout_after_rally"]
    assert [s["label"] for s in stage_labels(rule)][1:3] == ["中箱", "向上突破"]
    streak = [s["label"] for s in stage_labels(builtin_rules()["limit_streak"][0])]
    assert "LIMIT_UP_STREAK" not in streak[0]
    one = load_rule("id: x\nname: x\nmarkets: [CN]\nstages:\n  - {id: today, kind: event, when: \"C > MA(C, 20)\"}\nsignal_at: today.at\n")
    [only] = stage_labels(one)
    assert only["label"] == "信号" and only["head"].startswith("①")   # the whole heading stays for the hover
