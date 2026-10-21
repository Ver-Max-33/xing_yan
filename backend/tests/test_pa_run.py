"""价格行为分析 (design 2026-10-20 §1, §5.1, §7): one two-stage analysis through our AI client — PA_Agent's own order of
steps and validator, the AI's plan and the final one, the changes, and plain failures that keep what was spent."""
import json

import httpx

import pytest
from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.retry_policy import max_retries_for_category
from pa_agent.util.threading import CancelToken

from tests.pa_fixtures import answers, pa_market, transport
from wb.ai.client import LLM
from wb.pa.frame import daily_input
from wb.pa.run import analyze, prompt_version
from wb.pa.settings import DEFAULT, pa_settings


@pytest.fixture
def inp(paths):
    pa_market(paths)
    return daily_input(paths, "CN:600000")


@pytest.fixture
def llm_with(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    return lambda replies: LLM(paths, transport=transport(replies))


def test_the_fixture_answers_pass_pas_own_validator(inp):
    s1, s2 = answers(inp.frame, kind="limit")
    v = JsonValidator(pa_settings(DEFAULT))
    ok1 = v.validate("stage1", s1, kline_frame=inp.frame)
    assert isinstance(ok1, Ok) and isinstance(v.validate("stage2", s2, kline_frame=inp.frame, stage1_json=ok1.obj,
                                                         decision_stance="balanced", skip_next_bar=True), Ok)


def test_a_limit_plan_both_tracks_equal(inp, llm_with):             # entry < K1 close, TP1 exactly 1R, conf 60
    r = analyze(inp, llm_with(answers(inp.frame, kind="limit")), DEFAULT, cancel=CancelToken())
    assert r.status == "ok" and r.final_plan["order_type"] == "限价单" and r.ai_plan == r.final_plan
    assert len(r.call_ids) == 2 and r.prompt_hash == prompt_version() and r.changes == [] and r.cost > 0
    assert r.stage1["gate_result"] == "proceed" and "next_bar_prediction" not in r.stage2


def test_stage_2_continues_stage_1s_conversation(inp, llm_with):
    llm = llm_with(answers(inp.frame, kind="limit"))
    analyze(inp, llm, DEFAULT, cancel=CancelToken())
    s1, s2 = [json.loads(r[0])["messages"] for r in llm.conn.execute("SELECT request FROM llm_call ORDER BY rowid")]
    assert [m["role"] for m in s1] == ["system", "user"] and [m["role"] for m in s2] == ["system", "user", "assistant", "user"]
    assert s2[:2] == s1 and "涨停价" in s1[1]["content"]                      # DeepSeek's prefix chain, the A-share table


def test_a_2r_target_has_its_stop_widened_and_says_why(inp, llm_with):
    r = analyze(inp, llm_with(answers(inp.frame, kind="limit", r=2.0)), DEFAULT, cancel=CancelToken())
    assert r.final_plan["stop_loss_price"] < r.ai_plan["stop_loss_price"]
    assert any(c["reason"].startswith("第一止盈离买入价") for c in r.changes)


def test_a_short_plan_becomes_no_buy(inp, llm_with):
    r = analyze(inp, llm_with(answers(inp.frame, kind="short")), DEFAULT, cancel=CancelToken())
    assert r.ai_plan["order_direction"] == "做空" and r.final_plan["order_type"] == "不下单"
    assert r.changes[-1]["reason"] == "A 股不能做空"


def test_gate_wait_skips_the_second_call(inp, llm_with):
    r = analyze(inp, llm_with(answers(inp.frame, kind="wait")), DEFAULT, cancel=CancelToken())
    assert r.status == "ok" and len(r.call_ids) == 1 and r.final_plan["order_type"] == "不下单"
    assert r.ai_plan["order_type"] == "不下单" and r.changes == []


def test_stage_2_that_never_validates_fails_with_the_reason_and_the_cost(inp, llm_with):   # Review Focus 2
    s1, _ = answers(inp.frame)
    r = analyze(inp, llm_with([s1, "not json", "not json", "not json", "not json"]), DEFAULT, cancel=CancelToken())
    assert r.status == "failed" and r.error and r.cost > 0 and r.stage1 is not None
    assert len(r.call_ids) == 2 + max_retries_for_category("d", pa_settings(DEFAULT).validation)   # plain text is category d


def test_the_budget_running_out_after_stage_1_fails_plainly(inp, llm_with):                 # Review Focus 2
    llm = llm_with(answers(inp.frame))
    llm.save({**llm.config().model_dump(), "day_cap": 0.0001})                 # the first call spends it all
    r = analyze(inp, llm, DEFAULT, cancel=CancelToken())
    assert r.status == "failed" and "预算" in r.error and len(r.call_ids) == 1 and r.cost > 0


def test_cancel_between_stages(inp, llm_with):
    token = CancelToken()
    r = analyze(inp, llm_with(answers(inp.frame)), DEFAULT, cancel=token,
                on_stage=lambda s: token.set() if s.startswith("第二步") else None)
    assert r.status == "cancelled" and len(r.call_ids) == 1


def test_a_rate_limit_waits_and_asks_again_as_pa_agent_does(inp, paths, monkeypatch):
    # upstream wraps every call in its 429 backoff (5 → 80 s); ours gave up after the client's two quick retries and
    # failed the whole analysis, stage 1 already paid for
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr("pa_agent.ai.rate_limit._sleep_with_cancel", lambda wait, token: False)   # no real waiting
    replies, limited = iter(answers(inp.frame, kind="limit")), iter([True, True, True])   # three 429s: past the client's 2
    def model(request):
        if next(limited, False):
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
    llm = LLM(paths, transport=httpx.MockTransport(model))
    llm.sleep = lambda s: None
    r = analyze(inp, llm, DEFAULT, cancel=CancelToken())
    assert r.status == "ok" and r.final_plan["order_type"] == "限价单"
