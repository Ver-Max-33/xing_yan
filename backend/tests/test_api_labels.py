"""The labelling tool's API (spec §5.10): prices in the display basis on the way in and out, stored as
exchange prices on their days; agreement with the algorithm's boxes under chosen settings."""
import json
from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.data.store import Store
from wb.datasets import conform

H = {"X-WB-Token": "t"}
DAYS = [date(2025, 1, 1) + timedelta(days=i) for i in range(120)]


@pytest.fixture
def api(tmp_path):
    """One CN listing whose adjustment factor doubles on bar 60 (an ex-date that halves the price)."""
    paths = get_paths("test", tmp_path)
    n = len(DAYS)
    close = [10.0 + (i % 5) * 0.2 for i in range(60)] + [5.0 + (i % 5) * 0.1 for i in range(60)]
    bars = conform(pl.DataFrame({
        "listing_id": "CN:600000", "date": DAYS, "open": close, "high": [c * 1.01 for c in close],
        "low": [c * 0.99 for c in close], "close": close, "volume": [1e6] * n, "amount": [1e7] * n,
        "adj_factor": [1.0] * 60 + [2.0] * 60, "share_factor": 1.0, "suspended": False, "source": "test"}), "bars_1d")
    inst = conform(pl.DataFrame({"listing_id": ["CN:600000"], "market": "CN", "code": ["600000"], "name": ["浦发银行"],
                                 "status": "L"}), "instruments")
    cal = conform(pl.DataFrame({"date": DAYS, "is_open": True}), "calendar")
    Store(paths, "CN").publish({"bars_1d": bars, "instruments": inst, "calendar": cal}, as_of=DAYS[-1])
    return TestClient(create_app(tmp_path, token="t"))


LABEL = {"listing_id": "CN:600000", "cutoff": "2025-04-30", "is_box": True, "category": "flat", "start": "2025-01-05",
         "end": "2025-02-20", "top": 5.4, "bottom": 5.0, "scale": "mid",
         "touches": [{"date": "2025-01-10", "side": "top", "price": 5.4}], "exclusions": []}


def test_a_label_drawn_in_forward_adjusted_prices_is_stored_as_exchange_prices(api):
    r = api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H)
    assert r.status_code == 200
    lid = r.json()["id"]
    raw = api.get("/api/labels", params={"listing_id": "CN:600000", "basis": "raw"}, headers=H).json()[0]
    assert raw["top"] == pytest.approx(10.8) and raw["touches"][0]["price"] == pytest.approx(10.8)   # before the ex-date ×2
    qfq = api.get("/api/labels", params={"listing_id": "CN:600000", "basis": "qfq"}, headers=H).json()[0]
    assert qfq["top"] == pytest.approx(5.4) and qfq["grp"] in ("tune", "test") and qfq["id"] == lid


def test_labels_are_edited_reviewed_and_deleted(api):
    lid = api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H).json()["id"]
    assert api.put(f"/api/labels/{lid}", json={"note": "上沿再看看", "basis": "qfq"}, headers=H).status_code == 200
    assert api.put(f"/api/labels/{lid}/verdict", json={"verdict": "both_ok"}, headers=H).status_code == 200
    got = api.get("/api/labels", headers=H).json()[0]
    assert got["note"] == "上沿再看看" and got["verdict"] == "both_ok" and got["top"] == pytest.approx(5.4)
    assert api.put(f"/api/labels/{lid}/verdict", json={"verdict": "maybe"}, headers=H).status_code == 422
    assert api.post("/api/labels", json={**LABEL, "category": "nope", "basis": "qfq"}, headers=H).status_code == 422
    assert api.delete(f"/api/labels/{lid}", headers=H).status_code == 200
    assert api.get("/api/labels", headers=H).json() == []


def test_agreement_runs_the_box_primitive_with_the_settings_given(api):
    api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H)
    r = api.post("/api/labels/agreement", json={"market": "CN", "settings": {"tol": 0.02}}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert len(body["labels"]) == 1 and set(body["summary"]) == {"tune", "test"} and body["thresholds"]["edge"] == 0.02
    bad = api.post("/api/labels/agreement", json={"market": "CN", "settings": {"tol": "x"}}, headers=H)
    assert bad.status_code == 422


def test_malformed_label_bodies_get_a_reason_not_a_crash(api):
    lid = api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H).json()["id"]
    for body in ({**LABEL, "top": "abc"}, {**LABEL, "start": "garbage"}, {**LABEL, "touches": [1, 2]}, {**LABEL, "top": float("inf")},
                 {**LABEL, "basis": "nope"}):
        raw = json.dumps({"basis": "qfq", **body})                       # `Infinity` as a client may send it
        r = api.post("/api/labels", content=raw, headers={**H, "Content-Type": "application/json"})
        assert r.status_code == 422, body
    assert api.put(f"/api/labels/{lid}", json={"top": "abc", "basis": "qfq"}, headers=H).status_code == 422
    assert api.put("/api/labels/nope", json={"note": "x", "basis": "qfq"}, headers=H).status_code == 404
    assert api.put("/api/labels/nope/verdict", json={"verdict": "both_ok"}, headers=H).status_code == 404
    assert api.put("/api/hit_labels", json={"rule_id": "r", "listing_id": "CN:600000", "signal_date": "2025-01-05",
                                            "verdict": "right", "version": "abc"}, headers=H).status_code == 422
    assert api.post("/api/labels/agreement", json={"market": "CN", "thresholds": {"edge": "x"}}, headers=H).status_code == 422
    assert api.get("/api/labels", headers=H).status_code == 200              # nothing bad got stored


def test_agreement_takes_box_settings_and_refuses_unknown_ones(api):
    api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H)
    ok = {"market": "CN", "settings": {"tol": 0.02, "mid.dense": 0.35}}
    assert api.post("/api/labels/agreement", json=ok, headers=H).status_code == 200
    assert api.post("/api/labels/agreement", json={"market": "CN", "settings": {"body_touch": False}}, headers=H).status_code == 422


def test_agreement_covers_every_market_when_none_is_named(api):
    api.post("/api/labels", json={**LABEL, "basis": "qfq"}, headers=H)
    both = api.post("/api/labels/agreement", json={}, headers=H)            # JP has no data here: skipped, not an error
    assert both.status_code == 200
    one = api.post("/api/labels/agreement", json={"market": "CN"}, headers=H).json()
    assert both.json()["labels"] == one["labels"] and both.json()["summary"] == one["summary"]
    assert api.post("/api/labels/agreement", json={"market": "XX"}, headers=H).status_code == 422
