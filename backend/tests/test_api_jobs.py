"""The daily cycle and the 今日 feed through the API (spec §7.1): where each market stands, a manual run, what the
latest scans found (new signals, watch entries moved on or fallen out, new watch entries, data problems), the
notification channel's settings as it declares them (§3.5), and a test card to your own bot."""
import json
from datetime import date

import httpx
import polars as pl
import pytest
from fastapi.testclient import TestClient

from tests.test_daily import FakePipeline
from tests.test_l3_preview import TUNED
from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import trading_days, write
from wb.jobs.daily import Daily, wait_until
from wb.screening.scan import enabled_rules, scan
from wb.workspace.rules import Rules

H = {"X-WB-Token": "t"}


@pytest.fixture
def app(tmp_path):
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    app = create_app(tmp_path, token="t")
    app.state.daily = Daily(app.state.ctx.paths, pipeline=lambda p, m: FakePipeline(p, m), allow_demo=True)
    return app


def earlier_day(app):
    """The rule's run on the day before the data's last: what a scan compares with (a first scan has nothing) — its
    signal still 潜伏 then, so it is new now (a stock a signal the day before too is no new signal, 10-13)."""
    paths = app.state.ctx.paths("CN")
    [s] = scan(paths, "CN", enabled_rules(paths), day=trading_days(320)[-2])
    f = paths.profile_dir / "runs" / f"{s['run_id']}.parquet"
    pl.read_parquet(f).with_columns(pl.col("status").replace("signal", "watch")).write_parquet(f)


def test_a_manual_run_scans_and_the_feed_lists_what_it_found(app):
    c = TestClient(app)
    earlier_day(app)
    jobs = c.get("/api/jobs", headers=H).json()
    assert [j["market"] for j in jobs["markets"]] == ["CN", "JP"] and jobs["markets"][0]["status"] == "idle"
    assert jobs["markets"][0]["latest"]["demo"] is True and jobs["outbox"]["pending"] == 0
    assert jobs["markets"][0]["progress"] is None                                # what a running turn is doing: nothing
    r = c.post("/api/jobs/CN/run", headers=H).json()
    assert r["started"] is True and isinstance(r["fetch"], bool)               # whether it fetches or only rescans
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    jobs = c.get("/api/jobs", headers=H).json()
    assert jobs["markets"][0]["scan_summary"]["above_ma"]["counts"]["signal"] > 0
    assert jobs["outbox"]["pending"] == 0                                      # nothing queued per scan: the briefing says it
    feed = c.get("/api/today", headers=H).json()
    assert {e["kind"] for e in feed["events"]} <= {"signal", "advance", "invalid", "watch", "data"}
    sig = [e for e in feed["events"] if e["kind"] == "signal"]
    assert sig and sig[0]["rule_name"] == TUNED_NAME and sig[0]["name"] and sig[0]["market"] == "CN"
    assert sig[0]["listing_id"].startswith("CN:") and sig[0]["day"] == jobs["markets"][0]["scan_summary"]["above_ma"]["day"]


def test_a_rules_first_scan_lists_no_changes_and_says_what_it_found(app):
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    feed = c.get("/api/today", headers=H).json()
    assert [e for e in feed["events"] if e["market"] == "CN" and e["kind"] != "data"] == []   # nothing to compare with
    [first] = feed["first"]
    assert (first["market"], first["rule_id"], first["rule_name"]) == ("CN", "above_ma", TUNED_NAME)
    assert first["counts"]["signal"] > 0 and first["day"]
    assert c.get("/api/notify", headers=H).json()["recent"] == []              # no card per stock, nor a summary


def test_pause_and_continue_go_through_the_api(app):
    c = TestClient(app)
    assert c.post("/api/jobs/CN/pause", headers=H).json() == {"paused": False}     # nothing scanning
    assert c.post("/api/jobs/CN/resume", headers=H).json() == {"started": False}   # nothing paused
    assert c.post("/api/jobs/XX/pause", headers=H).status_code == 404
    assert c.post("/api/jobs/CN/cancel", headers=H).json() == {"cancelled": False}      # nothing scanning or paused


def test_a_market_in_trouble_is_a_data_event(app):
    app.state.daily._save("JP", status="delayed", message="JP 2026-10-02 的数据到 16:30 还没齐")
    feed = TestClient(app).get("/api/today", headers=H).json()
    assert [e for e in feed["events"] if e["kind"] == "data"] == [
        {"kind": "data", "market": "JP", "status": "delayed", "text": "JP 2026-10-02 的数据到 16:30 还没齐", "day": None}]


def test_a_bad_webhook_is_refused_before_it_is_stored(app):
    r = TestClient(app).put("/api/secrets/feishu_webhook", json={"value": "https://example.com/hook"}, headers=H)
    assert r.status_code == 422 and "飞书" in r.json()["detail"]


def test_the_channel_declares_its_settings_and_a_test_card_goes_to_your_bot(app, monkeypatch):
    c = TestClient(app)
    assert c.get("/api/health").json()["registries"]["CHANNELS"] == 1
    ch = c.get("/api/notify", headers=H).json()
    assert [f["name"] for f in ch["channels"]["feishu"]["fields"]] == ["feishu_webhook", "feishu_secret"]
    assert ch["channels"]["feishu"]["ready"] is False and ch["recent"] == []
    r = c.post("/api/notify/test", headers=H)
    assert r.status_code == 409 and "webhook" in r.json()["detail"]
    sent = []
    app.state.feishu_transport = httpx.MockTransport(
        lambda q: sent.append(json.loads(q.content)) or httpx.Response(200, json={"code": 0}))
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    assert c.get("/api/notify", headers=H).json()["channels"]["feishu"]["ready"] is True
    assert c.post("/api/notify/test", headers=H).json() == {"ok": True}
    assert sent[0]["msg_type"] == "interactive" and "测试" in json.dumps(sent[0], ensure_ascii=False)
    app.state.feishu_transport = httpx.MockTransport(lambda q: httpx.Response(200, json={"code": 19021, "msg": "sign match fail"}))
    r = c.post("/api/notify/test", headers=H)
    assert r.status_code == 502 and "签名" in r.json()["detail"]


TUNED_NAME = __import__("yaml").safe_load(TUNED)["name"]


def test_the_screen_is_the_latest_scan_with_its_funnel_quotes_and_columns(app):
    c = TestClient(app)
    assert c.get("/api/screen/above_ma", headers=H).json()["markets"] == {"CN": None, "JP": None}   # not scanned yet
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    c.put("/api/tags/CN:000001", json={"tags": ["银行股"]}, headers=H)
    s = c.get("/api/screen/above_ma", headers=H).json()
    assert {r["listing_id"]: r["tags"] for r in s["rows"]}.get("CN:000001", ["银行股"]) == ["银行股"]   # when listed
    assert s["rule"]["name"] == TUNED_NAME and [x["id"] for x in s["stages"]] == ["today"] and s["stages"][0]["label"]
    cn = s["markets"]["CN"]
    assert cn["funnel"] == [2, cn["counts"]["signal"]] and cn["day"] and s["markets"]["JP"] is None
    r = next(r for r in s["rows"] if r["status"] == "signal")
    assert r["market"] == "CN" and r["name"] and r["close"] > 0 and r["amount"] > 0 and "industry" in r
    assert r["tags"] == []
    assert len(r["spark"]) == 60 and r["spark"][-1] == pytest.approx(r["close"]) and r["depth"] == 0   # 前复权 to the close
    assert c.get("/api/screen/nope", headers=H).status_code == 404


def test_a_notify_switch_takes_true_or_false_and_the_jobs_say_whether_a_bot_is_set(app, monkeypatch):
    c = TestClient(app)
    assert c.put("/api/settings", json={"key": "notify.live", "value": False}, headers=H).json()["notify.live"] is False
    assert c.put("/api/settings", json={"key": "notify.live", "value": "maybe"}, headers=H).status_code == 422
    assert c.get("/api/jobs", headers=H).json()["feishu"] is False
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    assert c.get("/api/jobs", headers=H).json()["feishu"] is True


def test_the_screen_marks_recent_signals_and_names_the_ranking(app):
    from datetime import timedelta

    from wb.data.store import Store
    from wb.engine.l3.run import run_rule
    c = TestClient(app)
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("signal_at: today.at", 'signal_at: today.at\nranking:\n  - {expr: "C / MA(C, $n)", dir: desc}'))
    rule, _, version = Rules(paths).get("above_ma")
    latest = Store(paths, "CN").latest().as_of
    run_rule(paths, rule, "CN", day=latest - timedelta(days=1), version=version)          # yesterday's scan
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    s = c.get("/api/screen/above_ma", headers=H).json()
    assert s["ranking"] == [{"expr": "C / MA(C, $n)", "dir": "desc", "label": "C / MA(C, $n)", "unit": ""}]   # no label: its formula
    r = next(r for r in s["rows"] if r["status"] == "signal")
    assert r["rank0"] > 1 and r["recent_signal"] == str(latest - timedelta(days=1))          # it signalled yesterday too


def test_a_webhook_from_the_env_file_that_is_not_feishus_is_not_ready(app, monkeypatch):
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://example.com/hook")
    c = TestClient(app)
    ch = c.get("/api/notify", headers=H).json()["channels"]["feishu"]
    assert ch["ready"] is False and "飞书" in ch["problem"]
    assert c.get("/api/jobs", headers=H).json()["feishu"] is False


def test_the_screen_says_when_it_lists_only_the_first_rows(app):
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    cn = c.get("/api/screen/every_day?limit=1", headers=H).json()["markets"]["CN"]
    assert (cn["listed"], cn["total"]) == (1, 2)


def test_a_rule_that_fails_in_the_scan_is_a_data_event(app):
    from tests.test_daily import NEEDS_TURNOVER
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(NEEDS_TURNOVER)
    Rules(paths).set_state("needs_turnover", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: not c.get("/api/jobs", headers=H).json()["markets"][0]["running"])
    data = [e for e in c.get("/api/today", headers=H).json()["events"] if e["kind"] == "data"]
    assert any("换手率规则" in (e["text"] or "") for e in data)


def test_a_rule_waiting_for_late_data_is_a_data_event(app):
    d = app.state.daily
    d._save("CN", status="done", scan_at="2026-10-02T16:05:00",
            scan_summary=json.dumps({"cheap": {"name": "便宜的", "version": 1, "waiting": "等估值数据"}}, ensure_ascii=False))
    data = [e for e in TestClient(app).get("/api/today", headers=H).json()["events"] if e["kind"] == "data"]
    assert [e["text"] for e in data] == ["规则「便宜的」等估值数据，到了再扫"]


def test_the_closes_verdicts_on_the_days_intraday_alerts_are_in_the_feed(app):
    # design 14 盘中 flow: 收盘确认 / 撤销 → 写入今日收件箱
    from datetime import datetime

    from wb.devdata import END
    from wb.realtime.alerts import Alerts
    alerts = Alerts(app.state.ctx.paths("CN"))
    for lid in ("CN:600519", "CN:000001"):
        alerts.add(day=END, market="CN", listing_id=lid, kind="rule", key="above_ma", at=datetime(2026, 9, 30, 10, 31),
                   price=10.0, title=lid, text="站上 20 日线 v1 · 盘中满足")
    c = TestClient(app)
    assert c.post("/api/jobs/CN/run", headers=H).json()["started"] is True
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    live = [e for e in c.get("/api/today", headers=H).json()["events"] if e["kind"] == "live"]
    assert {e["listing_id"] for e in live} == {"CN:600519", "CN:000001"}
    assert {e["verdict"] for e in live} <= {"confirmed", "revoked"} and live[0]["at"] == "2026-09-30T10:31:00"
    assert live[0]["rule_id"] == "above_ma" and live[0]["name"] and live[0]["text"] == "站上 20 日线 v1 · 盘中满足"


class Instant(FakePipeline):
    def backfill(self, start, end=None, datasets=None):
        FakePipeline.calls.append(("backfill", start, datasets))


def test_the_data_page_backfills_and_checks_a_sample_in_the_markets_turn(app):
    from datetime import date

    import polars as pl
    app.state.daily = Daily(app.state.ctx.paths, pipeline=lambda p, m: Instant(p, m), allow_demo=True,
                            check=lambda pipe, n, days: pl.DataFrame({"field": ["close"], "ok": [True]}))
    c, FakePipeline.calls = TestClient(app), []
    assert c.post("/api/data/CN/backfill", json={"start": "2016-10-01", "datasets": ["financials"]}, headers=H).json() == {"started": True}
    assert wait_until(lambda: not app.state.daily.running("CN"))
    assert FakePipeline.calls == [("backfill", date(2016, 10, 1), ["financials"])]
    for bad in ({"start": "10/01/2016"}, {"start": "2016-10-01", "datasets": ["bars"]}, {"start": "2999-01-01"},
                {"start": "2016-10-01", "datasets": [["financials"]]}, {"start": "2016-10-01", "datasets": "financials"}):
        assert c.post("/api/data/CN/backfill", json=bad, headers=H).status_code == 422
    assert c.post("/api/data/CN/crosscheck", headers=H).json() == {"started": True}
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["message"] == "抽样核对：1 项全部一致")


def test_demo_data_is_refused_with_the_reason(app):
    app.state.daily = Daily(app.state.ctx.paths, pipeline=lambda p, m: FakePipeline(p, m))
    for url, body in (("/api/data/CN/backfill", {"start": "2016-10-01"}), ("/api/data/CN/crosscheck", None)):
        r = TestClient(app).post(url, json=body, headers=H)
        assert r.status_code == 409 and "演示数据" in r.json()["detail"]


def test_the_settings_page_probes_the_data_sources_again(app, monkeypatch):
    seen = []
    monkeypatch.setattr("wb.api.jobs.probe_market", lambda conn, profile, market: seen.append((profile, market)) or {})
    assert TestClient(app).post("/api/data/JP/probe", headers=H).status_code == 200 and seen == [("test", "JP")]


def test_only_the_financial_reports_need_the_bars_first(tmp_path):
    # a market without data: a reports-only history would be a snapshot with no daily bars to update from
    app = create_app(tmp_path, token="t")
    app.state.daily = Daily(app.state.ctx.paths, pipeline=lambda p, m: Instant(p, m))
    r = TestClient(app).post("/api/data/CN/backfill", json={"start": "2016-10-01", "datasets": ["financials"]}, headers=H)
    assert r.status_code == 422 and "先补日线" in r.json()["detail"]
    assert TestClient(app).post("/api/data/CN/backfill", json={"start": "2016-10-01"}, headers=H).json() == {"started": True}


def test_the_briefing_can_be_previewed_and_demo_data_is_never_sent(tmp_path):
    from wb.config import get_paths
    from wb.devdata import write
    write(get_paths("test", tmp_path), 320)
    c = TestClient(create_app(tmp_path, token="t"))
    b = c.get("/api/brief/CN", headers=H).json()["brief"]
    assert "开盘前" in b["title"] and b["head"][0] == "演示数据：只能预览，不会发到飞书"
    assert c.get("/api/brief/XX", headers=H).status_code == 404                        # as the other market routes say it
    r = c.post("/api/brief/CN/send", headers=H)
    assert r.status_code == 409 and "webhook" in r.json()["detail"]                   # no bot yet
    live = c.get("/api/notify", headers=H).json()["live"]
    assert live["CN"] == {"name": "yfinance_rt", "realtime": False}                    # the test profile's source is late


def test_the_screen_can_keep_only_what_another_rule_also_found(app):
    # 同时满足另一条规则: the listings of this rule's latest scan that the other rule's scan of the same market also hit
    # (命中) or has in 潜伏 too — every one of them, before the page's first rows are taken
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    alone = c.get("/api/screen/every_day", headers=H).json()
    other = {r["listing_id"] for r in c.get("/api/screen/above_ma", headers=H).json()["rows"] if r["status"] == "signal"}
    both = c.get("/api/screen/every_day", params={"also": "above_ma", "also_status": "signal"}, headers=H).json()
    assert {r["listing_id"] for r in both["rows"]} == other < {r["listing_id"] for r in alone["rows"]}
    cn = both["markets"]["CN"]
    assert cn["funnel"][0] == len(other) and cn["also"] == {"day": alone["markets"]["CN"]["day"], "count": len(other), "same_day": True}
    assert both["also"]["id"] == "above_ma" and both["also"]["status"] == "signal" and both["also"]["name"] == TUNED_NAME
    assert c.get("/api/screen/every_day", params={"also": "nope"}, headers=H).status_code == 404
    assert c.get("/api/screen/every_day", params={"also": "above_ma", "also_status": "fail"}, headers=H).status_code == 422


def test_the_screen_can_keep_only_the_stocks_a_formula_holds_for(app):
    # 附加条件: any rule's list cut to the stocks a formula holds for on that scan's day (例如 MA(BAR_LEN, 20) <= 0.03,
    # 近期 K 线短小) — the rows and the funnel are theirs, with 同时满足 too; a formula that cannot be read is said
    from wb.data.store import Store
    from wb.engine.l1.compiler import Compiler
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.engine.l1.run import evaluate
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    alone = c.get("/api/screen/every_day", headers=H).json()
    day = alone["markets"]["CN"]["day"]
    store = Store(paths, "CN")

    def read(formula):   # the formula on that day, read apart from the page
        comp = Compiler("CN", available=available_columns(store))
        comp.add("hit", formula, want="cond")
        out = evaluate(build_frame(store, "CN", paths.profile, comp.program().columns), comp.program())
        return set(out.filter((pl.col("date") == date.fromisoformat(day)) & pl.col("hit"))["listing_id"].to_list())
    formula = "C > MA(C, 5)"
    holds = read(formula)
    got = c.get("/api/screen/every_day", params={"where": formula}, headers=H).json()
    assert {r["listing_id"] for r in got["rows"]} == holds < {r["listing_id"] for r in alone["rows"]}
    cn = got["markets"]["CN"]
    assert cn["funnel"][0] == len(holds) and cn["where"] == [{"count": len(holds), "unknown": cn["where"][0]["unknown"]}]
    assert got["where"] == [{"expr": formula}]
    # several 附加条件: all of them, each one's count said
    a, b = "C > REF(C, 1)", "H / L - 1 < 0.03"
    ha, hb = read(a), read(b)
    two = c.get("/api/screen/every_day", params=[("where", a), ("where", b)], headers=H).json()
    assert {r["listing_id"] for r in two["rows"]} == ha & hb and set() < ha & hb < ha
    assert [w["count"] for w in two["markets"]["CN"]["where"]] == [len(ha), len(hb)]
    assert two["where"] == [{"expr": a}, {"expr": b}]
    other = {r["listing_id"] for r in c.get("/api/screen/above_ma", headers=H).json()["rows"] if r["status"] == "signal"}
    both = c.get("/api/screen/every_day", params={"where": formula, "also": "above_ma"}, headers=H).json()
    assert {r["listing_id"] for r in both["rows"]} == holds & other
    for bad in ("C >", "FOO(C) > 1", "MA(C, 5)"):   # not finished, no such function, not a condition — beside a good one
        r = c.get("/api/screen/every_day", params=[("where", formula), ("where", bad)], headers=H)
        assert r.status_code == 422 and r.json()["detail"].startswith("附加条件") and bad in r.json()["detail"], bad


def test_a_condition_can_be_written_for_each_market(app):
    # A 股流通市值 and 日股总市值 differ (J-Quants gives no free float): `CN: …; JP: …` — each market reads its own part, one
    # its formula does not name is not cut (said: skip); a part without its market, or a market named twice, is an error
    from wb.screening.screen import Unreadable, for_market
    w = "CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000"
    assert for_market(w, "CN") == "CIRC_MV / 1e8 <= 100" and for_market(w, "JP") == "TOTAL_MV / 1e8 <= 1000"
    assert for_market("JP：C > 1", "CN") is None and for_market("C > 1", "JP") == "C > 1"
    for bad in ("CN: C > 1; C > 2", "CN: C > 1; CN: C > 2"):
        with pytest.raises(Unreadable):
            for_market(bad, "CN")
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    plain = c.get("/api/screen/every_day", params={"where": "C > MA(C, 5)"}, headers=H).json()
    each = c.get("/api/screen/every_day", params={"where": "CN: C > MA(C, 5); JP: TOTAL_MV / 1e8 <= 1000"}, headers=H).json()
    assert each["rows"] == plain["rows"] and each["markets"]["CN"]["where"] == plain["markets"]["CN"]["where"]
    alone = c.get("/api/screen/every_day", headers=H).json()
    other = c.get("/api/screen/every_day", params={"where": "JP: C > 1e9"}, headers=H).json()
    assert other["rows"] == alone["rows"] and other["markets"]["CN"]["where"] == [{"skip": True}]
    r = c.get("/api/screen/every_day", params={"where": "CN: C > 1; C > 2"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"].startswith("附加条件")


def test_the_briefings_items_are_put_in_ordered_and_taken_out_through_the_api(app):
    # 2026-10-10: the 选股 view goes into the 开盘前简报 as it is; the list names each the way the briefing will
    c = TestClient(app)
    assert c.get("/api/brief-items", headers=H).json() == []
    body = {"rule_id": "above_ma", "wheres": ["MA(BAR_LEN, 20) <= 0.03"], "mode": "list"}
    [a] = c.post("/api/brief-items", json=body, headers=H).json()
    assert (a["name"], a["wheres"], a["mode"], a["problem"]) == ("站上 20 日线 · 近期 K 线短小", ["MA(BAR_LEN, 20) <= 0.03"], "list", None)
    r = c.post("/api/brief-items", json=body, headers=H)
    assert r.status_code == 422 and "已经在简报里" in r.json()["detail"]
    assert c.post("/api/brief-items", json={"rule_id": "long13"}, headers=H).status_code == 422      # not enabled
    b = c.post("/api/brief-items", json={"rule_id": "above_ma", "mode": "count"}, headers=H).json()[1]
    assert [x["id"] for x in c.post(f"/api/brief-items/{b['id']}/move", json={"step": -1}, headers=H).json()] == [b["id"], a["id"]]
    assert c.put(f"/api/brief-items/{a['id']}", json={"mode": "count"}, headers=H).json()[1]["mode"] == "count"
    assert c.put(f"/api/brief-items/{a['id']}", json={"mode": "all"}, headers=H).status_code == 422
    assert [x["id"] for x in c.delete(f"/api/brief-items/{b['id']}", headers=H).json()] == [a["id"]]
    Rules(app.state.ctx.paths("CN")).set_state("above_ma", enabled=False)
    assert c.get("/api/brief-items", headers=H).json()[0]["problem"] == "没有启用，不在每日扫描里"
    assert c.put("/api/brief-items/999", json={"mode": "list"}, headers=H).status_code == 404


# ---- one industry scheme in 选股 (design 2026-10-12-market-page-redesign §6.1, §6.3) --------------------------------------
@pytest.fixture
def every_day(app):
    """A rule every demo stock signals on, scanned in both markets."""
    paths = app.state.ctx.paths("CN")
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    for m in ("CN", "JP"):
        c.post(f"/api/jobs/{m}/run", headers=H)
        assert wait_until(lambda m=m: next(x for x in c.get("/api/jobs", headers=H).json()["markets"] if x["market"] == m)["status"] == "done")
    return c


def test_screen_rows_carry_shenwan_and_filter_by_industry(every_day):
    c = every_day
    rows = c.get("/api/screen/every_day", headers=H).json()["rows"]
    moutai = next(r for r in rows if r["listing_id"] == "CN:600519")
    assert (moutai["industry"], moutai["industry2"]) == ("食品饮料", "白酒Ⅱ")           # not Tushare's 白酒
    toyota = next(r for r in rows if r["listing_id"] == "JP:7203")
    assert (toyota["industry"], toyota["industry2"]) == ("輸送用機器", None)
    whole = c.get("/api/screen/every_day", headers=H).json()
    got = c.get("/api/screen/every_day", params={"industry": "l2:白酒Ⅱ"}, headers=H).json()
    assert {r["listing_id"] for r in got["rows"]} == {"CN:600519"}                      # 日股 has no 白酒Ⅱ: nothing there
    assert got["industry"] == {"ref": "l2:白酒Ⅱ", "level": "l2", "name": "白酒Ⅱ"}
    assert got["markets"]["CN"]["funnel"][0] == 1 < whole["markets"]["CN"]["funnel"][0]
    jp = c.get("/api/screen/every_day", params={"industry": "l1:輸送用機器"}, headers=H).json()
    assert {r["listing_id"] for r in jp["rows"]} == {"JP:7203"}
    assert c.get("/api/screen/every_day", params={"industry": "白酒"}, headers=H).status_code == 422
    bad = c.get("/api/screen/every_day", params={"industry": "l1:不存在"}, headers=H)
    assert bad.status_code == 422 and "不存在" in bad.json()["detail"]


def test_rows_without_an_industry_on_the_scan_day_show_none(app):
    # 600519's spell ends before the scan day (as a listing not yet classified has none): no industry, no error
    from datetime import timedelta

    from wb.data.store import Store
    from wb.datasets import conform
    paths = app.state.ctx.paths("CN")
    st = Store(paths, "CN")
    old = st.read("industry").filter(pl.col("listing_id") == "CN:600519")
    st.publish({"industry": conform(old.with_columns(pl.lit(old["in_date"][0] + timedelta(days=30)).alias("out_date")), "industry")},
               as_of=st.latest().as_of)
    Rules(paths).save(TUNED.replace("id: above_ma", "id: every_day").replace("C > MA(C, $n)", "C > 0 * MA(C, $n)"))
    Rules(paths).set_state("every_day", enabled=True)
    c = TestClient(app)
    c.post("/api/jobs/CN/run", headers=H)
    assert wait_until(lambda: c.get("/api/jobs", headers=H).json()["markets"][0]["status"] == "done")
    moutai = next(r for r in c.get("/api/screen/every_day", headers=H).json()["rows"] if r["listing_id"] == "CN:600519")
    assert (moutai["industry"], moutai["industry2"]) == (None, None)
