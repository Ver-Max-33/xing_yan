"""价格行为分析 through the API (design 2026-10-20 §6, §7): what an analysis would use before the click, a start that
runs in the background and is kept, one at a time, the refusals in plain words before anything is kept, cancelling,
the model setting, and a restart closing what was running."""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.pa_fixtures import answers, pa_market, transport
from wb.api.app import create_app
from wb.pa import records
from wb.pa.frame import daily_input
from wb.pa.jobs import RUNNER
from wb.workspace.store import Workspace

H = {"X-WB-Token": "t"}
LID = "CN:600000"


def make_app(paths, monkeypatch, **market):
    monkeypatch.setenv("WB_ENV_FILE", str(paths.root / "none.env"))
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    monkeypatch.delenv("WB_SECRET_DEEPSEEK_API_KEY", raising=False)
    pa_market(paths, **market)
    return create_app(paths.root, token="t")


@pytest.fixture
def app(paths, monkeypatch):
    return make_app(paths, monkeypatch)


def held(paths, replies: list[str]) -> tuple[httpx.MockTransport, threading.Event, threading.Event]:
    """The model answers only once `go` is set: an analysis kept running while the test asks again. `asked` is set
    once a call has reached it (the run is then inside its first call)."""
    go, asked, t = threading.Event(), threading.Event(), transport(replies)

    def answer(r):
        asked.set()
        go.wait(10)
        return t.handle_request(r)

    return httpx.MockTransport(answer), go, asked


def start(c: TestClient):
    return c.post("/api/pa", json={"listing_id": LID}, headers=H)


def test_an_analysis_from_start_to_record(app, paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    app.state.llm_transport = transport(answers(daily_input(paths, LID).frame, kind="limit"))
    c = TestClient(app)
    r = start(c)
    assert r.status_code == 202
    aid = r.json()["id"]
    RUNNER.wait(aid)
    rec = c.get(f"/api/pa/{aid}", headers=H).json()
    assert rec["status"] == "ok" and rec["final_plan"]["order_type"] == "限价单" and rec["stage1"]["cycle_position"]
    assert rec["model"] == "deepseek-v4-pro" and rec["trades"] == {"ai": {"tp1": None, "tp2": None}, "final": {"tp1": None, "tp2": None}}
    h = c.get("/api/pa", params={"listing_id": LID}, headers=H).json()
    assert [x["id"] for x in h] == [aid] and h[0]["trades"]["final"] == {"tp1": None, "tp2": None}   # each with its trades
    recent = c.get("/api/pa/recent", headers=H).json()
    assert [x["id"] for x in recent["records"]] == [aid] and recent["evaluation"]["enough"] is False
    assert c.get("/api/pa/nope", headers=H).status_code == 404


def test_not_enough_bars_is_422_and_nothing_is_kept(paths, monkeypatch):                         # Review Focus 1
    app = make_app(paths, monkeypatch, bars=99)
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c = TestClient(app)
    r = start(c)
    assert r.status_code == 422 and "只有 99 根" in r.json()["detail"]
    assert c.get("/api/pa", params={"listing_id": LID}, headers=H).json() == []


def test_a_second_start_while_one_runs_is_409(app, paths, monkeypatch):                          # Review Focus 3
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    app.state.llm_transport, go, _ = held(paths, answers(daily_input(paths, LID).frame))
    c = TestClient(app)
    first, second = start(c), start(c)
    go.set()
    RUNNER.wait(first.json()["id"])
    assert first.status_code == 202 and second.status_code == 409 and "正在分析 600000" in second.json()["detail"]
    app.state.llm_transport = transport(answers(daily_input(paths, LID).frame))
    third = start(c)                                                               # free again once it is done
    RUNNER.wait(third.json()["id"])
    assert third.status_code == 202


def test_the_same_input_again_is_the_same_analysis_and_asks_no_model(app, paths, monkeypatch):
    # 10-21: the same bars, rules, model and last plan
    # give the analysis already made: the model's answer is a draw, and asking again could answer otherwise
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    app.state.llm_transport = transport(answers(daily_input(paths, LID).frame, kind="wait"))   # one reply: 不买
    c = TestClient(app)
    first = start(c).json()
    RUNNER.wait(first["id"])
    again = start(c)                                       # no reply left: asking the model again would fail
    assert first["reused"] is False and again.status_code == 202 and again.json() == {"id": first["id"], "reused": True}
    assert [x["id"] for x in c.get("/api/pa", params={"listing_id": LID}, headers=H).json()] == [first["id"]]
    c.put("/api/pa/settings", json={"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "max"}, headers=H)
    app.state.llm_transport = transport(answers(daily_input(paths, LID).frame, kind="wait"))
    other = start(c).json()                                # another effort: another process, asked anew
    RUNNER.wait(other["id"])
    assert other["reused"] is False and other["id"] != first["id"]


def test_the_same_input_after_a_buy_is_still_the_same_analysis(app, paths, monkeypatch):
    # final review 1: the buy became the stock's last plan, so the next click's input differed and the model was asked
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    app.state.llm_transport = transport(answers(daily_input(paths, LID).frame, kind="limit"))
    c = TestClient(app)
    first = start(c).json()
    RUNNER.wait(first["id"])
    assert c.get(f"/api/pa/{first['id']}", headers=H).json()["final_plan"]["order_type"] == "限价单"
    assert start(c).json() == {"id": first["id"], "reused": True}


def test_cancel_while_it_runs(app, paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    app.state.llm_transport, go, asked = held(paths, answers(daily_input(paths, LID).frame))
    c = TestClient(app)
    aid = start(c).json()["id"]
    assert asked.wait(10)                                                         # inside its first call: cannot stop yet
    assert c.post(f"/api/pa/{aid}/cancel", headers=H).json() == {"ok": True}
    shown = c.get(f"/api/pa/{aid}", headers=H).json()                            # final review, Important 7: said at once
    assert shown["status"] == "running" and shown["stage"] == "取消中：等这一步的回答回来就停（这一步照样计费）"
    go.set()
    RUNNER.wait(aid)
    assert c.get(f"/api/pa/{aid}", headers=H).json()["status"] == "cancelled"
    assert c.post(f"/api/pa/{aid}/cancel", headers=H).json() == {"ok": False}                     # nothing left to stop


def test_the_data_use_switch_off_refuses(app, paths, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    Workspace(paths).set_setting("datause.tushare.llm", False)         # CN bars_1d come from tushare in both profiles
    r = start(TestClient(app))
    assert r.status_code == 422 and "Tushare 的「发送给 LLM」" in r.json()["detail"]


def test_only_a_shares_and_known_listings(app, monkeypatch):
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    c = TestClient(app)
    jp = c.post("/api/pa", json={"listing_id": "JP:7203"}, headers=H)
    assert jp.status_code == 422 and jp.json()["detail"] == "价格行为分析只支持 A 股"
    assert c.post("/api/pa", json={"listing_id": "CN:999999"}, headers=H).status_code == 422


def test_no_key_is_503_with_the_reason(app):
    r = start(TestClient(app))
    assert r.status_code == 503 and "API key" in r.json()["detail"]


def test_a_failed_model_run_is_kept_as_failed(app, paths, monkeypatch):                          # Review Focus 2
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    s1, _ = answers(daily_input(paths, LID).frame)
    app.state.llm_transport = transport([s1] + ["not json"] * 4)
    c = TestClient(app)
    aid = start(c).json()["id"]
    RUNNER.wait(aid)
    rec = c.get(f"/api/pa/{aid}", headers=H).json()
    assert rec["status"] == "failed" and rec["error"] and rec["cost"] > 0 and len(rec["call_ids"]) == 5


def test_startup_recovers_a_running_row(paths, monkeypatch):                                     # Review Focus 4
    pa_market(paths)
    aid = records.create(paths, daily_input(paths, LID), previous=None, model="m",
                         now=datetime(2026, 9, 30, 20, tzinfo=ZoneInfo("Asia/Shanghai")))   # left running
    create_app(paths.root, token="t")
    assert records.get(paths, aid)["status"] == "failed"


def test_preview_says_which_day_and_whether_it_can_run(app):
    c = TestClient(app)
    p = c.get("/api/pa/preview", params={"listing_id": LID}, headers=H).json()
    assert p["day"] == "2026-09-30" and p["supported"] is True and p["ready"] is False and "API key" in p["reason"]
    assert p["model"] == "deepseek-v4-pro" and p["adjusted"] is False and p["demo"] is False         # 测试档: raw prices
    jp = c.get("/api/pa/preview", params={"listing_id": "JP:7203"}, headers=H).json()
    assert jp["supported"] is False and jp["day"] is None


def test_settings_round_trip(app):
    c = TestClient(app)
    saved = c.put("/api/pa/settings", json={"provider": "deepseek", "model": "deepseek-flash", "effort": "low"}, headers=H)
    assert saved.json()["model"] == "deepseek-flash"
    got = c.get("/api/pa/settings", headers=H).json()
    assert got["task"]["model"] == "deepseek-flash" and got["status"]["name"] == "价格行为"
    assert c.put("/api/pa/settings", json={"provider": "nope"}, headers=H).status_code == 422
