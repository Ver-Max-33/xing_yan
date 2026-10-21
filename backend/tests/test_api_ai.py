"""AI settings and status through the API (spec §10.1): which model each task uses, the budget, the keys' sources."""
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app

H = {"X-WB-Token": "t"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    for name in ("DEEPSEEK", "OPENROUTER"):
        monkeypatch.delenv(f"WB_SECRET_{name}_API_KEY", raising=False)
    monkeypatch.setenv("WB_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    return TestClient(create_app(tmp_path, token="t"))


def test_without_a_key_the_status_says_what_is_missing_and_a_test_call_is_refused(c):
    st = c.get("/api/ai/status", headers=H).json()
    assert st["tasks"]["rule_assistant"]["ready"] is False and "DeepSeek" in st["tasks"]["rule_assistant"]["reason"]
    assert st["spent"] == {"day": 0, "month": 0} and st["day_cap"] == 5.0
    r = c.post("/api/ai/test", json={"task": "rule_assistant"}, headers=H)
    assert r.status_code == 503 and "API key" in r.json()["detail"]
    assert {"deepseek_api_key", "openrouter_api_key"} <= set(c.get("/api/secrets", headers=H).json())


def test_the_settings_round_trip_and_bad_values_are_refused_in_chinese(c):
    cfg = c.get("/api/ai/settings", headers=H).json()
    cfg["tasks"]["rule_assistant"] = {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}
    cfg["day_cap"] = 2.5
    r = c.put("/api/ai/settings", json=cfg, headers=H)
    assert r.status_code == 200 and r.json()["tasks"]["rule_assistant"]["provider"] == "openrouter"
    assert c.get("/api/ai/status", headers=H).json()["tasks"]["rule_assistant"]["reason"].count("OpenRouter") == 1
    bad = c.put("/api/ai/settings", json={**cfg, "day_cap": -1}, headers=H)
    assert bad.status_code == 422 and "day_cap" in bad.json()["detail"]
    assert c.get("/api/ai/calls", headers=H).json() == []
