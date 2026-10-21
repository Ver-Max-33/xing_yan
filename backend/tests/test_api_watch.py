"""观察条目 through the API (spec §11): add from a signal, status, texts with versions, fail lines and review dates."""
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


def test_a_watched_stock_through_its_life(c):
    e = c.post("/api/watch", json={"listing_id": "CN:600519", "source": "规则「站上 20 日线」v1 的信号", "reason": "箱体干净"}, headers=H).json()
    assert e["status"] == "watching" and e["name"] == "贵州茅台" and e["reason"] == "箱体干净" and e["added_price"]
    assert [x["listing_id"] for x in c.get("/api/watch", headers=H).json()] == ["CN:600519"]
    c.put("/api/watch/CN:600519/text", json={"kind": "note", "text": "等决算"}, headers=H)
    c.put("/api/watch/CN:600519/text", json={"kind": "note", "text": "等决算，11/05"}, headers=H)
    assert [v["text"] for v in c.get("/api/watch/CN:600519/versions?kind=note", headers=H).json()] == ["等决算，11/05", "等决算"]
    r = c.post("/api/watch/CN:600519/conditions", json={"kind": "fail", "text": "收盘跌回箱顶", "op": "<", "price": 1.0}, headers=H)
    cid = r.json()["id"]
    assert c.get("/api/watch/CN:600519", headers=H).json()["conditions"][0]["distance"] < 0
    assert c.post("/api/watch/CN:600519/conditions", json={"kind": "fail", "text": "x"}, headers=H).status_code == 422
    c.delete(f"/api/watch/CN:600519/conditions/{cid}", headers=H)
    assert c.get("/api/watch/CN:600519", headers=H).json()["conditions"] == []
    assert c.put("/api/watch/CN:600519/status", json={"status": "passed"}, headers=H).json()["status_name"] == "暂不考虑"
    assert c.put("/api/watch/CN:600519/status", json={"status": "buy"}, headers=H).status_code == 422
    assert c.put("/api/watch/CN:600519/status", json={"status": "holding"}, headers=H).json()["status_name"] == "持有中"
    lines = c.get("/api/watch/CN:600519/fail-lines", headers=H).json()   # 持有中's suggestions from its structures
    assert lines and {"source", "label", "price", "since", "distance"} <= set(lines[0])
    assert c.get("/api/watch/CN:000001", headers=H).status_code == 404


def test_a_stock_in_a_group_has_an_entry_and_a_closed_one_leaves_the_groups(c):
    g = c.get("/api/watchlist", headers=H).json()[0]["id"]
    c.post(f"/api/watchlist/{g}/items", json={"listing_id": "CN:600519", "source": "K 线页"}, headers=H)
    e = c.get("/api/watch/CN:600519", headers=H).json()
    assert (e["status"], e["source"]) == ("watching", "K 线页")
    c.delete(f"/api/watchlist/{g}/items/CN:600519", headers=H)                       # out of its last group: closed
    assert c.get("/api/watch/CN:600519", headers=H).json()["status"] == "closed"
    c.post(f"/api/watchlist/{g}/items", json={"listing_id": "CN:600519"}, headers=H)  # watched again
    assert c.get("/api/watch/CN:600519", headers=H).json()["status"] == "watching"
    c.put("/api/watch/CN:600519/status", json={"status": "closed"}, headers=H)       # closed on 观察: out of the groups
    assert [i for gr in c.get("/api/watchlist", headers=H).json() for i in gr["items"]] == []
    c.put("/api/watch/CN:600519/status", json={"status": "research"}, headers=H)     # opened again: in the first group
    assert [i["listing_id"] for i in c.get("/api/watchlist", headers=H).json()[0]["items"]] == ["CN:600519"]


def test_deleting_a_group_closes_the_stocks_that_were_only_in_it(c):
    gid = c.post("/api/watchlist/groups", json={"name": "短线"}, headers=H).json()["id"]
    first = c.get("/api/watchlist", headers=H).json()[0]["id"]
    c.post(f"/api/watchlist/{gid}/items", json={"listing_id": "CN:600519"}, headers=H)
    c.post(f"/api/watchlist/{gid}/items", json={"listing_id": "CN:000001"}, headers=H)
    c.post(f"/api/watchlist/{first}/items", json={"listing_id": "CN:000001"}, headers=H)      # also in another group
    c.delete(f"/api/watchlist/groups/{gid}", headers=H)
    status = {e["listing_id"]: e["status"] for e in c.get("/api/watch", headers=H).json()}
    assert status == {"CN:600519": "closed", "CN:000001": "watching"}


def test_a_bad_condition_is_a_422_and_the_list_still_reads(c):
    c.post("/api/watch", json={"listing_id": "CN:600519"}, headers=H)
    assert c.post("/api/watch/CN:600519/conditions", json={"kind": "review", "text": "x", "due": "11/05"}, headers=H).status_code == 422
    assert c.post("/api/watch/CN:600519/conditions", json={"kind": "fail", "text": "x", "op": "<", "price": "abc"},
                  headers=H).status_code == 422
    assert c.get("/api/watch", headers=H).status_code == 200

def test_a_watched_stock_can_be_deleted_with_all_it_had(c):
    # 删除 (spec §11): the entry and everything it had — its texts and their versions, conditions, the record, its
    # groups — gone, for one stock or several at once, the others untouched (已关闭 keeps the record; this does not)
    g = c.get("/api/watchlist", headers=H).json()[0]["id"]
    for lid in ("CN:600519", "CN:000001"):
        c.post(f"/api/watchlist/{g}/items", json={"listing_id": lid, "source": "选股"}, headers=H)
        c.put(f"/api/watch/{lid}/text", json={"kind": "note", "text": "看看"}, headers=H)
        c.post(f"/api/watch/{lid}/conditions", json={"kind": "review", "text": "复看", "due": "2026-11-05"}, headers=H)
    assert c.post("/api/watch/delete", json={"listing_ids": ["CN:600519", "CN:999999"]}, headers=H).json() == {"deleted": 1}
    assert c.get("/api/watch/CN:600519", headers=H).status_code == 404
    assert [e["listing_id"] for e in c.get("/api/watch", headers=H).json()] == ["CN:000001"]
    assert [i["listing_id"] for i in c.get("/api/watchlist", headers=H).json()[0]["items"]] == ["CN:000001"]
    assert c.get("/api/watch/CN:000001/versions?kind=note", headers=H).json()[0]["text"] == "看看"
    c.post("/api/watch", json={"listing_id": "CN:600519"}, headers=H)          # nothing of it left: added again, afresh
    e = c.get("/api/watch/CN:600519", headers=H).json()
    assert e["note"] == "" and e["conditions"] == [] and [r["text"] for r in e["record"]] == ["加入观察"]
    assert c.get("/api/watch/CN:600519/versions?kind=note", headers=H).json() == []
    assert c.post("/api/watch/delete", json={"listing_ids": "CN:600519"}, headers=H).status_code == 422


def test_many_stocks_go_into_a_group_at_once(c):
    # 选股 → 全选 → 加入观察: one request for them all, each watched from its latest close as one added alone is
    g = c.get("/api/watchlist", headers=H).json()[0]["id"]
    alone = c.post("/api/watch", json={"listing_id": "CN:600519"}, headers=H).json()["added_price"]
    c.post("/api/watch/delete", json={"listing_ids": ["CN:600519"]}, headers=H)
    r = c.post(f"/api/watchlist/{g}/items", json={"listing_ids": ["CN:600519", "CN:000001"], "source": "选股：规则「x」"}, headers=H)
    assert r.json() == {"ok": True, "added": 2}
    es = {e["listing_id"]: e for e in c.get("/api/watch", headers=H).json()}
    assert set(es) == {"CN:600519", "CN:000001"} and all(e["source"] == "选股：规则「x」" for e in es.values())
    assert es["CN:600519"]["added_price"] == alone and es["CN:000001"]["added_price"]
    assert [i["listing_id"] for i in c.get("/api/watchlist", headers=H).json()[0]["items"]] == ["CN:600519", "CN:000001"]
    assert c.post(f"/api/watchlist/{g}/items", json={"listing_ids": ["CN:600519", "not a code"]}, headers=H).status_code >= 400
