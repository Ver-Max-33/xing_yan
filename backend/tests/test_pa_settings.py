"""价格行为分析 (design 2026-10-20 §7, §8): its own model setting, kept apart from main's AI settings, and the AI client's
multi-turn JSON call (stage 2 goes on from stage 1's conversation)."""
import json

import httpx

from wb.ai.client import LLM, TaskModel
from wb.pa.settings import DEFAULT, TASK, load_task, max_tokens, pa_settings, prefix_chain, save_task, status


def test_the_default_task_and_a_saved_one(paths):
    assert load_task(paths) == DEFAULT
    saved = save_task(paths, {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"})
    assert load_task(paths) == saved and saved.model == "deepseek-flash"


def test_saving_it_leaves_the_main_ai_settings_readable(paths):
    save_task(paths, {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"})
    assert set(LLM(paths).config().tasks) == {"rule_assistant", "research"}       # main's AIConfig still loads


def test_chat_messages_sends_the_whole_conversation_as_pa_agent_does(paths, monkeypatch):
    # no response_format (upstream never sets one: its validator reads the answer) and its max_tokens, min(cap, 384,000)
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    seen = []
    llm = LLM(paths, transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})))
    msgs = [{"role": "system", "content": "s json"}, {"role": "user", "content": "u"}, {"role": "assistant", "content": "{}"},
            {"role": "user", "content": "u2"}]
    t = llm.chat_messages(TASK, DEFAULT, msgs, template_hash="t", evidence_hash="e", max_tokens=max_tokens(DEFAULT))
    assert seen[0]["messages"] == msgs and "response_format" not in seen[0]
    assert seen[0]["max_tokens"] == 384000 and seen[0]["model"] == "deepseek-v4-pro" and t.usage["prompt_tokens"] == 10
    assert t.content == "{}" and t.call_id and llm.calls(1)[0]["task"] == "price_action"


def test_status_says_why_it_cannot_run(paths):
    s = status(LLM(paths), DEFAULT)
    assert s["ready"] is False and "API key" in s["reason"] and s["name"] == "价格行为"


def test_pa_settings_follow_the_task_and_keep_pas_own_defaults():
    other = TaskModel(provider="openrouter", model="anthropic/claude-x", effort="none")
    s = pa_settings(other)
    assert (s.provider.model, s.provider.base_url, s.provider.thinking) == ("anthropic/claude-x", "https://openrouter.ai/api/v1", False)
    assert s.general.decision_stance == "balanced" and s.prompt.experience_max_entries == 0 and s.validation.retry_max == 3
    assert pa_settings(DEFAULT).provider.base_url == "https://api.deepseek.com" and pa_settings(DEFAULT).provider.thinking
    assert (max_tokens(DEFAULT), max_tokens(other)) == (384000, 384000)          # upstream: min(the gateway's cap, 384,000)
    on_openrouter = TaskModel(provider="openrouter", model="deepseek/deepseek-v4-pro", effort="high")
    assert (prefix_chain(DEFAULT), prefix_chain(other), prefix_chain(on_openrouter)) == (True, False, True)   # a DeepSeek model
