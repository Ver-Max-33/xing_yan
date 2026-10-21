"""The 市场 page's data and your 题材 tags through the API (spec §7.3)."""
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write

H = {"X-WB-Token": "t"}


@pytest.fixture
def client(tmp_path):
    write(get_paths("test", tmp_path), 320)
    return TestClient(create_app(tmp_path, token="t"))


def test_the_market_view_is_served_per_market(client):
    v = client.get("/api/market/CN", headers=H).json()
    assert v["day"] and len(v["breadth"]) == 60 and {i["index_id"] for i in v["indexes"]} == {"CN:IDX:000001", "CN:IDX:000300"}
    assert client.get("/api/market/JP", headers=H).json()["breadth"][-1]["total"] == 3
    assert client.get("/api/market/US", headers=H).status_code == 404


def test_tags_are_set_per_listing_and_listed_for_choosing(client):
    assert client.put("/api/tags/CN:600519", json={"tags": ["白酒", "核心资产", " "]}, headers=H).json() == ["核心资产", "白酒"]
    assert client.get("/api/tags/CN:600519", headers=H).json() == ["核心资产", "白酒"]
    client.put("/api/tags/CN:000001", json={"tags": ["白酒"]}, headers=H)
    assert client.get("/api/tags", headers=H).json()[0] == {"tag": "白酒", "n": 2}
    assert client.put("/api/tags/CN:600519", json={"tags": ['a"b']}, headers=H).status_code == 422
    assert client.put("/api/tags/XX:1", json={"tags": ["x"]}, headers=H).status_code == 422


# ---- the 盘中看板 (design 2026-10-12 §2–§8) ------------------------------------------------------------------------
from datetime import date, datetime, timedelta  # noqa: E402

import polars as pl  # noqa: E402

from tests.test_market_tape import CST, YDAY, FakeTF, limits_of, sess  # noqa: E402
from wb.data.store import Store  # noqa: E402
from wb.market.board import tape_row  # noqa: E402
from wb.market.facts import Facts  # noqa: E402
from wb.workspace.store import Workspace  # noqa: E402


@pytest.fixture
def prod(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_SECRET_TICKFLOW_API_KEY", "k")
    p = get_paths("prod", tmp_path)
    Workspace(p).set_setting("profile.CN", "prod")
    write(p, 320)
    st = Store(p, "CN")
    ahead = [YDAY + timedelta(days=k) for k in range(1, 12)]
    st.publish({"calendar": pl.DataFrame({"date": ahead, "is_open": [d.weekday() < 5 for d in ahead], "source": ["f"] * len(ahead)})}, as_of=YDAY)
    c = TestClient(create_app(tmp_path, token="t"))
    lv = c.app.state.live
    lv.tape.fill, lv.tape.fetch_limits = False, lambda day: limits_of(p)
    return c, p, monkeypatch


def at_time(c, p, monkeypatch, h, m, s=0, day=date(2026, 10, 1), fetch=True):
    """The app's tape fetches from a fake TickFlow at that moment, and the endpoints see that moment."""
    lv = c.app.state.live
    sn, now = sess(p, h, m, s, day)
    if fetch:
        src = FakeTF(p, day)
        src.now = now
        lv.tape.tick("CN", sn, src, now)
    monkeypatch.setattr(lv, "session", lambda market, when: sess(p, h, m, s, day)[0] if market == "CN" else lv.__class__.session(lv, market, when))


def test_board_live_mode_carries_the_board_read_and_yesterday(prod):
    c, p, mp = prod
    lv = c.app.state.live
    at_time(c, p, mp, 10, 0, 5)
    b = c.get("/api/market/CN/board", headers=H).json()
    assert (b["mode"], b["why_not"], b["error"], b["as_of"], b["stale"]) == ("live", None, None, "2026-09-30", False)
    assert b["board"]["traded"] == 2 and b["session"]["label"] == "上午盘" and b["session"]["time"] == "10:00:05"
    assert b["read"] is None and b["yesterday"] is None                  # no record of the day before
    assert isinstance(b["events"], list) and b["phase"] is not None
    assert "mine" not in b and "zone" not in b and "distribution" not in b["board"] and "leader" not in b["board"]   # no lists of yours
    yday = dict(tape_row(lv.tape.latest("CN")["board"], 30), at=datetime(2026, 9, 30, 10, 0, 5), limit_up=10, broken=1, seal_rate=10 / 11)
    lv.tape._put_rows("CN", YDAY, [yday], replace=True)
    b = c.get("/api/market/CN/board", headers=H).json()
    assert b["yesterday"]["limit_up"] == 10 and "sealed_ids" not in b["yesterday"]
    assert b["read"] is not None and b["read"]["label"] in ("偏强", "偏弱", "持平")


def test_board_close_mode_outside_the_session_is_the_stored_days(prod):
    c, p, mp = prod
    at_time(c, p, mp, 10, 0, day=date(2026, 10, 3), fetch=False)         # a Saturday
    b = c.get("/api/market/CN/board", headers=H).json()
    assert b["mode"] == "close" and b["board"]["day"] == "2026-09-30" and b["board"]["at"] == "2026-09-30T15:00:00"
    assert b["board"]["traded"] == 2 and b["board"]["limits"]["known"] is True
    at_time(c, p, mp, 9, 20, fetch=False)                                # the call auction: the last close, said so
    assert c.get("/api/market/CN/board", headers=H).json()["mode"] == "auction"


def test_board_auction_result_before_the_open(prod):
    c, p, mp = prod
    at_time(c, p, mp, 9, 26)
    b = c.get("/api/market/CN/board", headers=H).json()
    assert b["mode"] == "auction_result" and b["board"]["at"] == "2026-10-01T09:26:00"


def test_board_names_a_stale_snapshot(prod):
    c, p, mp = prod
    at_time(c, p, mp, 10, 0, 5, day=date(2026, 10, 6))                   # 10-01 … 10-05 never came in
    b = c.get("/api/market/CN/board", headers=H).json()
    assert (b["mode"], b["stale"], b["as_of"]) == ("live", True, "2026-09-30")
    assert b["board"]["limits"]["pool"] is None and b["board"]["ladder"] == []
    assert b["phase"] is None                                           # the days missing: no phase, no day count (§5.1)
    sh = b["board"]["indexes"]["CN:IDX:000001"]                          # today's change against the quote's own close before
    assert sh["chg"] == pytest.approx(3810.0 / 3800.0 - 1) and sh["dist60"] is None and sh["state"] is None
    assert all(x["rel5"] is None and x["prior15"] is None and x["rel20"] is None and x["tag"] is None and x["amount_ratio"] is None
               for lv in ("l1", "l2") for x in b["board"]["industries"][lv])


def test_jp_without_data_says_so(tmp_path):
    c = TestClient(create_app(tmp_path, token="t"))
    for m, name in (("CN", "A 股"), ("JP", "日股")):
        b = c.get(f"/api/market/{m}/board", headers=H)
        assert b.status_code == 200 and b.json()["board"] is None
        assert b.json()["why_not"] == f"{name}现在的数据档（测试档）里还没有数据"
    assert c.get("/api/market/US/board", headers=H).status_code == 404


def test_jp_board_is_its_close_with_33_industries_and_no_limits(client):
    b = client.get("/api/market/JP/board", headers=H).json()
    assert b["mode"] == "close" and "日股" in b["why_not"]
    assert b["board"]["limits"]["known"] is False and b["phase"] is None and b["events"] == []
    bd = b["board"]   # J-Quants gives no close before: every stock that traded still has its change
    assert bd["traded"] > 0 and bd["up"] + bd["down"] + bd["flat"] == bd["traded"]


def test_tape_gives_today_and_the_day_before(prod):
    c, p, mp = prod
    lv = c.app.state.live
    at_time(c, p, mp, 10, 0, 5)
    at_time(c, p, mp, 10, 1, 5)
    lv.tape._put_rows("CN", YDAY, [dict(tape_row(lv.tape.latest("CN")["board"], i), at=datetime(2026, 9, 30, 9, 30) + timedelta(minutes=i),
                                         limit_up=i) for i in (1, 2, 3)], replace=True)
    t = c.get("/api/market/CN/tape", headers=H).json()
    assert t["day"] == "2026-10-01" and [r["i"] for r in t["rows"]] == [30, 31] and "sealed_ids" not in t["rows"][0]
    assert t["prev_day"] == "2026-09-30" and [(r["i"], r["limit_up"]) for r in t["prev_rows"]] == [(1, 1), (2, 2), (3, 3)]
    assert t["index"]["prev"] > 0


def test_board_carries_yesterdays_whole_amount_and_no_members_any_more(prod):
    c, p, mp = prod
    at_time(c, p, mp, 10, 0, 5)
    b = c.get("/api/market/CN/board", headers=H).json()
    assert b["amount_before"] == pytest.approx(Store(p, "CN").read("bars_1d").filter(pl.col("date") == YDAY)["amount"].sum())
    assert c.get("/api/market/CN/members", headers=H, params={"level": "l1", "name": "银行"}).status_code == 404   # the page stops at industries


# ---- /industries: the board's industries for 选股 and the K-line page (redesign §8) -----------------------------------------
def test_industries_are_the_boards_without_asking_for_10_second_fetches(prod):
    c, p, mp = prod
    at_time(c, p, mp, 10, 0, 5)
    lv = c.app.state.live
    board = c.get("/api/market/CN/board", headers=H).json()["board"]
    lv.tape._wanted.clear()
    got = c.get("/api/market/CN/industries", headers=H).json()
    assert "CN" not in lv.tape._wanted                                   # the record keeps its minute cadence
    assert (got["market"], got["mode"], got["day"], got["time"]) == ("CN", "live", "2026-10-01", "10:00")
    assert [r["name"] for r in got["l1"]] == [r["name"] for r in board["industries"]["l1"]] and len(got["l1"]) == 2
    assert got["of"] == {lv_: sum(r["rank"] is not None for r in board["industries"][lv_]) for lv_ in ("l1", "l2")}
    one = got["l2"][0]
    assert set(one) == {"name", "chg", "rank", "tag", "rel5", "prior15", "rel20", "limit_up", "parent", "small"}
    assert {r["name"]: r["parent"] for r in got["l2"]} == {"白酒Ⅱ": "食品饮料", "股份制银行Ⅱ": "银行"}


def test_industries_outside_the_session_are_the_stored_days(prod):
    c, p, mp = prod
    at_time(c, p, mp, 20, 0, fetch=False)
    got = c.get("/api/market/CN/industries", headers=H).json()
    assert (got["mode"], got["day"], got["time"]) == ("close", "2026-09-30", None)


def test_japan_has_one_level(client):
    got = client.get("/api/market/JP/industries", headers=H).json()
    assert got["l2"] == [] and got["of"]["l2"] == 0 and {r["name"] for r in got["l1"]} == {"輸送用機器", "医薬品", "電気機器"}


def test_watched_prices_in_the_session_are_the_forming_bars(prod):
    """观察's prices in the session are the day's whole-market quote — what the K-line's forming bar reads — with the
    change against the quote's own close before; a stock not traded yet keeps its last daily close (the 观察 list once
    stood at the day before's close all session while the chart moved)."""
    from datetime import datetime

    from wb.realtime.providers.tushare_rt import SNAPSHOT
    c, p, mp = prod
    ws = Workspace(p)
    for lid in ("CN:600519", "CN:000001"):
        ws.add_item(ws.groups()[0]["id"], lid)
    folder = p.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([("CN:600519", datetime(2026, 10, 1, 10, 0, 3), 1500.0, 1520.0, 1490.0, 1510.0, 1e5, 1.5e8, 1495.0)],
                 schema=SNAPSHOT, orient="row").write_parquet(folder / "snapshot=2026-10-01.parquet")
    at_time(c, p, mp, 10, 0, 30, fetch=False)
    items = {i["listing_id"]: i for i in c.get("/api/watchlist", headers=H).json()[0]["items"]}
    assert items["CN:600519"]["close"] == 1510.0 and items["CN:600519"]["change"] == pytest.approx(1510 / 1495 - 1)
    assert items["CN:600519"]["date"] == "2026-10-01" and items["CN:600519"]["at"] == "2026-10-01T10:00:03"   # the quote's own time
    assert items["CN:000001"]["date"] == "2026-09-30"               # not traded yet today: the last daily close
