"""个股研究 (spec §10.3): the model reads the evidence pack and answers in a fixed shape — a summary, what supports and what
argues against (each with the facts it rests on, an inference marked as one), what to check, what would show it wrong,
when to look again, which way the evidence leans, and how this research relates to the last one (同向 / 反手 / 失效);
never a rating, a target, a position or a stop. A citation of a fact the pack does not have goes back once with the
reason; numbers no cited fact holds, a fact dated after the pack's day, or advice to trade mark the answer 需复核 —
kept, never shown as a conclusion."""
import json

import httpx

from wb.ai.client import LLM
from wb.ai.tasks.research import research

PACK = {"listing_id": "CN:600519", "name": "贵州茅台", "market": "CN", "day": "2026-10-02", "left_out": [], "hash": "h1",
        "facts": [
            {"id": "px.close", "kind": "px", "source": "tushare", "as_of": "2026-10-02", "text": "收盘 1,450.00（2026-10-02，交易所原始价）", "value": 1450.0},
            {"id": "px.chg20", "kind": "px", "source": "tushare", "as_of": "2026-10-02", "text": "近 20 个交易日涨跌 +5.3%", "value": 0.053},
            {"id": "box.long", "kind": "box", "source": "tushare", "as_of": "2026-09-01",
             "text": "最近一个已成立的大箱（2026-05-11 起，到这天还没结束）：上沿 1,500.00、下沿 1,380.00（宽 8.7%）", "value": 1500.0},
            {"id": "fin.2026H1", "kind": "fin", "source": "tushare", "as_of": "2026-08-18",
             "text": "2026 半年报（2026-08-15 公告，累计）：营收 907.03 亿元，归母净利 445.17 亿元，同比 -2.0%", "value": 4.4517e10}]}


def answer(**over):
    base = {"summary": "价格在大箱上沿 1,500.00 下方整理，半年报净利同比 -2.0%。",
            "support": [{"text": "近 20 个交易日涨 +5.3%，收盘 1,450.00 靠近上沿 1,500.00", "refs": ["px.chg20", "px.close", "box.long"], "inference": False}],
            "against": [{"text": "半年报归母净利同比 -2.0%", "refs": ["fin.2026H1"], "inference": False}],
            "to_verify": ["分红方案是否变化"], "invalidation": [{"text": "收盘跌破下沿 1,380.00", "refs": ["box.long"], "inference": False}],
            "next_review": ["收盘越过 1,500.00 时"], "lean": "相当", "continuity": {"verdict": "首次", "text": "", "refs": []}}
    return json.dumps({**base, **over}, ensure_ascii=False)


def scripted(paths, monkeypatch, *answers):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    sent = []

    def route(request):
        sent.append(json.loads(request.content))
        a = answers[min(len(sent), len(answers)) - 1]
        return httpx.Response(200, json={"choices": [{"message": {"content": a}}], "usage": {"prompt_tokens": 3000, "completion_tokens": 500}})
    return LLM(paths, transport=httpx.MockTransport(route)), sent


def test_a_research_answers_from_the_pack_citing_its_facts(paths, monkeypatch):
    llm, sent = scripted(paths, monkeypatch, answer())
    r = research(llm, PACK)
    assert r["status"] == "ok" and r["flags"] == [] and r["output"]["support"][0]["refs"] == ["px.chg20", "px.close", "box.long"]
    system, user = sent[0]["messages"][0]["content"], sent[0]["messages"][1]["content"]
    assert "[px.close]" in user and "2026-10-02" in user and "目标价" in system and "json" in (system + user).lower()
    assert "日文资料用中文转述" in system                                          # §10.3 日文资料：用中文解释


def test_a_citation_the_pack_does_not_have_goes_back_once(paths, monkeypatch):
    llm, sent = scripted(paths, monkeypatch, answer(support=[{"text": "开盘跳空", "refs": ["px.open"], "inference": False}]), answer())
    r = research(llm, PACK)
    assert len(sent) == 2 and r["status"] == "ok" and "px.open" in sent[1]["messages"][-1]["content"]


def test_numbers_no_fact_holds_or_advice_to_trade_mark_it_for_review(paths, monkeypatch):
    llm, _ = scripted(paths, monkeypatch, answer(support=[{"text": "收盘 1,650.00 已经突破上沿", "refs": ["px.close", "box.long"], "inference": False}],
                                                  next_review=["建议在 1,400.00 附近买入"]))
    r = research(llm, PACK)
    assert r["status"] == "需复核" and any("1,650.00" in f for f in r["flags"]) and any("买入" in f for f in r["flags"])


def test_a_source_left_out_is_said_to_the_model(paths, monkeypatch):
    llm, sent = scripted(paths, monkeypatch, answer())
    research(llm, {**PACK, "left_out": ["J-Quants"]})
    assert "资料不足" in sent[0]["messages"][1]["content"] and "J-Quants" in sent[0]["messages"][1]["content"]


PREV = {"day": "2026-09-01", "output": {"summary": "大箱里整理，等突破。", "lean": "支持",
                                        "invalidation": [{"text": "收盘跌破下沿 1,380.00", "refs": ["box.long"], "inference": False}]}}


def test_an_inference_is_kept_marked_and_still_cites_its_facts(paths, monkeypatch):
    guess = {"text": "如果净利继续下滑，可能拖累估值", "refs": ["fin.2026H1"], "inference": True}
    llm, sent = scripted(paths, monkeypatch, answer(against=[{**guess, "refs": []}]), answer(against=[guess]))
    r = research(llm, PACK)
    assert len(sent) == 2 and r["output"]["against"] == [guess] and r["status"] == "ok"       # an inference cites too


def test_the_first_research_says_so(paths, monkeypatch):
    llm, sent = scripted(paths, monkeypatch, answer(continuity={"verdict": "同向", "text": "和上次一样", "refs": []}), answer())
    assert research(llm, PACK)["output"]["continuity"]["verdict"] == "首次" and len(sent) == 2
    assert "首次" in sent[1]["messages"][-1]["content"]


def test_with_a_last_research_the_model_is_told_what_it_said_and_says_how_this_one_relates(paths, monkeypatch):
    same = {"verdict": "同向", "text": "仍在大箱里，失效条件没有发生", "refs": ["box.long", "px.close"]}
    llm, sent = scripted(paths, monkeypatch, answer(lean="支持"), answer(lean="支持", continuity=same))
    r = research(llm, PACK, PREV)
    user = sent[0]["messages"][1]["content"]
    assert "上次研究（2026-09-01）" in user and "大箱里整理，等突破。" in user and "收盘跌破下沿 1,380.00" in user and "支持" in user
    assert len(sent) == 2 and r["output"]["continuity"] == same                         # 首次 with a last one went back


def test_a_reversal_is_a_changed_lean_and_says_why(paths, monkeypatch):
    flip = {"verdict": "反手", "text": "半年报净利转为下滑", "refs": ["fin.2026H1"]}
    llm, sent = scripted(paths, monkeypatch, answer(lean="支持", continuity=flip), answer(lean="反对", continuity=flip))
    r = research(llm, PACK, PREV)
    assert len(sent) == 2 and "倾向" in sent[1]["messages"][-1]["content"] and r["output"]["lean"] == "反对"


def test_a_cited_fact_dated_after_the_packs_day_marks_it_for_review(paths, monkeypatch):
    late = {"id": "ann.9", "kind": "ann", "source": "cninfo", "as_of": "2026-10-05", "text": "2026-10-05 公告：回购", "value": None}
    llm, _ = scripted(paths, monkeypatch, answer(support=[{"text": "公司公告回购", "refs": ["ann.9"], "inference": False}]))
    r = research(llm, {**PACK, "facts": [*PACK["facts"], late]})
    assert r["status"] == "需复核" and any("晚于" in f for f in r["flags"])


def test_an_answer_of_the_wrong_shape_goes_back_once_never_a_crash_nor_a_record(paths, monkeypatch):
    # DeepSeek's JSON mode does not hold the model to the schema: a string where a list belongs, a number where text
    # belongs, go back with the reason (§10.1), and a second wrong one is an AIError the page shows, never a 500
    import pytest

    from wb.ai.client import AIError
    llm, sent = scripted(paths, monkeypatch, answer(support="无", to_verify="分红"), answer())
    r = research(llm, PACK)
    assert len(sent) == 2 and r["status"] == "ok" and "support" in sent[1]["messages"][-1]["content"]
    llm, _ = scripted(paths, monkeypatch, answer(against=[{"text": 1450, "refs": ["px.close"], "inference": False}]))
    with pytest.raises(AIError, match="against"):
        research(llm, PACK)


def test_your_own_notes_are_cited_whenever_you_wrote_them(paths, monkeypatch):
    # a note written after the data's day (an evening, a weekend) is yours, not data known later
    note = {"id": "note.reason", "kind": "note", "source": "user", "as_of": "2026-10-04", "text": "你写的关注理由：箱体干净", "value": None}
    llm, _ = scripted(paths, monkeypatch, answer(support=[{"text": "你关注它是因为箱体干净", "refs": ["note.reason"], "inference": False}]))
    assert not any("晚于" in f for f in research(llm, {**PACK, "facts": [*PACK["facts"], note]})["flags"])


def test_dates_and_a_sign_said_in_words_are_not_numbers_out_of_thin_air(paths, monkeypatch):
    # "2026-08-15" holds 8 and 15, not -8 and -15; "同比 -2.0%" said as "下滑 2.0%" is the same number
    llm, _ = scripted(paths, monkeypatch, answer(against=[{"text": "8 月 15 日公告的半年报净利同比下滑 2.0%", "refs": ["fin.2026H1"],
                                                            "inference": False}]))
    assert research(llm, PACK)["flags"] == []


def test_a_trade_word_quoted_from_the_evidence_is_not_advice(paths, monkeypatch):
    rating = {"id": "ann.5", "kind": "ann", "source": "cninfo", "as_of": "2026-09-01",
              "text": "2026-09-01 公告：关于主体信用评级结果的公告", "value": None}
    llm, _ = scripted(paths, monkeypatch, answer(support=[{"text": "公司发布了关于主体信用评级结果的公告", "refs": ["ann.5"], "inference": False}]))
    assert not any("交易建议" in f for f in research(llm, {**PACK, "facts": [*PACK["facts"], rating]})["flags"])
    llm, _ = scripted(paths, monkeypatch, answer(next_review=["给出买入评级之后"]))       # not a quote: still advice
    assert any("交易建议（买入）" in f for f in research(llm, PACK)["flags"])


def test_full_width_figures_in_japanese_material_are_the_same_numbers(paths, monkeypatch):
    # an imported 決算 text writes １２，３４５; the model writes 12,345
    deck = {"id": "ir.ab12", "kind": "ir", "source": "user", "as_of": "2026-10-01", "text": "你导入的资料《決算説明》第 3 页：売上高１２，３４５億円",
            "value": None}
    llm, _ = scripted(paths, monkeypatch, answer(support=[{"text": "売上高约 12,345 亿日元", "refs": ["ir.ab12"], "inference": False}]))
    assert research(llm, {**PACK, "facts": [*PACK["facts"], deck]})["flags"] == []
