"""个股研究 through the API (spec §10.3, §10.4): the evidence pack to look at before anything is sent, a research on the
latest day, the stock's researches with what changed between them, and the record of every research with its forward
evaluation."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_ai_research import answer
from tests.test_validate_engine import boxes_market
from wb.api.app import create_app
from wb.config import get_paths
from wb.workspace.store import Workspace

H = {"X-WB-Token": "t"}
LID = "CN:600000"
CITE = {"text": "见收盘", "refs": ["px.close"], "inference": False}
PLAIN = {"support": [CITE], "against": [CITE], "invalidation": [CITE]}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    monkeypatch.delenv("WB_SECRET_DEEPSEEK_API_KEY", raising=False)
    paths = get_paths("test", tmp_path)
    boxes_market(paths)
    Workspace(paths).set_setting("datause.cninfo.store", False)   # no announcements fetched in a unit test
    return create_app(tmp_path, token="t")


def test_the_pack_is_there_to_read_before_anything_is_sent_and_ai_waits_without_a_key(app):
    c = TestClient(app)
    p = c.get("/api/research/pack", params={"listing_id": LID}, headers=H).json()
    assert p["day"] == "2026-09-30" and any(f["id"] == "px.close" for f in p["facts"]) and p["left_out"] == []
    assert p["demo"] is False                                                   # real data: its announcements can be fetched
    r = c.post("/api/research", json={"listing_id": LID}, headers=H)
    assert r.status_code == 503 and "API key" in r.json()["detail"]
    assert c.get("/api/research", params={"listing_id": LID}, headers=H).json() == []
    assert c.post("/api/research", json={"listing_id": "CN:999999"}, headers=H).status_code == 422   # nothing to give it


def test_a_research_and_the_next_one_with_what_changed_and_the_record(app, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    same = {"verdict": "同向", "text": "仍在箱里", "refs": ["px.close"]}
    answers = [answer(**PLAIN), answer(**PLAIN, continuity=same)]
    app.state.llm_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": answers.pop(0)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
    c = TestClient(app)
    first = c.post("/api/research", json={"listing_id": LID}, headers=H).json()
    second = c.post("/api/research", json={"listing_id": LID}, headers=H).json()
    assert second["output"]["continuity"] == same and second["trigger"] == "manual"
    h = c.get("/api/research", params={"listing_id": LID}, headers=H).json()
    assert [x["id"] for x in h] == [second["id"], first["id"]] and h[1]["diff"] is None
    assert h[0]["diff"] == {"added": [], "removed": [], "changed": []}                     # the same day: nothing new
    rec = c.get("/api/research/recent", headers=H).json()
    assert [x["id"] for x in rec["records"]] == [second["id"], first["id"]] and "pack" not in rec["records"][0]
    assert rec["evaluation"]["pending"] == 2 and json.dumps(rec, ensure_ascii=False)


def test_a_document_you_import_through_the_api_its_pages_its_excerpt_and_deleting_it(app):
    from tests.test_docs_pdf import make_pdf
    c, raw = TestClient(app), {**H, "Content-Type": "application/octet-stream"}
    r = c.post("/api/docs/ir", params={"listing_id": LID, "filename": "deck.pdf"}, content=make_pdf("Cover", "Guidance raised to 4.9 trillion yen"), headers=raw)
    d = r.json()
    assert r.status_code == 200 and (d["title"], d["page"], d["excerpt"]) == ("deck", 2, "Guidance raised to 4.9 trillion yen")
    got = c.get(f"/api/docs/ir/{d['id']}", headers=H).json()
    assert got["doc"]["id"] == d["id"] and got["pages"] == ["Cover", "Guidance raised to 4.9 trillion yen"]
    assert c.put(f"/api/docs/ir/{d['id']}", json={"excerpt": "Cover", "page": 1}, headers=H).json()["excerpt"] == "Cover"
    assert [x["id"] for x in c.get("/api/docs/ir", params={"listing_id": LID}, headers=H).json()] == [d["id"]]
    facts = c.get("/api/research/pack", params={"listing_id": LID}, headers=H).json()["facts"]
    assert any(f["id"] == f"ir.{d['id']}" and f["text"].endswith("第 1 页：Cover") for f in facts)
    bad = c.post("/api/docs/ir", params={"listing_id": LID, "filename": "x.zip"}, content=b"PK\x03\x04\x00binary", headers=raw)
    assert bad.status_code == 422 and "不是 PDF" in bad.json()["detail"]
    assert c.delete(f"/api/docs/ir/{d['id']}", headers=H).status_code == 200
    assert c.get("/api/docs/ir", params={"listing_id": LID}, headers=H).json() == []


def test_a_file_over_the_limit_is_refused_before_it_is_read(app, monkeypatch):
    # the size is known from the request before the body is taken: never the whole file in memory first
    monkeypatch.setattr("wb.docs.ir.MAX_BYTES", 1000)

    def never(*a, **k):
        raise AssertionError("read a file over the limit")
    monkeypatch.setattr("wb.docs.ir.add", never)
    r = TestClient(app).post("/api/docs/ir", params={"listing_id": LID, "filename": "big.pdf"}, content=b"%PDF-" + b"0" * 2000,
                             headers={**H, "Content-Type": "application/octet-stream"})
    assert r.status_code == 413 and "太大" in r.json()["detail"]
