"""The LLM client (spec §10.1): OpenRouter and DeepSeek through their OpenAI-compatible endpoint, structured output,
one retry with the reason when the answer is not what was asked, every call logged and counted against the budget."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from wb.ai.client import LLM, AIError, AIUnavailable, deepseek_peak

BJ = ZoneInfo("Asia/Shanghai")
PEAK = datetime(2026, 10, 9, 10, 0, tzinfo=BJ)          # a Friday 10:00 Beijing time: DeepSeek's peak hours

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def answer(content, prompt=120, completion=30, cost=None, hit=None):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, **({"cost": cost} if cost is not None else {}),
             **({"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit} if hit is not None else {})}
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}], "usage": usage})


def llm(paths, handler):
    sent = []

    def route(request):
        sent.append(request)
        return handler(request, len(sent))
    c = LLM(paths, transport=httpx.MockTransport(route))
    c.clock = lambda: PEAK
    return c, sent


def need_ok(d):
    if d.get("ok") is not True:
        raise ValueError("ok 要是 true")
    return d


def test_without_a_key_ai_is_unavailable_and_says_where_to_add_it(paths, monkeypatch):
    monkeypatch.delenv("WB_SECRET_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("WB_ENV_FILE", str(paths.root / "none.env"))
    monkeypatch.setattr("keyring.get_password", lambda *a: None)          # whatever this machine's keychain holds
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}'))
    st = c.status()["tasks"]["rule_assistant"]
    assert st["ready"] is False and "DeepSeek" in st["reason"]
    with pytest.raises(AIUnavailable, match="API key"):
        c.chat_json("rule_assistant", "回复 json", "hi", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert sent == []


def test_deepseek_asks_for_a_json_object_thinking_as_hard_as_the_task_says(paths, monkeypatch):
    # DeepSeek's API since V4 (api-docs.deepseek.com): deepseek-flash / deepseek-v4-pro, thinking on by default, its
    # strength by reasoning_effort (none / low / high / max); deepseek-chat and deepseek-reasoner stopped on 2026-07-24
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}', prompt=1_000_000, completion=500_000, hit=400_000))
    out = c.chat_json("rule_assistant", "只输出 json", "hi", SCHEMA, "ok", need_ok, template_hash="t1", evidence_hash="e1")
    assert out.value == {"ok": True} and out.replayed is False
    body = json.loads(sent[0].content)
    assert sent[0].url == "https://api.deepseek.com/chat/completions" and sent[0].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "deepseek-v4-pro" and body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high" and "temperature" not in body
    [row] = c.calls()
    assert (row["task"], row["model"], row["ok"], row["tokens_in"], row["tokens_out"]) == ("rule_assistant", "deepseek-v4-pro", 1, 1_000_000, 500_000)
    # peak prices: ¥0.30 / M cache hits, ¥9 / M misses, ¥27 / M out
    assert row["cost"] == pytest.approx(0.4 * 0.30 + 0.6 * 9.0 + 0.5 * 27.0)
    assert "sk-test" not in json.dumps(row, ensure_ascii=False)             # the key is never logged


def test_thinking_off_and_each_strength_reach_deepseek_as_its_api_writes_them(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}'))
    tasks = c.config().model_dump()["tasks"]
    for effort in ("none", "low", "max"):
        c.save({**c.config().model_dump(), "tasks": {**tasks, "research": {"provider": "deepseek", "model": "deepseek-flash", "effort": effort}}})
        c.chat_json("research", "json", "u", SCHEMA, "ok", need_ok, template_hash=effort, evidence_hash="e")
    off, low, top = (json.loads(r.content) for r in sent)
    assert off["thinking"] == {"type": "disabled"} and "reasoning_effort" not in off and off["temperature"] == 0
    assert (low["thinking"], low["reasoning_effort"]) == ({"type": "enabled"}, "low") and top["reasoning_effort"] == "max"


def test_off_peak_deepseek_calls_cost_half(paths, monkeypatch):
    # 北京时间周一至周五（不含法定节假日）9:00–12:00、14:00–18:00 为高峰时段，其余时段半价 (DeepSeek 模型 & 价格)
    assert deepseek_peak(PEAK) and deepseek_peak(datetime(2026, 10, 9, 17, 59, tzinfo=BJ))
    assert not deepseek_peak(datetime(2026, 10, 9, 12, 30, tzinfo=BJ))                    # lunch
    assert not deepseek_peak(datetime(2026, 10, 10, 10, 0, tzinfo=BJ))                    # Saturday
    assert not deepseek_peak(datetime(2026, 10, 9, 1, 0, tzinfo=ZoneInfo("Asia/Tokyo")))  # 00:00 in Beijing
    assert not deepseek_peak(datetime(2026, 10, 1, 10, 0, tzinfo=BJ), holiday=lambda d: d == date(2026, 10, 1))
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, _ = llm(paths, lambda r, n: answer('{"ok": true}', prompt=1_000_000, completion=0, hit=0))
    c.clock = lambda: datetime(2026, 10, 10, 10, 0, tzinfo=BJ)
    c.chat_json("research", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert c.calls()[0]["cost"] == pytest.approx(2.0 / 2)                                  # deepseek-flash, a Saturday


def test_a_retired_deepseek_model_moves_to_what_it_became_never_to_a_dearer_one(paths):
    # DeepSeek's own mapping while the names were phased out: chat → flash without thinking, reasoner → flash thinking
    c, _ = llm(paths, lambda r, n: answer('{"ok": true}'))
    c.conn.execute("INSERT OR REPLACE INTO setting VALUES ('ai', ?)", (json.dumps({
        "tasks": {"rule_assistant": {"provider": "deepseek", "model": "deepseek-chat"},
                  "research": {"provider": "deepseek", "model": "deepseek-reasoner"}},
        "day_cap": 3, "month_cap": 30, "usd_cny": 7.1,
        "prices": {"deepseek-chat": {"input": 2.0, "output": 8.0}, "deepseek-reasoner": {"input": 4.0, "output": 16.0},
                   "deepseek-flash": {"input": 1.5, "output": 6.0}}}),))
    cfg = c.config()
    assert (cfg.tasks["rule_assistant"].model, cfg.tasks["rule_assistant"].effort) == ("deepseek-flash", "none")
    assert (cfg.tasks["research"].model, cfg.tasks["research"].effort) == ("deepseek-flash", "high")
    assert cfg.day_cap == 3                                                              # the rest is yours, as it was
    assert set(cfg.prices) == {"deepseek-flash", "deepseek-v4-pro"} and cfg.prices["deepseek-flash"].input == 1.5
    # typed and saved today, it is moved the same way at once: what the page shows is what runs
    saved = c.save({**cfg.model_dump(), "tasks": {**cfg.model_dump()["tasks"], "rule_assistant": {"provider": "deepseek", "model": "deepseek-chat", "effort": "low"}}})
    assert (saved.tasks["rule_assistant"].model, saved.tasks["rule_assistant"].effort) == ("deepseek-flash", "low")


def test_a_config_naming_one_task_keeps_the_others_at_their_defaults(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}'))
    c.save({**c.config().model_dump(), "tasks": {"rule_assistant": {"provider": "openrouter", "model": "a/b"}}})
    assert c.config().tasks["research"].model == "deepseek-flash"
    c.chat_json("research", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")   # not a KeyError
    assert len(sent) == 1


def test_a_price_of_nothing_is_refused_so_the_budget_cannot_go_blind(paths):
    from pydantic import ValidationError
    c, _ = llm(paths, lambda r, n: answer('{"ok": true}'))
    for bad in ({"input": 0, "output": 8}, {"input": 2, "output": 0}, {"input": 2, "cached": 0, "output": 8}):
        with pytest.raises(ValidationError):
            c.save({**c.config().model_dump(), "prices": {"deepseek-flash": bad}})


def test_openrouter_gets_the_strength_only_when_one_is_chosen(paths, monkeypatch):
    # some OpenRouter models reject a reasoning setting: 默认 sends none
    monkeypatch.setenv("WB_SECRET_OPENROUTER_API_KEY", "or-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}', cost=0.01))
    for effort in ("default", "low"):
        c.save({**c.config().model_dump(), "tasks": {"rule_assistant": {"provider": "openrouter", "model": "a/b", "effort": effort}}})
        c.chat_json("rule_assistant", "s", "u", SCHEMA, "ok", need_ok, template_hash=effort, evidence_hash="e")
    plain, low = (json.loads(r.content) for r in sent)
    assert "reasoning" not in plain and low["reasoning"] == {"effort": "low", "exclude": True}
    assert plain["temperature"] == 0 and "temperature" not in low          # reasoning models reject a temperature but 1


def test_openrouter_asks_for_the_strict_schema_and_bills_in_dollars(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_OPENROUTER_API_KEY", "or-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}', cost=0.01))
    c.save({**c.config().model_dump(), "tasks": {"rule_assistant": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}}})
    c.chat_json("rule_assistant", "s", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    body = json.loads(sent[0].content)
    assert str(sent[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "ok", "strict": True, "schema": SCHEMA}}
    assert body["provider"] == {"require_parameters": True} and body["usage"] == {"include": True}
    assert c.calls()[0]["cost"] == pytest.approx(0.01 * c.config().usd_cny)


def test_an_answer_that_is_not_what_was_asked_gets_one_more_try_with_the_reason(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": false}' if n == 1 else '{"ok": true}'))
    out = c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert out.value == {"ok": True} and len(sent) == 2
    retry = json.loads(sent[1].content)["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"ok": false}'} and "ok 要是 true" in retry[-1]["content"]
    c2, _ = llm(paths, lambda r, n: answer("不是 JSON"))
    with pytest.raises(AIError, match="不是 JSON") as e:
        c2.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert e.value.raw == "不是 JSON"                                        # shown to you as it came, never silently dropped


def test_the_day_budget_stops_ai_before_it_is_spent(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}', prompt=2_000_000, completion=0))
    c.save({**c.config().model_dump(), "day_cap": 3.0})
    c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")   # ¥18 spent (v4-pro, peak)
    with pytest.raises(AIUnavailable, match="今天"):
        c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e2")
    assert len(sent) == 1 and c.status()["spent"]["day"] == pytest.approx(18.0)


def test_a_wrong_key_and_a_rate_limit_say_so(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, _ = llm(paths, lambda r, n: httpx.Response(401, json={"error": {"message": "invalid"}}))
    with pytest.raises(AIError, match="key"):
        c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    c, sent = llm(paths, lambda r, n: httpx.Response(429, json={}))
    c.sleep = lambda s: None
    with pytest.raises(AIError, match="限流"):
        c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert len(sent) == 3                                                    # two retries, then the reason
    assert [r["ok"] for r in c.calls()][:1] == [0]


def test_a_logged_answer_can_be_replayed_without_calling_again(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}'))
    a = c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    b = c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e", replay=True)
    assert (b.value, b.replayed, b.call_id, len(sent)) == (a.value, True, a.call_id, 1)


def test_the_keys_can_be_written_in_the_env_file_by_their_usual_names(tmp_path):
    from wb.secrets import read_env_file
    f = tmp_path / ".env"
    f.write_text("DeepSeek: sk-1\nOPENROUTER_API_KEY=or-2\n", encoding="utf-8")
    assert read_env_file(f) == {"deepseek_api_key": "sk-1", "openrouter_api_key": "or-2"}


def test_a_local_stand_in_can_take_the_providers_place_for_tests(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("WB_AI_BASE_URL", "http://127.0.0.1:9/v1")
    c, sent = llm(paths, lambda r, n: answer('{"ok": true}'))
    c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert str(sent[0].url) == "http://127.0.0.1:9/v1/chat/completions"


def test_a_model_whose_cost_cannot_be_counted_is_not_called(paths, monkeypatch):
    c, calls = llm(paths, lambda r, n: answer('{"ok": true}'))
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c.save({**c.config().model_dump(), "tasks": {**c.config().model_dump()["tasks"], "rule_assistant": {"provider": "deepseek", "model": "deepseek-v9"}}})
    assert "价格表" in c.status()["tasks"]["rule_assistant"]["reason"]   # the budget could not hold it
    with pytest.raises(AIUnavailable, match="价格表"):
        c.chat_json("rule_assistant", "json", "hi", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    assert calls == []


def test_the_answer_counts_every_try_in_its_cost_and_time(paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c, sent = llm(paths, lambda r, n: answer('{"ok": false}' if n == 1 else '{"ok": true}'))
    a = c.chat_json("rule_assistant", "json", "u", SCHEMA, "ok", need_ok, template_hash="t", evidence_hash="e")
    one = (120 * 9.0 + 30 * 27.0) / 1e6                                           # deepseek-v4-pro at peak, no cache hits
    assert a.attempts == 2 and a.cost == pytest.approx(2 * one)                    # what you paid, both tries
    assert c.status()["spent"]["day"] == pytest.approx(2 * one)
