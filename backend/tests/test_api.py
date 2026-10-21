from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app, default_index
from wb.config import get_paths
from wb.data.store import Store
from wb.datasets import conform
from wb.workspace.store import Workspace

DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(40) if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5]


@pytest.fixture
def client(tmp_path):
    paths = get_paths("test", tmp_path)
    st = Store(paths, "JP")
    n = len(DAYS)
    bars = conform(pl.DataFrame({
        "listing_id": "JP:7203", "date": DAYS, "open": [2800.0 + i for i in range(n)],
        "high": [2810.0 + i for i in range(n)], "low": [2790.0 + i for i in range(n)],
        "close": [2805.0 + i for i in range(n)], "volume": [1e6] * n, "amount": [2.8e9] * n,
        "adj_factor": 1.0, "share_factor": 1.0, "suspended": False, "source": "test"}), "bars_1d")
    inst = conform(pl.DataFrame({"listing_id": ["JP:7203", "JP:130A"], "market": "JP", "code": ["7203", "130A"],
                                 "name": ["トヨタ自動車", "Ｖｅｒｉｔａｓ"], "name_en": ["TOYOTA MOTOR", "Veritas"],
                                 "status": "L"}), "instruments")
    cal = conform(pl.DataFrame({"date": DAYS, "is_open": True}), "calendar")
    idx = conform(pl.DataFrame({"index_id": "JP:IDX:N225", "date": DAYS, "open": 38000.0, "high": 38100.0,
                                "low": 37900.0, "close": 38050.0}), "index_bars_1d")
    st.publish({"bars_1d": bars, "instruments": inst, "calendar": cal, "index_bars_1d": idx}, as_of=DAYS[-1])
    return TestClient(create_app(tmp_path, token="secret-token")), paths


H = {"X-WB-Token": "secret-token"}


def test_token_required_except_health(client):
    c, _ = client
    assert c.get("/api/health").json()["ok"]
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"X-WB-Token": "wrong"}).status_code == 401
    assert c.get("/api/status", headers=H).status_code == 200
    c.cookies.set("wb_token", "secret-token")
    assert c.get("/api/status").status_code == 200


def test_status_reports_snapshot_per_market(client):
    c, _ = client
    st = {m["market"]: m for m in c.get("/api/status", headers=H).json()["markets"]}
    assert st["JP"]["snapshot"]["as_of"] == str(DAYS[-1]) and st["CN"]["snapshot"] is None


def test_search_and_instrument(client):
    c, _ = client
    assert [r["listing_id"] for r in c.get("/api/search", params={"q": "130a"}, headers=H).json()] == ["JP:130A"]
    inst = c.get("/api/instrument/JP:7203", headers=H).json()
    assert inst["name"] == "トヨタ自動車" and inst["index_id"] == "JP:IDX:N225" and inst["quality"] == []
    assert c.get("/api/instrument/JP:9999", headers=H).status_code == 404


def test_bars_paging_and_indicators(client):
    c, _ = client
    r = c.get("/api/bars", params={"listing_id": "JP:7203", "limit": 10}, headers=H).json()
    assert len(r["rows"]) == 10 and r["more"] and r["adjusted"]
    last = r["rows"][-1]
    assert last["date"] == str(DAYS[-1]) and last["ma5"] == pytest.approx(sum(2805.0 + i for i in range(len(DAYS) - 5, len(DAYS))) / 5)
    first = date.fromisoformat(r["rows"][0]["date"])
    older = c.get("/api/bars", params={"listing_id": "JP:7203", "end": str(first - timedelta(days=1))}, headers=H).json()
    assert len(older["rows"]) == len(DAYS) - 10 and not older["more"]
    wk = c.get("/api/bars", params={"listing_id": "JP:7203", "period": "week"}, headers=H).json()
    assert wk["rows"][-1]["complete"] in (True, False) and len(wk["rows"]) < len(DAYS)
    assert c.get("/api/bars", params={"listing_id": "JP:7203", "period": "hour"}, headers=H).status_code == 422


def test_index_bars(client):
    c, _ = client
    r = c.get("/api/index_bars", params={"index_id": "JP:IDX:N225"}, headers=H).json()
    assert len(r["rows"]) == len(DAYS) and r["rows"][-1]["close"] == 38050.0


def test_watchlist_with_quotes(client):
    c, _ = client
    groups = c.get("/api/watchlist", headers=H).json()
    gid = groups[0]["id"]
    assert c.post(f"/api/watchlist/{gid}/items", json={"listing_id": "JP:7203"}, headers=H).json()["ok"]
    item = c.get("/api/watchlist", headers=H).json()[0]["items"][0]
    assert item["listing_id"] == "JP:7203" and item["name"] == "トヨタ自動車"
    assert item["close"] == 2805.0 + len(DAYS) - 1 and item["change"] == pytest.approx(1 / (2805.0 + len(DAYS) - 2))
    c.delete(f"/api/watchlist/{gid}/items/JP:7203", headers=H)
    assert c.get("/api/watchlist", headers=H).json()[0]["items"] == []


def test_drawings_settings_secrets(client, monkeypatch):
    c, paths = client
    body = {"listing_id": "JP:7203", "kind": "segment", "points": [{"date": "2026-09-01", "price": 2800.0}]}
    assert c.put("/api/drawings/d1", json=body, headers=H).json()["id"] == "d1"
    assert c.get("/api/drawings", params={"listing_id": "JP:7203"}, headers=H).json()[0]["kind"] == "segment"
    assert c.put("/api/drawings/d2", json={"listing_id": "JP:7203"}, headers=H).status_code == 422
    s = c.put("/api/settings", json={"key": "profile.CN", "value": "prod"}, headers=H).json()
    assert s["profile.CN"] == "prod" and Workspace(paths).profile("CN") == "prod"
    assert c.put("/api/settings", json={"key": "profile.CN", "value": "x"}, headers=H).status_code == 422
    monkeypatch.setenv("WB_SECRET_TUSHARE_TOKEN", "abc")
    secrets = c.get("/api/secrets", headers=H).json()
    assert secrets["tushare_token"] == "env" and "abc" not in str(secrets)
    r = c.put("/api/secrets/tushare_token", json={"value": "new"}, headers=H)   # the environment's: not changed from here
    assert r.status_code == 422 and "环境变量" in r.json()["detail"]
    assert c.put("/api/secrets/deepseek_api_key", json={"value": "sk-1"}, headers=H).json()["deepseek_api_key"] == "keychain"


def test_the_data_uses_of_each_source_and_where_each_market_s_prices_come_from(client):
    # spec §4.10: the four switches of every source, changed through the settings; each market's source in its profile
    c, paths = client
    d = c.get("/api/datause", headers=H).json()
    jq = next(s for s in d["sources"] if s["id"] == "jquants")
    assert (jq["name"], jq["store"], jq["llm"], jq["notify"], jq["export"]) == ("J-Quants", True, True, True, False)
    assert d["markets"] == {"CN": "tushare", "JP": "yfinance"}                           # the test profile's daily sources
    c.put("/api/settings", json={"key": "datause.yfinance.export", "value": False}, headers=H)
    assert next(s for s in c.get("/api/datause", headers=H).json()["sources"] if s["id"] == "yfinance")["export"] is False


def test_default_index_by_board():
    assert default_index("CN:600519", "prod") == "CN:IDX:000001"
    assert default_index("CN:688001", "prod") == "CN:IDX:000688"
    assert default_index("CN:300750", "prod") == "CN:IDX:399006"
    assert default_index("CN:000001", "prod") == "CN:IDX:399001"
    assert default_index("CN:830799", "prod") == "CN:IDX:899050"
    assert default_index("JP:7203", "prod") == "JP:IDX:TOPIX" and default_index("JP:7203", "test") == "JP:IDX:N225"


def test_instrument_reports_quality_issues(client):
    c, paths = client
    from wb.data.quality import Issue

    st = Store(paths, "JP")
    snap = st.publish({"bars_1d": conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [DAYS[-1]], "close": [1.0]}),
                                          "bars_1d")}, as_of=DAYS[-1],
                      issues={"bars_1d": [Issue("adj_identity", "row", "x", "JP:7203", DAYS[-2]),
                                          Issue("adj_identity", "row", "x", "JP:7203", DAYS[-1])]})
    q = c.get("/api/instrument/JP:7203", headers=H).json()["quality"]
    assert q == [{"check_name": "adj_identity", "n": 2, "first": str(DAYS[-2]), "last": str(DAYS[-1])}] and snap


def test_zero_volume_day_does_not_break_the_chart(client):
    c, paths = client
    st = Store(paths, "JP")
    n = len(DAYS)
    vol = [1e6] * n
    vol[5] = 0.0  # a day without trades (yfinance writes 0)
    bars = conform(pl.DataFrame({"listing_id": "JP:130A", "date": DAYS, "open": 500.0, "high": 510.0, "low": 490.0,
                                 "close": 500.0, "volume": vol, "adj_factor": 1.0, "share_factor": 1.0}), "bars_1d")
    st.publish({"bars_1d": bars}, as_of=DAYS[-1])
    r = c.get("/api/bars", params={"listing_id": "JP:130A"}, headers=H)
    assert r.status_code == 200
    assert r.json()["rows"][6]["vol_mult"] is None
    assert r.json()["scales"] == [[DAYS[0].isoformat(), 1.0]]


def test_health_counts_every_registry_and_startup_logs_them(tmp_path, caplog):
    # spec §3.5: the counts are compared between the packaged app and development (P4), so they must
    # be the full registries, not whatever happened to be imported
    import logging

    from fastapi.testclient import TestClient

    from wb import registry
    from wb.api.app import create_app

    with caplog.at_level(logging.INFO, logger="wb"):
        client = TestClient(create_app(tmp_path))
    counts = client.get("/api/health").json()["registries"]
    registry.load_all()
    assert counts == registry.counts() and min(counts.values()) > 0
    assert any("registries" in r.message for r in caplog.records)


def test_bad_requests_get_clear_answers_not_500(client):
    c, _ = client
    h = {"x-wb-token": "secret-token"}
    assert c.get("/api/bars", params={"listing_id": "NOPE", "period": "day"}, headers=h).status_code == 422
    assert c.get("/api/instrument/XX:1", headers=h).status_code == 422
    assert c.post("/api/watchlist/groups", json={"name": "自选"}, headers=h).status_code == 409  # exists already
    assert c.get("/api/status", headers={"x-wb-token": "tökén".encode("latin-1")}).status_code == 401  # non-ASCII
    assert c.put("/api/settings", json={"key": "theme", "value": "purple"}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"key": "colors", "value": 3}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"key": "theme", "value": "light"}, headers=h).status_code == 200


def test_the_quality_badge_counts_the_latest_snapshot_only(client):
    c, paths = client
    from wb.data.quality import Issue

    st = Store(paths, "JP")
    bars = conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [DAYS[-1]], "close": [1.0]}), "bars_1d")
    st.publish({"bars_1d": bars}, as_of=DAYS[-1], issues={"bars_1d": [Issue("adj_identity", "row", "x", "JP:7203", DAYS[-2])]})
    fixed = conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [DAYS[-1]], "close": [1.1]}), "bars_1d")
    st.publish({"bars_1d": fixed}, as_of=DAYS[-1])                      # corrected in the next snapshot
    q = c.get("/api/instrument/JP:7203", headers=H).json()["quality"]
    assert all(i["first"] != str(DAYS[-2]) for i in q)                  # the old snapshot's issue no longer warns


def test_the_jp_board_index_is_the_market_files_benchmark():
    from wb.markets import load_market
    assert [default_index("JP:7203", p) for p in ("test", "prod")] == [load_market("JP").benchmark[p] for p in ("test", "prod")]


def test_each_datasets_capability_comes_with_its_last_fetch_error(client):
    # spec §4.9: per dataset and market — the source, the history start, the latest date, the rate, the last error
    from wb.data.capability import record_error, save_probes
    from wb.data.providers.base import Probe
    c, paths = client
    conn = Store(paths, "JP").conn
    save_probes(conn, "yfinance", "JP", [Probe("bars_1d", True, date(2000, 1, 4), None, 60, "")])
    record_error(conn, "JP", "yfinance", "bars_1d", "2026-09-30", "HTTPError: 429 Too Many Requests")
    [row] = c.get("/api/capabilities", params={"market": "JP"}, headers=H).json()
    assert row["last_error"] == "HTTPError: 429 Too Many Requests" and row["last_error_unit"] == "2026-09-30" and row["last_error_at"]


def test_a_cn_daily_bar_carries_its_turnover(tmp_path):
    # 筹码分布 (spec §8.6) spreads each day's turnover: the chart needs 换手率 with the bars (valuation, CN)
    paths = get_paths("test", tmp_path)
    st = Store(paths, "CN")
    n = len(DAYS)
    bars = conform(pl.DataFrame({
        "listing_id": "CN:600000", "date": DAYS, "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
        "volume": 1e6, "amount": 1e7, "adj_factor": 1.0, "share_factor": 1.0, "suspended": False, "source": "test"}), "bars_1d")
    val = conform(pl.DataFrame({"listing_id": "CN:600000", "date": DAYS[1:], "turnover_rate": [0.5 + i / 100 for i in range(n - 1)],
                                "source": "test"}), "valuation")
    inst = conform(pl.DataFrame({"listing_id": ["CN:600000"], "market": "CN", "code": ["600000"], "name": ["浦发银行"],
                                 "status": "L"}), "instruments")
    cal = conform(pl.DataFrame({"date": DAYS, "is_open": True}), "calendar")
    st.publish({"bars_1d": bars, "valuation": val, "instruments": inst, "calendar": cal}, as_of=DAYS[-1])
    c = TestClient(create_app(tmp_path, token="secret-token"))
    rows = c.get("/api/bars", params={"listing_id": "CN:600000"}, headers=H).json()["rows"]
    assert rows[0]["turnover_rate"] is None and rows[1]["turnover_rate"] == pytest.approx(0.5) and rows[-1]["turnover_rate"] == pytest.approx(0.5 + (n - 2) / 100)
    wk = c.get("/api/bars", params={"listing_id": "CN:600000", "period": "week"}, headers=H).json()["rows"]
    assert all(r["turnover_rate"] is None for r in wk)   # a week's chips are not drawn


def test_the_k_line_of_a_beijing_listing_starts_on_its_first_day_there(tmp_path):
    """You chose (10-13): its 新三板 years are another market's — the K-line, its indicators and structures start where the
    rules do."""
    from tests.test_l1_frame import beijing
    from wb.devdata import write
    from wb.config import get_paths
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    days = sorted(Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == lid)["date"].to_list())
    beijing(paths, lid, days[100])
    c = TestClient(create_app(tmp_path, token="t"))
    rows = c.get("/api/bars", params={"listing_id": lid, "limit": 5000}, headers={"X-WB-Token": "t"}).json()["rows"]
    assert rows[0]["date"] == days[100].isoformat() and len(rows) == len(days) - 100
