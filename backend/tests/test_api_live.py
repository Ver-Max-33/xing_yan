"""盘中 through the API (spec §8.5): each market's rows and session now; a stock's minutes of the day with the average
price (VWAP), its previous close and its box's top; and a stream of server-sent events that opens with the state now
and then carries every poll."""
import asyncio
import json
from datetime import date, datetime

import polars as pl
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.realtime.stream import Broadcaster

H = {"X-WB-Token": "t"}


@pytest.fixture
def c(tmp_path):
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    folder = paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True)
    pl.DataFrame([("JP:7203", datetime(2026, 10, 2, 9, k), 100.0, 101.0, 99.0, 100.0 + k, 1000.0 * (k + 1)) for k in range(3)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-10-02.parquet")
    return TestClient(create_app(tmp_path, token="t"))


def test_the_average_price_line_is_the_turnover_over_the_volume_when_the_minutes_have_it(c):
    folder = c.app.state.ctx.paths("JP").market_dir("JP") / "intraday"
    pl.DataFrame([("JP:7203", datetime(2026, 10, 3, 9, k), 100.0, 101.0, 99.0, 100.0 + k, 1000.0, 1000.0 * (99.5 + k))
                  for k in range(2)], schema={**MINUTES, "amount": pl.Float64}, orient="row").write_parquet(folder / "date=2026-10-03.parquet")
    r = c.get("/api/live/JP:7203/minutes", headers=H, params={"day": "2026-10-03"}).json()
    assert [x["vwap"] for x in r["minutes"]] == [pytest.approx(99.5), pytest.approx(100.0)]


def test_each_market_now(c):
    m = {x["market"]: x for x in c.get("/api/live", headers=H).json()["markets"]}
    assert set(m) == {"CN", "JP"} and m["JP"]["session"]["label"] and "rows" in m["JP"] and "indexes" in m["JP"]
    # the status bar says where each market is in its day, on every page
    status = {x["market"]: x for x in c.get("/api/status", headers=H).json()["markets"]}
    assert status["JP"]["session"]["label"] == m["JP"]["session"]["label"]


def test_the_latent_list_of_each_market(c):
    """The 潜伏 stocks you do not watch, by the whole-market quote (盯盘 follows yours only, 10-12): rows and the session;
    an unknown market is not found."""
    r = c.get("/api/live/CN/latent", headers=H)
    assert r.status_code == 200 and set(r.json()) >= {"market", "session", "rows"} and r.json()["market"] == "CN"
    assert c.get("/api/live/US/latent", headers=H).status_code == 404


def test_a_stocks_minutes_of_the_day_with_its_average_price(c):
    r = c.get("/api/live/JP:7203/minutes", headers=H).json()
    assert r["day"] == "2026-10-02" and [x["close"] for x in r["minutes"]] == [100.0, 101.0, 102.0]
    vol = [1000.0, 2000.0, 3000.0]
    assert r["minutes"][-1]["vwap"] == pytest.approx(sum(p * v for p, v in zip([100, 101, 102], vol)) / sum(vol))
    assert r["prev"] is not None and c.get("/api/live/JP:6758/minutes", headers=H).json()["minutes"] == []
    # where each minute falls in the trading day (lunch left out), for the chart's x axis
    assert [x["i"] for x in r["minutes"]] == [0, 1, 2] and r["total"] == 330
    assert r["sessions"] == [["09:00", "11:30"], ["12:30", "15:30"]]


def test_the_feed_opens_with_the_state_now_then_carries_each_poll_and_ends_when_the_page_leaves(c):
    from wb.api.live import sse
    live = c.app.state.live

    async def run():
        feed, left = Broadcaster(), asyncio.Event()

        async def gone():
            return left.is_set()
        it = sse(live, feed, gone, keepalive=0.05)
        hello = await it.__anext__()
        feed.publish({"type": "quotes", "market": "JP", "rows": []})
        quotes = await it.__anext__()
        quiet = await it.__anext__()
        left.set()
        rest = [x async for x in it]
        return hello, quotes, quiet, rest, feed._subs
    hello, quotes, quiet, rest, subs = asyncio.run(run())
    assert hello.startswith("event: hello\ndata: ") and {m["market"] for m in json.loads(hello.split("data: ", 1)[1])["markets"]} == {"CN", "JP"}
    assert quotes.startswith("event: quotes\n") and quiet == ": keep-alive\n\n" and subs == []


def test_a_slow_page_loses_its_oldest_event_never_blocks_the_poller():
    async def run():
        b = Broadcaster(size=2)
        q = b.subscribe()
        for k in range(3):
            b.publish({"k": k})
        await asyncio.sleep(0.01)
        return [q.get_nowait()["k"], q.get_nowait()["k"]]
    assert asyncio.run(run()) == [1, 2]



def test_the_average_price_of_minutes_before_any_volume_is_the_price(c, tmp_path):
    # Yahoo's Tokyo bars start with zero-volume minutes (spec §4.2): 0 / 0 must not leave the average empty
    paths = get_paths("test", tmp_path)
    pl.DataFrame([("JP:6758", datetime(2026, 10, 2, 9, k), 100.0, 101.0, 99.0, 100.0 + k, v) for k, v in enumerate([0.0, 0.0, 1000.0])],
                 schema=MINUTES, orient="row").write_parquet(paths.market_dir("JP") / "intraday" / "date=2026-10-02.parquet")
    r = c.get("/api/live/JP:6758/minutes", headers=H).json()
    assert [x["vwap"] for x in r["minutes"]] == [100.0, 101.0, 102.0]


def test_the_days_alerts_and_muting_a_stock_for_the_rest_of_its_day(c):
    # spec §8.5 预警中心; design 04: 忽略今天 / 今天不再提醒
    from datetime import time

    from wb.realtime.alerts import Alerts
    day = c.app.state.live.session("JP", datetime.now().astimezone()).day
    Alerts(c.app.state.ctx.paths("JP")).add(day=day, market="JP", listing_id="JP:7203", kind="line", key="d1",
                                            at=datetime.combine(day, time(10, 8)), price=2861.0, title="トヨタ自動車 7203 · 日股",
                                            text="涨破你画的线 2,860")
    r = c.get("/api/live/alerts", headers=H).json()
    assert [(a["listing_id"], a["text"]) for a in r["alerts"]] == [("JP:7203", "涨破你画的线 2,860")] and r["muted"] == []
    assert c.post("/api/live/mute", json={"listing_id": "JP:7203"}, headers=H).json() == {"muted": True}
    assert c.get("/api/live/alerts", headers=H).json()["muted"] == ["JP:7203"]
    assert c.post("/api/live/mute", json={"listing_id": "XX:1"}, headers=H).status_code == 422


def test_the_tachibana_session_is_logged_in_when_you_ask_and_only_ever_with_e_shiten(c, monkeypatch):
    # spec §4.8: the transition's phone authentication comes first, so a login is only ever your click
    import wb.realtime.providers.tachibana as tb
    monkeypatch.setattr(tb, "get_secret", lambda name: None)
    tb.SESSIONS.clear()
    assert c.get("/api/live/tachibana", headers=H).json() == {
        "configured": False, "base": tb.DEMO_URL, "demo": True, "logged_in": False, "day": None}
    r = c.post("/api/live/tachibana/login", headers=H)
    assert r.status_code == 422 and "tachibana_auth_id" in r.json()["detail"]
    # the address the auth id goes to: an e支店 one over HTTPS, refused before it is stored
    for bad in ("https://evil.example.com/e_api_v4r9/", "http://kabuka.e-shiten.jp/e_api_v4r9/"):
        assert c.put("/api/secrets/tachibana_url", json={"value": bad}, headers=H).status_code == 422
    monkeypatch.setattr(tb, "get_secret", lambda name: {"tachibana_url": "https://evil.example.com/x/", "tachibana_auth_id": "A",
                                                         "tachibana_key_file": "/nope.pem"}.get(name))
    r = c.post("/api/live/tachibana/login", headers=H)
    assert r.status_code == 422 and "e-shiten.jp" in r.json()["detail"]                 # one set in .env is checked too



def test_a_bad_day_is_refused_and_a_stocks_minutes_do_not_build_the_markets_view(c, monkeypatch):
    # the page has the box top in the stock's row already; a stock just added to 自选 shows on the next ask
    assert c.get("/api/live/JP:7203/minutes?day=../x", headers=H).status_code == 422
    live, built = c.app.state.live, []
    real = live.followed
    monkeypatch.setattr(live, "followed", lambda m: built.append(m) or real(m))
    for _ in range(3):
        c.get("/api/live/JP:7203/minutes", headers=H)
    assert built == [] and "top" not in c.get("/api/live/JP:7203/minutes", headers=H).json()
    c.get("/api/live", headers=H)
    assert built.count("JP") == 1


def test_the_k_line_can_end_with_todays_bar_still_forming(c, monkeypatch):
    # spec §8.5: asked for (the 盘中未确认 switch), the bars end with today's, marked, its indicators counting it; the
    # structures as they stand now count it too — the boxes it breaks this morning show broken
    last = c.get("/api/bars?listing_id=JP:7203&period=day&basis=raw&limit=1", headers=H).json()["rows"][-1]["close"]
    bar = {"date": date(2026, 10, 2), "open": last, "high": last * 1.3, "low": last, "close": last * 1.28, "volume": 6e7, "amount": 1e9}
    monkeypatch.setattr(c.app.state.live, "temp_day", lambda lid, now=None: bar if lid == "JP:7203" else None)
    rows = c.get("/api/bars?listing_id=JP:7203&period=day&basis=raw&limit=3&live=true", headers=H).json()["rows"]
    assert rows[-1]["date"] == "2026-10-02" and rows[-1]["temp"] is True and rows[-1]["close"] == pytest.approx(last * 1.28)
    assert rows[-1]["ma5"] is not None and [r["temp"] for r in rows[:-1]] == [False, False]
    plain = c.get("/api/bars?listing_id=JP:7203&period=day&basis=raw&limit=3", headers=H).json()["rows"]
    assert plain[-1]["date"] == "2026-09-30" and plain[-1]["temp"] is False
    week = c.get("/api/bars?listing_id=JP:7203&period=week&basis=raw&limit=2&live=true", headers=H).json()["rows"]
    assert week[-1]["temp"] is True and week[-1]["complete"] is False

    def broken(q):
        boxes = c.get(f"/api/structures?listing_id=JP:7203{q}", headers=H).json()["boxes"]
        return [e for b in boxes for e in b["events"] if e["date"] == "2026-10-02" and e["event"] == "break_up"]
    assert broken("&live=true") and not broken("")


def test_a_minute_k_lines_structures_are_found_on_its_own_bars(c):
    """5 分钟 K 线's own boxes (design 2026-10-12-structures-every-period): on the chart's 5-minute bars, dated as they are
    (their end), the band a 5-minute bar's (0.17%) but never under two ticks (Toyota at 770 yen: 2 yen, 0.26%)."""
    import math
    from datetime import timedelta

    from wb.engine.l2.periods import settings
    folder = c.app.state.live.paths_of("JP").market_dir("JP") / "intraday"
    px = lambda k: 770.0 + 3.0 * math.sin(k / 4.0) if k < 240 else 776.0 + 0.4 * (k - 240)   # noqa: E731 - a box, then up
    ats = [datetime(2026, 9, 30, 9, 0) + timedelta(minutes=k) for k in range(150)] + \
          [datetime(2026, 9, 30, 12, 30) + timedelta(minutes=k) for k in range(180)]
    pl.DataFrame([("JP:7203", a, px(k), px(k) + 0.5, px(k) - 0.5, px(k), 1000.0) for k, a in enumerate(ats)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-09-30.parquet")
    q = {"listing_id": "JP:7203", "basis": "raw", "period": "m5"}
    s = c.get("/api/structures", params=q, headers=H).json()
    bars = {r["date"] for r in c.get("/api/bars", params={**q, "limit": 5000}, headers=H).json()["rows"]}
    assert s["period"] == "m5" and s["boxes"] and {b["start"] for b in s["boxes"]} | {b["known_at"] for b in s["boxes"]} <= bars
    assert s["tol"] == pytest.approx(settings("m5", price=770.0, tick=1)["BOX"]["tol"], rel=0.02)


def test_the_live_pages_small_boxes_are_the_minute_k_lines_own(tmp_path):
    """盯盘's 分时小箱 and the 5 分钟 K 线 find the same boxes in the same minutes (one setting for each period, design
    2026-10-12-structures-every-period) — 盯盘 stamps a bar by its start, the K-line by its end."""
    import math
    from datetime import timedelta
    paths = get_paths("test", tmp_path)   # no 10-02 minutes (the fixture's): the K-line reads the calendar's days only
    write(paths, 320)
    folder = paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True)
    c = TestClient(create_app(tmp_path, token="t"))
    px = lambda k: 770.0 + 3.0 * math.sin(k / 4.0) if k < 240 else 776.0 + 0.4 * (k - 240)   # noqa: E731
    ats = [datetime(2026, 9, 30, 9, 0) + timedelta(minutes=k) for k in range(150)] + \
          [datetime(2026, 9, 30, 12, 30) + timedelta(minutes=k) for k in range(180)]
    pl.DataFrame([("JP:7203", a, px(k), px(k) + 0.5, px(k) - 0.5, px(k), 1000.0) for k, a in enumerate(ats)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-09-30.parquet")
    k_line = [b for b in c.get("/api/structures", params={"listing_id": "JP:7203", "basis": "raw", "period": "m5"}, headers=H).json()["boxes"]
              if b["superseded_at"] is None]   # 盯盘 draws each box as it stands now: its versions before are not sent
    live = c.get("/api/live/JP:7203/micro?period=5", headers=H).json()["boxes"]
    micro = c.get("/api/live/JP:7203/micro?period=5", headers=H).json()
    assert {e["box_id"] for e in micro["events"]} <= {b["box_id"] for b in micro["boxes"]}   # no event of a box not sent
    end = lambda t: (datetime.fromisoformat(t) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")   # noqa: E731
    assert k_line and sorted((b["scale"], b["start"], round(b["segments"][0]["top"], 6)) for b in k_line) == \
        sorted((b["scale"], end(b["start"]), round(b["top"], 6)) for b in live)


def test_a_stocks_small_boxes_in_its_minutes(c, monkeypatch):
    # spec §8.5 分时小箱: 1, 5 and 15-minute bars of the minute K-line's days, the boxes in them and each break's volume —
    # the kept minutes are 10-02's (not a day of the demo calendar): the session's own, after its close
    from wb.realtime.session import Session
    monkeypatch.setattr(c.app.state.live, "session", lambda m, now: Session(m, date(2026, 10, 2), "after", 330, 330, "收盘", now))
    r = c.get("/api/live/JP:7203/micro?period=5", headers=H).json()
    assert r["period"] == 5 and r["bars"] and set(r) >= {"bars", "boxes", "events", "waves", "gold"}
    one = c.get("/api/live/JP:7203/micro?period=1", headers=H).json()      # the day's waves; outside its session none is going
    assert one["waves"] and not any(w["open"] for w in one["waves"])
    assert c.get("/api/live/JP:7203/micro?period=7", headers=H).status_code == 422


def test_a_small_box_bar_is_still_forming_only_on_the_day_that_trades(c, monkeypatch):
    # the kept minutes are 10-02's; on a later day's session nothing of theirs is still forming
    from wb.realtime.session import Session
    live = c.app.state.live
    monkeypatch.setattr(live, "session", lambda m, now: Session(m, date(2026, 10, 5), "trading", 30, 330, "前场", now))
    assert not any(b["partial"] for b in c.get("/api/live/JP:7203/micro?period=1", headers=H).json()["bars"])
    monkeypatch.setattr(live, "session", lambda m, now: Session(m, date(2026, 10, 2), "trading", 3, 330, "前场", now))
    r = c.get("/api/live/JP:7203/micro?period=1", headers=H).json()
    assert [b["partial"] for b in r["bars"]] == [False, False, True] and r["waves"][-1]["open"] is True


def test_a_stocks_minutes_over_several_days_side_by_side(c, tmp_path):
    # the last days' 分时 shrunk side by side — each day on its own clock, its own average, one after another
    folder = get_paths("test", tmp_path).market_dir("JP") / "intraday"
    pl.DataFrame([("JP:7203", datetime(2026, 10, 1, 9, k), 90.0, 91.0, 89.0, 90.0 + k, 1000.0) for k in range(2)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-10-01.parquet")
    one = c.get("/api/live/JP:7203/minutes", headers=H).json()
    two = c.get("/api/live/JP:7203/minutes?days=2", headers=H).json()
    assert one["days"] == ["2026-10-02"] and two["days"] == ["2026-10-01", "2026-10-02"] and two["day"] == "2026-10-02"
    assert [x["i"] for x in two["minutes"]] == [0, 1, 330, 331, 332] and [x["d"] for x in two["minutes"]] == [0, 0, 1, 1, 1]
    assert two["minutes"][1]["vwap"] == pytest.approx(90.5) and two["minutes"][2]["vwap"] == 100.0   # each day's own average
    assert two["prev"] == one["prev"]                                                                 # the close before the last day
    assert c.get("/api/live/JP:7203/minutes?days=21", headers=H).status_code == 422


def test_a_day_without_this_stock_keeps_its_place_among_the_days_shown(c, tmp_path):
    # a day it was not followed (or was halted): its slot stays, empty, and the days after it keep their places
    folder = get_paths("test", tmp_path).market_dir("JP") / "intraday"
    pl.DataFrame([("JP:6758", datetime(2026, 10, 1, 9, 0), 90.0, 91.0, 89.0, 90.0, 1000.0)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-10-01.parquet")
    two = c.get("/api/live/JP:7203/minutes?days=2", headers=H).json()
    assert two["days"] == ["2026-10-01", "2026-10-02"] and {x["d"] for x in two["minutes"]} == {1}
    assert min(x["i"] for x in two["minutes"]) == 330


def test_any_a_share_on_the_k_line_has_todays_bar_and_registers_its_view(tmp_path, monkeypatch):
    # design 2026-10-12 §9: not in any watch group, yet during the session its forming bar is there (from the day's
    # whole-market snapshot) and opening its chart or its 分时 puts it first in 盯盘's minutes poll
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from wb.data.store import Store
    from wb.realtime.providers.tushare_rt import SNAPSHOT
    from wb.realtime.session import session_at
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    st = Store(paths, "CN")
    as_of = st.latest().as_of
    ahead = [as_of + timedelta(days=k) for k in range(1, 12)]
    st.publish({"calendar": pl.DataFrame({"date": ahead, "is_open": [d.weekday() < 5 for d in ahead], "source": ["f"] * len(ahead)})}, as_of=as_of)
    day = next(d for d in ahead if d.weekday() < 5)
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True)
    pl.DataFrame([("CN:000001", datetime(day.year, day.month, day.day, 10, 0, 3), 11.1, 11.4, 11.0, 11.3, 2.5e6, 2.8e7, 11.0)],
                 schema=SNAPSHOT, orient="row").write_parquet(folder / f"snapshot={day}.parquet")
    c = TestClient(create_app(tmp_path, token="t"))
    now = datetime(day.year, day.month, day.day, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    cal = st.read("calendar")
    opens = dict(zip(cal["date"], cal["is_open"], strict=True))
    lv = c.app.state.live
    monkeypatch.setattr(lv, "session", lambda market, when: session_at(market, now, lambda d: opens.get(d)))
    rows = c.get("/api/bars", headers=H, params={"listing_id": "CN:000001", "live": "true"}).json()["rows"]
    assert rows[-1]["date"] == day.isoformat() and rows[-1]["temp"] is True
    assert lv._viewed("CN", now) == ["CN:000001"]
    c.get("/api/live/CN:600519/minutes", headers=H)
    assert lv._viewed("CN", now) == ["CN:600519", "CN:000001"]


def test_a_minute_k_line_is_its_kept_minutes_cut_into_bars_and_its_last_forms(c, monkeypatch):
    """1 / 5 / 15 / 30 / 60 分钟 on the K-line page (10-12): /api/bars with period m5 — the stock's minutes cut into 5-minute
    bars stamped with their end; with live, the last one forming while the session trades (9:03: the 9:00–9:05 bar)."""
    from wb.realtime.session import session_at
    lv = c.app.state.live
    monkeypatch.setattr(lv, "session", lambda market, when=None: session_at(market, datetime(2026, 10, 2, 9, 3, tzinfo=__import__(
        "zoneinfo").ZoneInfo("Asia/Tokyo")), lambda d: d.weekday() < 5))
    monkeypatch.setattr(lv, "source_for", lambda p, m: type("NoHistory", (), {"minutes": lambda self, ids, day, ix=(): pl.DataFrame(schema=MINUTES)})())
    pl.DataFrame([("JP:7203", datetime(2026, 9, 30, 14, k), 90.0, 91.0, 89.0, 90.0, 500.0) for k in range(5)], schema=MINUTES,
                 orient="row").write_parquet(lv.paths_of("JP").market_dir("JP") / "intraday" / "date=2026-09-30.parquet")
    q = {"listing_id": "JP:7203", "period": "m5", "basis": "raw"}
    r = c.get("/api/bars", headers=H, params={**q, "live": "true"}).json()
    assert r["period"] == "m5" and not r["more"] and [x["date"] for x in r["rows"]] == ["2026-09-30T14:05", "2026-10-02T09:05"]
    last = r["rows"][-1]
    assert last["temp"] is True and not r["rows"][0]["temp"]
    assert (last["open"], last["close"], last["volume"]) == (100.0, 102.0, 6000.0)   # the three minutes 9:00–9:02
    # the newest `limit` (the chart's refresh asks for 2), never "more": a minute chart has no older pages to load
    one = c.get("/api/bars", headers=H, params={**q, "live": "true", "limit": 1}).json()
    assert [x["date"] for x in one["rows"]] == ["2026-10-02T09:05"] and not one["more"]
    off = c.get("/api/bars", headers=H, params=q).json()   # without live: the days the daily chart has
    assert [x["date"] for x in off["rows"]] == ["2026-09-30T14:05"] and not off["rows"][0]["temp"]


def test_the_live_pages_small_boxes_read_the_k_lines_twenty_days(tmp_path):
    """Review 10-13: 盯盘's 分时小箱 read every day kept (from 09-07 on the user's data), the minute K-line its last 20
    trading days — so their boxes differed (the two-tick band is of the window's price too): both read the K-line's days."""
    import math
    from datetime import timedelta
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    folder = paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True)
    c = TestClient(create_app(tmp_path, token="t"))
    for day, base in ((datetime(2026, 8, 20), 700.0), (datetime(2026, 9, 30), 770.0)):   # 08-20: kept, 20 days back and more
        px = lambda k, b=base: b + 3.0 * math.sin(k / 4.0) if k < 240 else b + 6.0 + 0.4 * (k - 240)   # noqa: E731
        ats = [day.replace(hour=9) + timedelta(minutes=k) for k in range(150)] + \
              [day.replace(hour=12, minute=30) + timedelta(minutes=k) for k in range(180)]
        pl.DataFrame([("JP:7203", a, px(k), px(k) + 0.5, px(k) - 0.5, px(k), 1000.0) for k, a in enumerate(ats)],
                     schema=MINUTES, orient="row").write_parquet(folder / f"date={day.date()}.parquet")
    k_line = [b for b in c.get("/api/structures", params={"listing_id": "JP:7203", "basis": "raw", "period": "m5"}, headers=H).json()["boxes"]
              if b["superseded_at"] is None]
    live = c.get("/api/live/JP:7203/micro?period=5", headers=H).json()["boxes"]
    end = lambda t: (datetime.fromisoformat(t) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")   # noqa: E731
    assert k_line and sorted((b["scale"], b["start"], round(b["segments"][0]["top"], 6)) for b in k_line) == \
        sorted((b["scale"], end(b["start"]), round(b["top"], 6)) for b in live)


def test_a_minute_k_lines_indicators_count_the_kept_days_before_those_it_shows(c):
    """You chose (10-13): a minute chart's MACD, KDJ and MA60 are worked out on every day kept (the warm-up 通达信 has)
    and the chart shows its period's days — 5 分钟 10 days: its first bar's MA60 is known, not 59 bars blank."""
    from datetime import timedelta

    from wb.charts.series import MINUTE_DAYS
    folder = c.app.state.live.paths_of("JP").market_dir("JP") / "intraday"
    days = [d for d in (date(2026, 9, 30) - timedelta(days=k) for k in range(30)) if d.weekday() < 5][:15]
    for k, d in enumerate(days):
        ats = [datetime(d.year, d.month, d.day, 9, 0) + timedelta(minutes=m) for m in range(150)] + \
              [datetime(d.year, d.month, d.day, 12, 30) + timedelta(minutes=m) for m in range(180)]
        pl.DataFrame([("JP:7203", a, 100.0 + k, 101.0 + k, 99.0 + k, 100.0 + k, 1000.0) for a in ats], schema=MINUTES,
                     orient="row").write_parquet(folder / f"date={d}.parquet")
    rows = c.get("/api/bars", params={"listing_id": "JP:7203", "basis": "raw", "period": "m5", "limit": 5000}, headers=H).json()["rows"]
    shown = sorted({r["date"][:10] for r in rows})
    assert len(shown) == MINUTE_DAYS["m5"] and rows[0]["ma60"] is not None
