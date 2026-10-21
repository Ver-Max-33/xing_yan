"""验证 through the API (spec §9): a run is queued and done in the background, one at a time; it keeps its rule version,
the data profiles, the range and the split fixed before it ran; the event study's signals come with their outcomes;
a check-up lists each condition; a formula is tested on its own against random entries."""
import time

import pytest
from fastapi.testclient import TestClient

from tests.test_l3_run import ABOVE_MA
from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write

H = {"X-WB-Token": "t"}


@pytest.fixture
def c(tmp_path):
    write(get_paths("test", tmp_path), 320)
    client = TestClient(create_app(tmp_path, token="t"))
    assert client.post("/api/rules", json={"text": ABOVE_MA}, headers=H).status_code == 200
    return client


def done(c, rid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/validate/{rid}", headers=H).json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.1)
    raise AssertionError("still running")


def test_an_event_study_runs_in_the_background_and_keeps_what_it_ran_on(c):
    setup = c.get("/api/validate/setup?markets=CN,JP", headers=H).json()
    assert [m["market"] for m in setup["markets"]] == ["CN", "JP"] and setup["markets"][0]["profile"] == "test"
    assert [m["benchmark"] for m in setup["markets"]] == ["CN:IDX:000300", "JP:IDX:N225"]          # the test profile's
    assert setup["start"] < setup["oos_start"] < setup["end"]
    rid = c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    r = done(c, rid)
    assert r["status"] == "done" and r["rule_id"] == "above_ma" and r["version"] == 1 and r["profiles"] == {"CN": "test"}
    assert r["oos_start"] == r["result"]["summary"]["split"]["oos_start"]
    s = r["result"]["summary"]
    rows = c.get(f"/api/validate/{rid}/rows", headers=H).json()
    assert len(rows) == s["signals"] > 0 and rows[0]["name"] and "x20" in rows[0]
    assert [x["id"] for x in c.get("/api/validate?rule_id=above_ma", headers=H).json()] == [rid]


def test_a_checkup_and_a_single_condition(c):
    rid = c.post("/api/validate", json={"kind": "checkup", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    r = done(c, rid)
    assert r["status"] == "done" and r["result"]["ablation"] == []                    # ABOVE_MA has only its event
    rid = c.post("/api/validate", json={"kind": "single", "expr": "C > MA(C, 20)", "markets": ["CN", "JP"]}, headers=H).json()["id"]
    r = done(c, rid)
    assert r["status"] == "done" and r["result"]["events"]["signals"] > 0 and r["result"]["random"]["signals"] > 0


def test_what_cannot_run_is_said_before_it_is_queued(c):
    assert c.post("/api/validate", json={"kind": "single", "expr": "C >", "markets": ["CN"]}, headers=H).status_code == 422
    bad = c.post("/api/validate", json={"kind": "single", "expr": "CROSS(DIF, DEA)", "markets": ["CN"]}, headers=H)
    assert bad.status_code == 422 and bad.json()["detail"].startswith("CN：第 7 个字符：不认识的名字 DIF")   # not "x.when（…）"
    assert c.post("/api/validate", json={"kind": "event", "rule_id": "nope"}, headers=H).status_code == 404
    assert c.post("/api/validate", json={"kind": "buy", "rule_id": "above_ma"}, headers=H).status_code == 422


def test_a_market_without_data_is_left_out(tmp_path):
    from wb.db import connect
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    connect(paths.app_db, "app").execute("DELETE FROM snapshot WHERE market='JP'")      # JP: no snapshot at all
    client = TestClient(create_app(tmp_path, token="t"))
    client.post("/api/rules", json={"text": ABOVE_MA}, headers=H)
    rid = client.post("/api/validate", json={"kind": "event", "rule_id": "above_ma"}, headers=H).json()["id"]
    r = done(client, rid)
    assert r["status"] == "done" and r["markets"] == ["CN"]


def test_while_a_validation_runs_other_whole_market_work_says_so(c):
    import threading

    from wb.engine.l3.run import RUN_LOCK
    held, done_ = threading.Event(), threading.Event()

    def validation():
        with RUN_LOCK:
            held.set()
            done_.wait(10)
    t = threading.Thread(target=validation)
    t.start()
    held.wait(10)
    try:
        r = c.post("/api/runs", json={"rule_id": "above_ma", "market": "CN"}, headers=H)
        assert r.status_code == 409 and "验证" in r.json()["detail"]
    finally:
        done_.set()
        t.join()


def test_a_run_whose_worker_is_gone_is_not_shown_running_forever(tmp_path):
    from wb.config import get_paths
    from wb.validate.jobs import Validations
    write(get_paths("test", tmp_path), 320)
    v = Validations(lambda m: get_paths("test", tmp_path))
    v._conn().execute("INSERT INTO validation_run (id, kind, markets, profiles, start, end, oos_start, status, created_at) "
                      "VALUES ('x', 'event', '[\"CN\"]', '{}', '2025-01-01', '2026-01-01', '2025-06-01', 'running', '2026-10-04')")
    r = v.get("x")
    assert r["status"] == "error" and "后台" in r["error"]


def test_a_book_is_run_in_the_background_with_its_settings_and_its_ledger_can_be_read(c):
    # spec §9.2: the exits, the positions and each market's costs are the run's; the trades, the ledger and the NAV
    body = {"kind": "backtest", "rule_id": "above_ma", "markets": ["CN", "JP"],
            "book": {"max_positions": 4, "hold": 10, "box_stop": False, "trail": 0.08, "target": None, "stall": 5, "weak_positions": 2},
            "costs": {"CN": {"commission": 0.0003, "min_commission": 5, "transfer": 0.00001, "stamp": 0.0005, "slippage": 0.001},
                      "JP": {"commission": 0.001, "min_commission": 0, "transfer": 0, "stamp": 0, "slippage": 0.002}},
            "cash": {"CN": 500000, "JP": 5000000}}
    rid = c.post("/api/validate", json=body, headers=H).json()["id"]
    r = done(c, rid)
    assert r["status"] == "done" and r["settings"]["CN"]["max_positions"] == 4 and r["settings"]["JP"]["costs"]["commission"] == 0.001
    assert r["settings"]["CN"]["stall"] == 5 and r["settings"]["JP"]["weak_positions"] == 2
    cn = r["result"]["books"]["CN"]
    assert cn["settings"]["t_plus_1"] is True and r["result"]["books"]["JP"]["settings"]["t_plus_1"] is False
    assert cn["settings"]["cash"] == 500000 and cn["stats"]["end_nav"] > 0 and cn["curve"]
    trades = c.get(f"/api/validate/{rid}/trades", headers=H).json()
    ledger = c.get(f"/api/validate/{rid}/ledger", headers=H).json()
    nav = c.get(f"/api/validate/{rid}/nav", headers=H).json()
    assert trades and {"market", "name", "entry_date", "exit_date", "reason"} <= set(trades[0])
    for m, cash in (("CN", 500000), ("JP", 5000000)):     # each book's ledger balances in its own currency
        last = [x for x in nav if x["market"] == m][-1]
        assert cash + sum(x["amount"] for x in ledger if x["market"] == m) == pytest.approx(last["cash"])
    assert c.get(f"/api/validate/{rid}/rows", headers=H).status_code == 404               # an event study's table, not a book's


@pytest.mark.parametrize(("book", "says"), [({"max_positions": 0}, "最多持仓"), ({"hold": 0}, "持有"), ({"trail": 1.5}, "移动止损"),
                                           ({"target": -0.1}, "止盈"), ({"hold": None, "box_stop": False}, "离场"),
                                           ({"stall": 0}, "不创新高"), ({"max_positions": 3, "weak_positions": 4}, "大盘弱时"),
                                           ({"own": "yes"}, "规则自己的判断")])
def test_a_book_that_cannot_run_is_said_before_it_is_queued(c, book, says):
    r = c.post("/api/validate", json={"kind": "backtest", "rule_id": "above_ma", "markets": ["CN"], "book": book}, headers=H)
    assert r.status_code == 422 and says in r.json()["detail"]
    r = c.post("/api/validate", json={"kind": "backtest", "rule_id": "above_ma", "markets": ["CN"], "costs": {"CN": {"stamp": 0.5}}}, headers=H)
    assert r.status_code == 422 and "印花税" in r.json()["detail"]


TUNED = ABOVE_MA.replace("id: above_ma", "id: above_ma_n").replace('"C > MA(C, 20)"', '"C > MA(C, $n)"').replace(
    "signal_at:", "params:\n  - {id: n, value: 20, range: [10, 30], step: 5, tune: true}\nsignal_at:")


def test_robustness_runs_the_grid_and_the_costs_raised(c):
    # spec §9.3: the books over the tuned parameter's values, and the rule's own book with costs × 1, × 2, × 3
    assert c.post("/api/rules", json={"text": TUNED}, headers=H).status_code == 200
    rid = c.post("/api/validate", json={"kind": "robust", "rule_id": "above_ma_n", "markets": ["CN"], "book": {"hold": 10}},
                 headers=H).json()["id"]
    r = done(c, rid)
    assert r["status"] == "done" and r["settings"]["CN"]["hold"] == 10
    res = r["result"]
    assert res["axes"] == [{"param": "n", "values": [10, 15, 20, 25, 30], "current": 20}] and len(res["cells"]) == 5
    assert [x["k"] for x in res["costs"]] == [1, 2, 3] and "total" in res["costs"][0]["CN"]
    bad = c.post("/api/validate", json={"kind": "robust", "rule_id": "above_ma_n", "markets": ["CN"], "axes": ["nope"]}, headers=H)
    assert bad.status_code == 422 and "nope" in bad.json()["detail"]


def test_the_setup_gives_each_markets_book_by_default(c):
    # what the 组合回测 form starts from: each market's cash, lot, T+1 and costs
    s = c.get("/api/validate/setup?markets=CN,JP", headers=H).json()
    cn, jp = s["books"]["CN"], s["books"]["JP"]
    assert (cn["cash"], cn["lot"], cn["t_plus_1"], cn["costs"]["stamp"]) == (1_000_000, 100, True, 0.0005)
    assert (jp["cash"], jp["t_plus_1"], jp["costs"]["commission"], jp["hold"], jp["max_positions"]) == (10_000_000, False, 0, 20, 10)


def test_n_days_without_a_new_high_is_an_exit_of_its_own(c):
    body = {"kind": "backtest", "rule_id": "above_ma", "markets": ["CN"], "book": {"hold": None, "box_stop": False, "stall": 5}}
    assert c.post("/api/validate", json=body, headers=H).status_code == 200


# ---- 暂停、继续、取消 (1.32): a run stops at its next step; paused, it keeps the chunks it finished --------------------
def chunked(monkeypatch, on_build=None):
    """A chunk a listing (the test market has two in CN), each frame built counted — and `on_build(n)` as the n-th is
    built."""
    from wb.validate import engine
    built, real = [], engine.build_frame
    monkeypatch.setattr(engine, "CHUNK", 1)

    def counted(*a, **k):
        built.append(1)
        if on_build:
            on_build(len(built))
        return real(*a, **k)
    monkeypatch.setattr(engine, "build_frame", counted)
    return built


def wait_status(c, rid, statuses, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/validate/{rid}", headers=H).json()
        if r["status"] in statuses:
            return r
        time.sleep(0.05)
    raise AssertionError(f"not {statuses}: {r['status']}")


def test_a_paused_run_keeps_the_chunks_it_finished_and_continues_without_doing_them_again(c, monkeypatch):
    whole = done(c, c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"])
    rid = {}
    built = chunked(monkeypatch, lambda n: n == 1 and c.post(f"/api/validate/{rid['id']}/pause", headers=H))
    rid["id"] = c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    r = wait_status(c, rid["id"], ("paused", "done", "error"))
    assert r["status"] == "paused" and "已暂停" in r["progress"] and "第 1/2 批" in r["progress"]   # where it stopped
    assert len(built) == 1
    assert c.post(f"/api/validate/{rid['id']}/resume", headers=H).json() == {"ok": True}
    r = done(c, rid["id"])
    assert r["status"] == "done" and r["result"]["summary"]["signals"] == whole["result"]["summary"]["signals"]
    assert len(built) == 2                                                       # the first chunk was read back, not built again


def test_a_paused_book_continues_with_the_days_its_rates_changed(c, monkeypatch):
    rid = {}
    chunked(monkeypatch, lambda n: n == 1 and c.post(f"/api/validate/{rid['id']}/pause", headers=H))
    rid["id"] = c.post("/api/validate", json={"kind": "backtest", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    assert wait_status(c, rid["id"], ("paused", "done", "error"))["status"] == "paused"
    assert c.post(f"/api/validate/{rid['id']}/resume", headers=H).json() == {"ok": True}
    r = done(c, rid["id"])
    assert r["status"] == "done", r.get("error")
    assert ["2023-08-28", "stamp", 0.001] in r["settings"]["CN"]["costs"]["history"]     # 印花税 0.1% before it was halved


def test_a_cancelled_run_keeps_nothing_and_one_still_queued_never_runs(c, monkeypatch):
    rid = {}
    chunked(monkeypatch, lambda n: n == 1 and c.post(f"/api/validate/{rid['id']}/cancel", headers=H))
    rid["id"] = c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    queued = c.post("/api/validate", json={"kind": "checkup", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    assert c.post(f"/api/validate/{queued}/cancel", headers=H).json() == {"ok": True}   # waiting: gone at once
    r = wait_status(c, rid["id"], ("cancelled", "done", "error"))
    assert r["status"] == "cancelled" and r["result"] is None
    assert c.get(f"/api/validate/{queued}", headers=H).json()["status"] == "cancelled"
    assert c.post(f"/api/validate/{rid['id']}/resume", headers=H).json() == {"ok": False}   # nothing to continue
    import pathlib
    assert not any(pathlib.Path(p).name.startswith(rid["id"]) for p in (c.app.state.validations.folder.glob("*")))


def test_a_run_cut_off_by_quitting_the_app_comes_back_paused_and_continues(c, monkeypatch, tmp_path):
    from wb.validate.jobs import Validations
    rid = {}
    chunked(monkeypatch, lambda n: n == 1 and c.post(f"/api/validate/{rid['id']}/pause", headers=H))
    rid["id"] = c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"]
    wait_status(c, rid["id"], ("paused",))
    v = c.app.state.validations
    v._conn().execute("UPDATE validation_run SET status='running' WHERE id=?", (rid["id"],))   # as a quit app leaves it
    again = Validations(v.paths_of)                                                # the next start
    r = again.get(rid["id"])
    assert r["status"] == "paused" and "关掉" in r["progress"]
    assert again.resume(rid["id"]) is True
    end = time.time() + 60
    while again.get(rid["id"])["status"] not in ("done", "error") and time.time() < end:
        time.sleep(0.05)
    assert again.get(rid["id"])["status"] == "done"


def test_pause_resume_and_cancel_say_when_there_is_nothing_to_do(c):
    rid = done(c, c.post("/api/validate", json={"kind": "event", "rule_id": "above_ma", "markets": ["CN"]}, headers=H).json()["id"])["id"]
    for action in ("pause", "resume", "cancel"):
        assert c.post(f"/api/validate/{rid}/{action}", headers=H).json() == {"ok": False}   # it is done
    assert c.post("/api/validate/nope/pause", headers=H).status_code == 404
