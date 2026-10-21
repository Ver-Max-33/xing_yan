"""危险提醒 through the API (design 2026-10-15 §3, §6)."""
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write

H = {"X-WB-Token": "t"}


@pytest.fixture
def c(tmp_path):
    write(get_paths("test", tmp_path), 320)
    return TestClient(create_app(tmp_path, token="t"))


def test_the_warnings_of_a_list_and_who_is_not_covered(c):
    r = c.post("/api/danger", json={"market": "CN", "ids": ["CN:600519", "CN:830799"]}, headers=H).json()
    assert r["covered"] and r["day"] and isinstance(r["warnings"], dict)
    assert all(lid.startswith("CN:") for lid in r["warnings"]) and "CN:600519" not in r["uncovered"]
    jp = c.post("/api/danger", json={"market": "JP", "ids": ["JP:7203"]}, headers=H).json()
    assert jp["covered"] is False and "沪深" in jp["why"]


def test_a_stocks_history_for_the_chart(c):
    r = c.get("/api/danger/events", params={"listing_id": "CN:600519"}, headers=H).json()
    assert r["covered"] and all({"date", "kind", "label"} <= set(e) for e in r["events"])
    assert c.get("/api/danger/events", params={"listing_id": "JP:7203"}, headers=H).json()["covered"] is False
