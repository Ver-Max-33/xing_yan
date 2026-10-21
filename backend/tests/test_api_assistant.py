"""The rule assistant through the API (spec §6.1–§6.4): draft, edit, and choices applied without AI."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_ai_rule_assistant import GOOD, SCALE, envelope
from wb.api.app import create_app

H = {"X-WB-Token": "t"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    monkeypatch.delenv("WB_SECRET_DEEPSEEK_API_KEY", raising=False)
    return create_app(tmp_path, token="t")


def test_a_draft_without_a_key_is_unavailable_with_the_reason(app):
    r = TestClient(app).post("/api/assistant/draft", json={"text": "放量突破箱顶"}, headers=H)
    assert r.status_code == 503 and "API key" in r.json()["detail"]


def test_a_draft_and_an_edit_through_the_api(app, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    answers = [envelope(assumptions=[SCALE]), envelope(rule=GOOD.replace("value: 2.0", "value: 2.5"), note="突破量要求高一点")]
    app.state.llm_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": answers.pop(0)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
    c = TestClient(app)
    d = c.post("/api/assistant/draft", json={"text": "横盘一两个月后放量突破"}, headers=H).json()
    assert d["rule_text"].startswith("id: shrink_breakout") and d["assumptions"][0]["id"] == "box_scale"
    assert d["understanding"]["sections"][0]["title"] == "适用"
    e = c.post("/api/assistant/edit", json={"rule_text": GOOD, "instruction": "突破量要求高一点"}, headers=H).json()
    assert e["note"] == "突破量要求高一点" and "-  - {id: vol, value: 2.0" in e["diff"]


def test_a_choice_is_applied_by_the_program(app):
    c = TestClient(app)
    r = c.post("/api/assistant/apply", json={"text": GOOD, "changes": [{"path": "stages.box.with.scale", "value": "long"}]}, headers=H)
    assert r.status_code == 200 and "scale: long" in r.json()["text"] and r.json()["understanding"]["summary"]
    assert "+" in r.json()["diff"] and "scale: long" in r.json()["diff"]                 # what the program changed, shown before use
    bad = c.post("/api/assistant/apply", json={"text": GOOD, "changes": [{"path": "stages.zz.with.scale", "value": "long"}]}, headers=H)
    assert bad.status_code == 422 and "stages.zz" in bad.json()["detail"]
