"""The API behind the chart's structures, rule preview, runs and the gallery (spec §6.3, §8.2)."""
import time

import polars as pl
import pytest
from fastapi.testclient import TestClient

from tests.test_l3_preview import TUNED
from tests.test_l3_run import ABOVE_MA
from wb.api.app import create_app
from wb.config import get_paths
from wb.data.store import Store
from wb.devdata import write

H = {"X-WB-Token": "t"}


@pytest.fixture
def api(tmp_path):
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    return TestClient(create_app(tmp_path, token="t")), lid


def test_structures_come_in_the_display_basis_with_known_at(api):
    c, lid = api
    r = c.get("/api/structures", params={"listing_id": lid, "basis": "qfq"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["listing_id"] == lid and body["snapshot_id"]
    assert set(body) >= {"boxes", "slant", "pivots", "swings", "gold", "false_bottoms"}
    assert body["pivots"] and all("known_at" in p for p in body["pivots"])
    assert all(b["segments"] for b in body["boxes"])
    assert c.get("/api/structures", params={"listing_id": lid, "basis": "nope"}, headers=H).status_code == 422


def test_a_week_or_a_months_structures_are_found_on_its_own_bars(api):
    """Every period's own structures (design 2026-10-12-structures-every-period): the day's algorithm on the week's bars
    with its band scaled by a week's bar (3.5%), each date one of the chart's weekly bars; a period there is not refused."""
    from wb.engine.l2.periods import RANGE
    c, lid = api
    for period in ("week", "month"):
        s = c.get("/api/structures", params={"listing_id": lid, "basis": "qfq", "period": period}, headers=H).json()
        bars = c.get("/api/bars", params={"listing_id": lid, "basis": "qfq", "period": period, "limit": 5000}, headers=H).json()["rows"]
        days = {b["start"] for b in s["boxes"]} | {b["known_at"] for b in s["boxes"]} | {p["date"] for p in s["pivots"]}
        assert s["period"] == period and s["tol"] == pytest.approx(0.015 * RANGE[period])
        assert days <= {r["date"] for r in bars} and (days or period == "month")
    day = c.get("/api/structures", params={"listing_id": lid, "basis": "qfq"}, headers=H).json()
    assert day["period"] == "day" and day["tol"] == 0.015
    assert c.get("/api/structures", params={"listing_id": lid, "period": "m7"}, headers=H).status_code == 422


def test_left_pressure_on_a_day(api):
    c, lid = api
    r = c.get("/api/pressure", params={"listing_id": lid, "basis": "qfq"}, headers=H).json()
    assert set(r) >= {"date", "space", "box_id", "bottom", "top", "half_year_high", "one_year_high"}


def test_rules_are_listed_read_checked_and_saved(api):
    c, _ = api
    ids = {r["id"] for r in c.get("/api/rules", headers=H).json()}
    assert "long13" in ids
    one = c.get("/api/rules/long13", headers=H).json()
    assert one["builtin"] and "1.098" in one["text"] and one["version"] == 1
    assert c.get("/api/rules/nope", headers=H).status_code == 404
    bad = c.post("/api/rules/check", json={"text": "id: x\nname: y\nmarkets: [CN]\nstages: []\nsignal_at: a.at"}, headers=H).json()
    assert not bad["ok"] and bad["errors"]
    ok = c.post("/api/rules/check", json={"text": ABOVE_MA}, headers=H).json()
    assert ok["ok"] is True and ok["errors"] == []
    saved = c.post("/api/rules", json={"text": ABOVE_MA}, headers=H).json()
    assert saved == {"id": "above_ma", "version": 1, "new": True}
    assert c.post("/api/rules", json={"text": ABOVE_MA.replace("id: above_ma", "id: long13")}, headers=H).status_code == 422
    assert "properties" in c.get("/api/rules/schema", headers=H).json()


def test_a_preview_gives_signals_and_structures_and_explains_a_day(api):
    c, lid = api
    p = c.post("/api/preview", json={"text": TUNED, "listing_id": lid, "basis": "qfq"}, headers=H)
    assert p.status_code == 200
    body = p.json()
    assert body["signals"] and body["structures"]["pivots"] is not None
    assert body["rule"]["params"] == [{"id": "n", "value": 20, "range": [5, 60], "step": 1, "unit": ""}]
    fewer = c.post("/api/preview", json={"text": TUNED, "listing_id": lid, "params": {"n": 5}}, headers=H).json()
    assert len(fewer["signals"]) != len(body["signals"])
    assert c.post("/api/preview", json={"text": TUNED, "listing_id": lid, "params": {"n": 500}}, headers=H).status_code == 422
    day = body["signals"][0]["signal_date"]
    d = c.post("/api/preview/detail", json={"text": TUNED, "listing_id": lid, "day": day}, headers=H).json()
    assert d["detail"]["status"] == "signal" and d["lines"][0].endswith("信号")
    assert d["bands"] == [{"stage": "today", "label": "信号", "kind": "event", "start": day, "end": day}]   # tagged by name


def test_a_market_run_is_recorded_and_its_rows_read_back(api):
    c, _ = api
    run = c.post("/api/runs", json={"text": ABOVE_MA, "market": "CN"}, headers=H).json()
    assert run["mode"] == "date" and sum(run["counts"].values()) == 2
    assert c.get("/api/runs", headers=H).json()[0]["id"] == run["id"]
    rows = c.get(f"/api/runs/{run['id']}/rows", headers=H).json()
    assert len(rows) == 2 and {"listing_id", "status", "name"} <= set(rows[0])
    assert c.get("/api/runs/nope/rows", headers=H).status_code == 404


def test_the_gallery_lists_hits_on_a_sample(api):
    c, _ = api
    hits = c.post("/api/gallery", json={"text": ABOVE_MA, "market": "CN", "sample": {"n": 300}, "years": 1}, headers=H).json()
    assert hits["listings"] == 2 and hits["hits"] and {"listing_id", "signal_date"} <= set(hits["hits"][0])


def test_a_missed_hit_is_explained_through_the_api(api):
    c, lid = api
    r = c.post("/api/rules/missed", json={"text": ABOVE_MA, "listing_id": lid, "start": "2000-01-01", "end": "2100-01-01",
                                         "sample": {"n": 300}, "years": 1}, headers=H)
    assert r.status_code == 200 and r.json()["status"] in ("hit", "missed")
    assert c.post("/api/rules/missed", json={"text": ABOVE_MA, "listing_id": lid}, headers=H).status_code == 422


def test_two_versions_are_compared_through_the_api(api):
    c, _ = api
    r = c.post("/api/rules/compare", json={"market": "CN", "a": {"text": TUNED}, "b": {"text": TUNED, "params": {"n": 5}},
                                          "sample": {"n": 300}, "years": 1}, headers=H).json()
    assert {"added", "lost", "kept"} <= set(r)


def test_gallery_verdicts_and_what_a_change_does_to_them(api):
    c, _ = api
    hits = c.post("/api/gallery", json={"text": TUNED, "market": "CN", "sample": {"n": 300}, "years": 1}, headers=H).json()["hits"]
    for h, v in ((hits[0], "right"), (hits[1], "wrong")):
        r = c.put("/api/hit_labels", json={"rule_id": "above_ma", "listing_id": h["listing_id"], "signal_date": h["signal_date"],
                                           "verdict": v}, headers=H)
        assert r.status_code == 200
    assert len(c.get("/api/hit_labels", params={"rule_id": "above_ma"}, headers=H).json()) == 2
    r = c.post("/api/rules/compare", json={"market": "CN", "a": {"text": TUNED}, "b": {"text": TUNED, "params": {"n": 5}},
                                          "sample": {"n": 300}, "years": 1}, headers=H).json()
    assert r["hits"][0] == len(hits) and r["right"][0] == 1 and r["wrong"][0] == 1
    assert {"right_lost", "wrong_added"} <= set(r)


def test_values_json_cannot_carry_go_out_as_null():
    from wb.api.rules import clean
    assert clean({"space": float("inf"), "xs": [float("nan"), 1.5], "n": 2}) == {"space": None, "xs": [None, 1.5], "n": 2}


def test_malformed_rule_requests_get_a_reason_not_a_crash(api):
    c, lid = api
    for path, body in (("/api/preview", {"text": 123, "listing_id": lid}), ("/api/preview", {"text": TUNED, "listing_id": lid, "params": [1, 2]}),
                       ("/api/preview", {"text": TUNED, "listing_id": lid, "basis": "nope"}),
                       ("/api/gallery", {"text": ABOVE_MA, "market": "CN", "years": "abc"}),
                       ("/api/gallery", {"text": ABOVE_MA, "market": "CN", "sample": {"n": "x"}}),
                       ("/api/rules/missed", {"text": ABOVE_MA, "listing_id": lid, "start": "2026-01-02", "end": "2026-01-01"})):
        r = c.post(path, json=body, headers=H)
        assert r.status_code == 422, (path, body, r.status_code)


def test_one_full_market_run_at_a_time(api):
    from wb.api.rules import RUN_LOCK
    c, _ = api
    with RUN_LOCK:
        assert c.post("/api/runs", json={"text": ABOVE_MA, "market": "CN"}, headers=H).status_code == 409
    assert c.post("/api/runs", json={"text": ABOVE_MA, "market": "CN"}, headers=H).status_code == 200


def test_the_replay_slider_gets_the_listings_trading_days(api):
    c, lid = api
    days = c.get("/api/dates", params={"listing_id": lid}, headers=H).json()
    bars = c.get("/api/bars", params={"listing_id": lid, "limit": 5000}, headers=H).json()["rows"]
    assert days == [b["date"] for b in bars]
    assert c.get("/api/dates", params={"listing_id": "CN:999999"}, headers=H).json() == []


def test_the_library_enables_and_puts_away_rules_and_a_new_rule_keeps_its_own_id(api):
    c, _ = api
    assert c.put("/api/rules/long13/state", json={"enabled": True}, headers=H).status_code == 200
    row = next(r for r in c.get("/api/rules", headers=H).json() if r["id"] == "long13")
    assert row["enabled"] is True and row["archived"] is False and row["verdicts"] == {"right": 0, "wrong": 0}
    assert c.put("/api/rules/nope/state", json={"enabled": True}, headers=H).status_code == 404
    assert c.put("/api/rules/long13/state", json={"enabled": "yes"}, headers=H).status_code == 422
    assert c.post("/api/rules", json={"text": ABOVE_MA, "create": True}, headers=H).status_code == 200
    again = c.post("/api/rules", json={"text": ABOVE_MA.replace("MA(C, 20)", "MA(C, 30)"), "create": True}, headers=H)
    assert again.status_code == 409 and "已经有规则 above_ma" in again.json()["detail"]
    assert c.post("/api/rules", json={"text": ABOVE_MA.replace("MA(C, 20)", "MA(C, 30)")}, headers=H).json()["version"] == 2


def test_a_change_is_checked_against_every_hit_you_marked_not_only_the_sample(api):
    c, _ = api
    hits = c.post("/api/gallery", json={"text": TUNED, "market": "CN", "sample": {"n": 300}, "years": 1}, headers=H).json()["hits"]
    h = hits[0]
    c.put("/api/hit_labels", json={"rule_id": "above_ma", "listing_id": h["listing_id"], "signal_date": h["signal_date"],
                                   "verdict": "right"}, headers=H)
    empty = c.post("/api/watchlist/groups", json={"name": "空分组"}, headers=H).json()["id"]  # a sample without it
    r = c.post("/api/rules/compare", json={"market": "CN", "a": {"text": TUNED}, "b": {"text": TUNED},
                                          "sample": {"group_id": empty}, "years": 1}, headers=H).json()
    assert r["right"] == [1, 1] and r["listings"] == 1


def test_a_checked_rule_says_what_it_is(api):
    c, _ = api
    r = c.post("/api/rules/check", json={"text": TUNED}, headers=H).json()
    assert r["ok"] and r["rule"]["id"] == "above_ma" and r["rule"]["markets"] == ["CN", "JP"]
    assert [(p["id"], p["value"]) for p in r["rule"]["params"]] == [("n", 20)]
    assert "$n" in str(r["rule"]["stages"])                  # each slider says where its parameter is read
    u = r["understanding"]                                    # 我的理解 (spec §6.1): read back by the program
    assert u["summary"].startswith("站上") and [s["title"] for s in u["sections"]][:3] == ["适用", "参数", "阶段"]


def test_a_pool_name_that_nothing_has_is_said_when_the_rule_is_checked(api):
    # a typo there would leave the pool empty without a word: 0 hits that look like "nothing qualifies"
    c, _ = api
    gid = c.post("/api/watchlist/groups", json={"name": "重点"}, headers=H).json()["id"]
    c.post(f"/api/watchlist/{gid}/items", json={"listing_id": "CN:600519"}, headers=H)
    c.put("/api/tags/CN:600519", json={"tags": ["白酒"]}, headers=H)
    ok = ABOVE_MA.replace("signal_at:", "universe: {watchlist: 重点, tags: [白酒], boards: [主]}\nsignal_at:")
    assert c.post("/api/rules/check", json={"text": ok}, headers=H).json()["warnings"] == []
    typo = ABOVE_MA.replace("signal_at:", "universe: {watchlist: 重要, tags: [白洒], industries: [银杏], boards: [创业]}\nsignal_at:")
    assert c.post("/api/rules/check", json={"text": typo}, headers=H).json()["warnings"] == [
        "没有叫「重要」的自选分组", "还没有股票打上「白洒」这个题材标签", "数据里没有叫「银杏」的行业", "数据里没有名字含「创业」的板块"]


def test_a_tushare_label_in_the_pool_is_named_and_placed(api):
    # the pool reads 申万一级 (IN_INDUSTRY): Tushare's own label "白酒" selected nothing, and the check let it pass
    c, _ = api
    cn = ABOVE_MA.replace("markets: [CN, JP]", "markets: [CN]")
    said = lambda names: c.post("/api/rules/check", json={"text": cn.replace("signal_at:", f"universe: {{industries: [{names}]}}\nsignal_at:")},  # noqa: E731
                                headers=H).json()["warnings"]
    assert said("白酒") == ["「白酒」是 Tushare 的分类，在申万里属于「食品饮料」（股票池按申万一级）"]
    assert said("食品饮料") == [] and said("银行") == []

def test_an_index_pool_says_when_the_profile_has_no_list_or_a_late_one(api, tmp_path):
    c, _ = api
    cn = ABOVE_MA.replace("markets: [CN, JP]", "markets: [CN]").replace("signal_at:", "universe: {index: CN:IDX:000300}\nsignal_at:")
    assert c.post("/api/rules/check", json={"text": cn}, headers=H).json()["warnings"] == [
        "CN 的数据档还没有 CN:IDX:000300 的成分表：股票池会是空的"]
    st = Store(get_paths("test", tmp_path), "CN")
    last = st.latest().as_of
    st.publish({"index_members": pl.DataFrame({"index_id": ["CN:IDX:000300"], "date": [last], "listing_id": ["CN:600519"],
                                               "weight": [1.0], "segment": [None], "source": ["t"]},
                                              schema_overrides={"segment": pl.Utf8})}, as_of=last, note="members")
    assert c.post("/api/rules/check", json={"text": cn}, headers=H).json()["warnings"] == [
        f"CN:IDX:000300 的成分表从 {last} 起才有，之前的日子不在股票池里（样例图集和校准看的是近两年）"]

def test_a_stored_rule_that_no_longer_loads_opens_with_its_text_to_be_fixed(api, tmp_path):
    c, _ = api
    from wb.workspace.rules import Rules
    Rules(get_paths("test", tmp_path)).conn.execute(
        "INSERT INTO rule_version VALUES ('old', 1, 'h', 'id: old\nname: 旧\nmarkets: [CN]\nstages: []', '2026-01-01')")
    r = c.get("/api/rules/old", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["rule"] is None and body["error"] and body["text"].startswith("id: old") and body["version"] == 1
    assert c.get("/api/rules/long13", headers=H).json()["error"] is None


def test_a_market_sample_is_listed_for_labelling(api):
    c, lid = api
    got = c.get("/api/sample", params={"market": "CN", "n": 5, "seed": 3}, headers=H).json()
    assert lid in got or len(got) == 5
    assert all(x.startswith("CN:") for x in got)
    assert got == c.get("/api/sample", params={"market": "CN", "n": 5, "seed": 3}, headers=H).json()   # fixed by the seed
    assert c.get("/api/sample", params={"market": "XX"}, headers=H).status_code == 422


def test_a_market_without_a_snapshot_says_so_instead_of_failing(tmp_path):
    c = TestClient(create_app(tmp_path, token="t"))   # nothing downloaded yet
    for r in (c.get("/api/sample", params={"market": "JP"}, headers=H),
              c.post("/api/gallery", json={"text": ABOVE_MA, "market": "JP", "sample": {"n": 30}}, headers=H),
              c.post("/api/rules/compare", json={"market": "JP", "a": {"text": TUNED}, "b": {"text": TUNED, "params": {"n": 5}},
                                                "sample": {"n": 30}}, headers=H)):
        assert r.status_code == 422 and "快照" in r.json()["detail"]


def _mark(c, rule_id="above_ma"):
    """Y / N on four of the rule's hits in the gallery (three right, one wrong)."""
    hits = c.post("/api/gallery", json={"text": TUNED, "market": "CN", "sample": {"n": 300}, "years": 1}, headers=H).json()["hits"]
    for h, v in zip(hits[:4], ["right", "right", "wrong", "right"], strict=True):
        assert c.put("/api/hit_labels", json={"rule_id": rule_id, "listing_id": h["listing_id"], "signal_date": h["signal_date"],
                                              "verdict": v}, headers=H).status_code == 200


def _ended(c, rid, statuses=("done", "error", "cancelled"), timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/validate/{rid}", headers=H).json()
        if r["status"] in statuses:
            return r
        time.sleep(0.05)
    raise AssertionError(f"not {statuses}: {r['status']} {r['progress']}")


def test_a_calibration_runs_in_the_background_and_keeps_its_result(api):
    # 1.33: a calibration is a run in 验证's queue — its progress, 暂停 / 继续 / 取消, a result kept with the run
    c, _ = api
    _mark(c)
    r = c.post("/api/rules/calibrate", json={"text": TUNED, "sample": {"n": 30}, "years": 1}, headers=H)
    assert r.status_code == 200 and set(r.json()) == {"id"}
    run = _ended(c, r.json()["id"])
    assert run["status"] == "done" and run["kind"] == "calibrate" and run["rule_id"] == "above_ma" and run["version"] == 0
    assert run["settings"] == {"text": TUNED, "params": {}, "years": 1, "sample": {"n": 30, "seed": 0}}   # what it ran on
    j = run["result"]
    assert j["params"] == ["n"] and j["current"]["values"] == {"n": 20} and j["candidates"]
    assert j["samples"]["right"] == 3 and "多标一些" in j["warning"]                     # 4 marks: likely to fit only them
    assert [x["id"] for x in c.get("/api/validate", params={"rule_id": "above_ma", "kind": "calibrate"}, headers=H).json()] == [run["id"]]
    # what cannot run is said before it is queued
    other = c.post("/api/rules/calibrate", json={"text": TUNED.replace("id: above_ma", "id: other")}, headers=H)
    assert other.status_code == 422 and "样例图集" in other.json()["detail"]             # nothing marked on this rule
    plain = c.post("/api/rules/calibrate", json={"text": ABOVE_MA}, headers=H)
    assert plain.status_code == 422 and "tune" in plain.json()["detail"]                 # nothing to tune


def test_the_template_library_lists_its_cards(api):
    c, _ = api
    cards = c.get("/api/templates", headers=H).json()
    assert len(cards) == 18 and {"box", "general"} == {x["group"] for x in cards}
    pin = next(x for x in cards if x["id"] == "tpl_xia_po_hui_xiang")
    assert pin["name"] == "跌破箱底后很快收回（下破回箱）" and pin["describe"] and pin["examples"][0]["date"] == "2018-08-06"
    groups = {r["id"]: r["group"] for r in c.get("/api/rules", headers=H).json()}
    assert groups["tpl_giraffe"] == "box" and groups["vol_burst"] == "builtin"


def test_a_calibration_waits_its_turn_instead_of_being_refused(api):
    from wb.api.rules import RUN_LOCK
    c, _ = api
    _mark(c)
    assert RUN_LOCK.acquire(blocking=False)                       # a scan of the whole market under way
    try:
        r = c.post("/api/rules/calibrate", json={"text": TUNED, "sample": {"n": 30}, "years": 1}, headers=H)
        assert r.status_code == 200
        time.sleep(0.5)
        waiting = c.get(f"/api/validate/{r.json()['id']}", headers=H).json()
        assert waiting["status"] == "queued" and "扫描" in waiting["progress"]
    finally:
        RUN_LOCK.release()
    assert _ended(c, r.json()["id"])["status"] == "done"


def test_a_paused_calibration_keeps_the_parameter_sets_it_finished(api, monkeypatch):
    from wb.engine.l3 import calibrate as cal
    c, _ = api
    _mark(c)
    body = {"text": TUNED, "sample": {"n": 30}, "years": 1}
    whole = _ended(c, c.post("/api/rules/calibrate", json=body, headers=H).json()["id"])
    scored, real, rid = [], cal._found, {}

    def counted(*a, **k):                                          # one call a parameter set scored on your marks
        scored.append(1)
        if len(scored) == 2 and "id" in rid:
            c.post(f"/api/validate/{rid['id']}/pause", headers=H)
        return real(*a, **k)
    monkeypatch.setattr(cal, "_found", counted)
    rid["id"] = c.post("/api/rules/calibrate", json=body, headers=H).json()["id"]
    r = _ended(c, rid["id"], ("paused", "done", "error"))
    assert r["status"] == "paused" and "第 2 / 56 组" in r["progress"] and "A 股" in r["progress"]   # where it stopped
    assert c.post(f"/api/validate/{rid['id']}/resume", headers=H).json() == {"ok": True}
    r = _ended(c, rid["id"])
    assert r["status"] == "done" and r["result"] == whole["result"]
    assert len(scored) == 56                                       # the two it finished were read back, not scored again


def _one_stage(when: str) -> str:
    return "\n".join(["id: broken", "name: 跑不了", "markets: [CN, JP]", "params:", "  - {id: n, value: 20}", "stages:",
                      f'  - {{id: today, kind: event, when: "{when}"}}', "signal_at: today.at", ""])


@pytest.mark.parametrize("when, says", [
    ("C > MAA(C, $n)", "不认识的函数 MAA"),                     # a misspelled function
    ("MA(CC, $n) > 0", "不认识的名字 CC"),                      # a field that does not exist
    ("C > $n", "后复权价格不能和固定数字比较"),                   # a fixed price against adjusted prices
])
def test_the_check_compiles_every_formula_so_a_rule_that_cannot_run_is_never_said_to(api, when, says):
    c, _ = api
    r = c.post("/api/rules/check", json={"text": _one_stage(when)}, headers=H).json()
    assert r["ok"] is False and any(says in e for e in r["errors"]), r
    s = c.post("/api/rules", json={"text": _one_stage(when), "create": True}, headers=H)   # nor saved
    assert s.status_code == 422 and says in s.json()["detail"]


def test_a_saved_rule_that_no_longer_compiles_is_marked_and_cannot_be_enabled(api, tmp_path):
    c, _ = api
    from wb.workspace.rules import Rules
    store = Rules(get_paths("test", tmp_path))
    with store.conn:   # a rule saved before formulas were compiled on save (or when a function went away)
        store.conn.execute("INSERT INTO rule_version VALUES (?, ?, ?, ?, ?)", ("broken", 1, "h", _one_stage("C > MAA(C, $n)"), "2026-10-01T00:00:00"))
    row = next(r for r in c.get("/api/rules", headers=H).json() if r["id"] == "broken")
    assert "不认识的函数 MAA" in (row.get("problem") or "")
    on = c.put("/api/rules/broken/state", json={"enabled": True}, headers=H)
    assert on.status_code == 422 and "不认识的函数 MAA" in on.json()["detail"]
    assert c.put("/api/rules/broken/state", json={"archived": True}, headers=H).status_code == 200   # it can still be put away


def test_every_built_in_rule_compiles_for_each_of_its_markets():
    """The catalog never marks a built-in rule as one that cannot run: this is why."""
    from wb.engine.l3.match import compile_errors
    from wb.workspace.rules import builtin_rules
    assert {rid: compile_errors(rule) for rid, (rule, _) in builtin_rules().items() if compile_errors(rule)} == {}
