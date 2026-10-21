"""规则助手 (spec §6.1–§6.4): the prompt comes from the registry and the template cards, the answer is checked like
any rule (errors go back to the model, at most twice), assumptions and unmapped phrases are applied by the
program without calling AI again, and an edit comes back as the whole new rule with its difference shown."""
import json

import httpx
import pytest

from wb.ai.client import LLM, AIError
from wb.ai.tasks.rule_assistant import apply_choices, draft, edit, system_prompt

GOOD = """id: shrink_breakout
name: 横盘缩量后放量突破
markets: [CN, JP]
params:
  - {id: vol, value: 2.0, range: [1.5, 3.0], step: 0.25, tune: true}
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - id: up
    kind: event
    on: box.break_up
    at:
      - {id: volume, expr: "VOL_RATIO(5) >= $vol"}
signal_at: up.at
watch_stage: box
"""


def envelope(rule=GOOD, assumptions=(), unmapped=(), note=""):
    return json.dumps({"rule": rule, "assumptions": list(assumptions), "unmapped": list(unmapped), "note": note}, ensure_ascii=False)


SCALE = {"id": "box_scale", "text": "你说的横盘一两个月，按中箱识别", "path": "stages.box.with.scale", "chosen": "mid",
         "options": [{"value": "short", "label": "小箱"}, {"value": "mid", "label": "中箱"}, {"value": "long", "label": "大箱"}]}
WASH = {"phrase": "主力洗盘", "reason": "无法直接计算", "proxies": [
    {"label": "缩量 + 长下影", "stage": "box", "where": "during", "expr": "COUNT(L < MIN(O, C) * 0.97) >= 2"}]}


def scripted(paths, monkeypatch, *answers):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    sent = []

    def route(request):
        sent.append(json.loads(request.content))
        a = answers[min(len(sent), len(answers)) - 1]
        return httpx.Response(200, json={"choices": [{"message": {"content": a}}], "usage": {"prompt_tokens": 9000, "completion_tokens": 600}})
    return LLM(paths, transport=httpx.MockTransport(route)), sent


def test_the_prompt_is_built_from_the_registry_and_the_template_cards():
    s = system_prompt()
    assert "json" in s.lower()
    assert "BAND_VOL_RATIO" in s and "MA_SLOPE_PCT(N, K=5)" in s and "### BOX" in s and "context" in s
    assert "break_up" in s and "limit_hit_top" in s                              # the events a stage can hang on
    assert "收盘跌破箱底之后缩量，很快又收回到箱子里" in s and "id: tpl_xia_po_hui_xiang" in s   # a card's words → its rule
    assert "级别" in s and "撞顶涨停" in s                                        # §6.2 must-ask points
    assert "不输出买入、卖出、持有、仓位、目标价、止损价之类的建议" in s           # no trading advice (a quoted description may say 买入)


def test_a_draft_comes_back_checked_with_its_reading_cards_and_unmapped_phrases(paths, monkeypatch):
    llm, sent = scripted(paths, monkeypatch, envelope(assumptions=[SCALE], unmapped=[WASH]))
    d = draft(llm, "先横盘一两个月，期间缩量、主力洗盘，然后放量突破箱顶")
    assert d["rule_text"].startswith("id: shrink_breakout") and d["understanding"]["summary"].startswith("横盘缩量后放量突破")
    assert [o["value"] for o in d["assumptions"][0]["options"]] == ["short", "mid", "long"]
    assert d["unmapped"][0]["phrase"] == "主力洗盘" and d["attempts"] == 1
    user = sent[0]["messages"][1]["content"]
    assert "先横盘一两个月" in user and sent[0]["response_format"] == {"type": "json_object"}


def test_a_rule_that_does_not_load_goes_back_with_the_reasons_at_most_twice(paths, monkeypatch):
    bad = GOOD.replace("VOL_RATIO(5)", "NO_SUCH_FN(5)")
    llm, sent = scripted(paths, monkeypatch, envelope(rule=bad), envelope(rule=bad), envelope())
    d = draft(llm, "放量突破")
    assert d["attempts"] == 3 and len(sent) == 3
    assert "NO_SUCH_FN" in sent[1]["messages"][-1]["content"]                     # the loader's own reason went back
    llm, _ = scripted(paths, monkeypatch, envelope(rule=bad))
    with pytest.raises(AIError) as e:
        draft(llm, "放量突破")
    assert "NO_SUCH_FN" in str(e.value) and "NO_SUCH_FN" in e.value.raw           # shown as it came, not dropped


def test_an_assumption_whose_options_do_not_apply_is_sent_back(paths, monkeypatch):
    wrong = {**SCALE, "path": "stages.nope.with.scale"}
    llm, sent = scripted(paths, monkeypatch, envelope(assumptions=[wrong]), envelope(assumptions=[SCALE]))
    d = draft(llm, "横盘后突破")
    assert d["attempts"] == 2 and "stages.nope" in sent[1]["messages"][-1]["content"]


def test_choices_and_proxies_are_applied_by_the_program_without_ai():
    text = apply_choices(GOOD, [{"path": "stages.box.with.scale", "value": "long"}, {"path": "params.vol.value", "value": "2.5"}],
                         [{"stage": "box", "where": "during", "expr": "COUNT(L < MIN(O, C) * 0.97) >= 2"}])
    from wb.engine.l3.rule import load_rule
    r = load_rule(text)
    assert r.stage("box").with_["scale"] == "long" and r.param("vol") == 2.5
    assert [c.expr for c in r.stage("box").during] == ["COUNT(L < MIN(O, C) * 0.97) >= 2"]
    with pytest.raises(ValueError, match="stages.zz"):
        apply_choices(GOOD, [{"path": "stages.zz.with.scale", "value": "long"}])
    # a filter 条件体检 suggests (候选条件归因) comes in with a name of its own
    named = load_rule(apply_choices(GOOD, add=[{"stage": "up", "where": "at", "expr": "MA_DIST(20) > 0.05", "id": "f_dist20"}]))
    assert [(c.id, c.expr) for c in named.stage("up").at][-1] == ("f_dist20", "MA_DIST(20) > 0.05")
    # adopting the same feature again (a later check-up's threshold): the one condition is updated, never doubled
    again = load_rule(apply_choices(named_text := apply_choices(GOOD, add=[{"stage": "up", "where": "at", "expr": "MA_DIST(20) > 0.05", "id": "f_dist20"}]),
                                    add=[{"stage": "up", "where": "at", "expr": "MA_DIST(20) > 0.08", "id": "f_dist20"}]))
    assert [(c.id, c.expr) for c in again.stage("up").at if c.id == "f_dist20"] == [("f_dist20", "MA_DIST(20) > 0.08")] and named_text


def test_an_edit_returns_the_whole_new_rule_with_its_note_and_the_difference(paths, monkeypatch):
    new = GOOD.replace("  - {id: box, kind: structure, use: BOX, with: {scale: mid}}\n",
                       "  - id: box\n    kind: structure\n    use: BOX\n    with: {scale: mid}\n    during:\n"
                       "      - {id: no_limit, expr: \"COUNT(LIMIT_UP_CLOSE) == 0\"}\n")
    llm, sent = scripted(paths, monkeypatch, envelope(rule=new, note="箱体期间不能有涨停：加了一个期间条件"))
    e = edit(llm, GOOD, "箱体期间不能有涨停", "你在样例图集里标了：对 3、错 1")
    assert e["note"].startswith("箱体期间不能有涨停") and "+      - {id: no_limit" in e["diff"]
    user = sent[0]["messages"][1]["content"]
    assert "箱体期间不能有涨停" in user and "id: shrink_breakout" in user and "对 3、错 1" in user


def test_a_change_to_the_last_line_shows_as_a_removed_and_an_added_line(paths, monkeypatch):
    old = GOOD.replace("signal_at: up.at\nwatch_stage: box\n", "watch_stage: box\nsignal_at: up.at\n")
    llm, _ = scripted(paths, monkeypatch, envelope(rule=old.replace("signal_at: up.at", "signal_at: up.end")))
    e = edit(llm, old, "信号放在突破那一段结束的时候")
    assert "-signal_at: up.at\n+signal_at: up.end\n" in e["diff"]


def test_the_confirmed_option_is_one_of_the_options_and_what_the_rule_holds(paths, monkeypatch):
    stray = {**SCALE, "chosen": "huge"}                               # not one of the options
    other = {**SCALE, "chosen": "short"}                              # an option, but the rule says mid
    llm, sent = scripted(paths, monkeypatch, envelope(assumptions=[stray]), envelope(assumptions=[other]), envelope(assumptions=[SCALE]))
    d = draft(llm, "横盘一两个月后突破")
    assert d["attempts"] == 3
    assert "huge" in sent[1]["messages"][-1]["content"] and "mid" in sent[2]["messages"][-1]["content"]


def test_the_prompt_says_what_the_checks_ask_so_a_draft_needs_fewer_tries():
    s = system_prompt()
    assert "all / any 写成一条公式文字" in s and "during 里的条件都要用汇总" in s
    assert "同一个说法不要既写进 assumptions 又写进 unmapped" in s and "每个参数都要在规则里用到" in s


def test_the_prompt_says_a_window_length_can_be_a_parameter():
    s = system_prompt()
    assert "时间窗的根数（bars、gap、within）可以写成整数参数" in s and "只能写数字" not in s


def test_a_path_written_with_an_index_is_told_to_name_the_condition(paths, monkeypatch):
    by_index = {**SCALE, "id": "vol", "path": "stages.up.at[0].expr", "chosen": "VOL_RATIO(5) >= $vol",
                "options": [{"value": "VOL_RATIO(5) >= $vol", "label": "量比"}]}
    llm, sent = scripted(paths, monkeypatch, envelope(assumptions=[by_index]), envelope(assumptions=[SCALE]))
    d = draft(llm, "放量突破")
    assert d["attempts"] == 2
    told = sent[1]["messages"][-1]["content"]
    assert "按 id 写" in told and "stages.up.at.volume.expr" in told and "None" not in told


def test_the_prompt_names_every_place_a_card_can_change():
    s = system_prompt()
    assert "stages.<阶段id>.during.<条件id>.expr" in s and "stages.<阶段id>.all" in s and "不用下标" in s


def test_the_prompt_writes_a_trade_plan_and_its_example_loads():
    # 交易计划 (design 2026-10-09 §3): the assistant drafts `plan`, not the older `judge`; the grammar's example is a rule
    from wb.ai.tasks.rule_assistant import GRAMMAR
    p = system_prompt()
    assert "plan:" in GRAMMAR and "box.false_bottom" in GRAMMAR and "C < MA(C, 5)" in GRAMMAR and "judge: {fail" not in GRAMMAR
    assert GRAMMAR in p
    from wb.engine.l3.rule import load_rule
    r = load_rule("""
id: x
name: 例子
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: up, kind: event, on: box.break_up}
signal_at: up.at
plan:
  entry: close
  stop: [{line: "box.false_bottom", label: 假底}, {line: "box.bottom * 0.985", label: 箱底}]
  take: [{line: "pressure.bottom", share: 0.5, label: 左侧压力}]
  exit: [{when: "C < MA(C, 5)", label: 跌破 5 日线}]
  days: 60
""")
    assert r.trade_plan.take[0].share == 0.5
