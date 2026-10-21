"""盘中 (spec §4.8, §8.5): while a market trades, its realtime source is asked for today's minutes of the stocks you
follow — your watch groups and the enabled rules' 潜伏 lists — and each stock's row carries its change on the previous
close, the intraday volume ratio ((today's volume ÷ minutes traded) ÷ (5-day average ÷ minutes in the day)) and its
distance to the box's top; the day's minutes are kept in a file; outside the session the rows are the close."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from wb.data.store import Store
from wb.db import connect
from wb.devdata import write
from wb.jobs.live import Live, box_top
from wb.notify.outbox import Outbox
from wb.realtime.alerts import Alerts
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.workspace.rules import Rules
from wb.workspace.store import Workspace
from wb.workspace.watch import Watch

TK = ZoneInfo("Asia/Tokyo")
DAY = date(2026, 10, 2)


class Fake:
    """Two minutes for each stock asked for: 100 → 110, 3,000 shares each; indexes too."""
    name, delay, poll_seconds, max_listings, approx_volume = "fake", 0, 60, None, False

    def __init__(self):
        self.asked = []

    def minutes(self, ids, day, indexes=()):
        self.asked.append((sorted(ids), sorted(indexes)))
        rows = [(lid, datetime(day.year, day.month, day.day, 9, k), 100.0 + 10 * k, 111.0, 99.0, 100.0 + 10 * k, 3000.0)
                for lid in [*ids, *indexes] for k in (0, 1)]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


@pytest.fixture
def live(paths):
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    src = Fake()
    events = []
    return Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True), src, events, paths


def mine(paths, *lids: str, status: str = "watching") -> None:
    """Yours: in your first watch group, 观察中 (盯盘 follows your stocks only since 10-12; a box's top is the buying side)."""
    ws, w = Workspace(paths), Watch(lambda m: paths)
    for lid in lids:
        ws.add_item(ws.groups()[0]["id"], lid)
        w.add(lid, status=status)
        w.set_status(lid, status)


def test_while_the_market_trades_its_followed_stocks_are_polled_and_their_rows_carry_the_day(live):
    lv, src, events, paths = live
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))                         # JP: 2 minutes into the morning
    jp = next(e for e in events if e["market"] == "JP")
    row = next(r for r in jp["rows"] if r["listing_id"] == "JP:7203")
    bars = Store(paths, "JP").read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date")
    prev = bars["close"][-1]
    avg5 = bars["volume"].tail(5).mean()
    assert (row["last"], row["rule"], row["watched"], row["live"]) == (110.0, None, True, True)
    assert row["chg"] == pytest.approx(110.0 / prev - 1)
    assert row["vol_ratio"] == pytest.approx((6000 / 2) / (avg5 / 330))
    assert jp["session"]["label"] == "前场" and jp["source"] == {"name": "fake", "delay": 0, "approx_volume": False, "approx_minutes": False}
    assert {r["listing_id"] for r in jp["indexes"]} == {"JP:IDX:N225", "JP:IDX:TOPIX"}
    kept = pl.read_parquet(paths.market_dir("JP") / "intraday" / f"date={DAY}.parquet")
    assert kept.filter(pl.col("listing_id") == "JP:7203").height == 2


def test_outside_the_session_nothing_is_polled_and_the_rows_are_the_close(live):
    lv, src, events, paths = live
    sunday = datetime(2026, 10, 4, 10, 0, tzinfo=TK)
    lv.tick(sunday)
    assert src.asked == []
    state = lv.state("JP", sunday)                                            # the session as of then, not the wall clock
    row = next(r for r in state["rows"] if r["listing_id"] == "JP:7203")
    close = Store(paths, "JP").read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date")["close"][-1]
    assert (row["last"], row["live"], row["vol_ratio"]) == (close, False, None) and state["session"]["label"] == "休市"


def test_a_watch_rows_box_top_is_its_break_line():
    detail = {"distances": [{"id": "break_up", "value": 95.0, "threshold": 100.0}]}
    assert box_top(json.dumps(detail)) == 100.0 and box_top(None) is None and box_top(json.dumps({"distances": []})) is None
    # a slanted small box's line moves: the scan's is the scan day's, the session watched is the next bar
    sloped = {"distances": [{"id": "break_up", "value": 95.0, "threshold": 100.0, "per_bar": 0.4}]}
    assert box_top(json.dumps(sloped)) == pytest.approx(100.4)


def quotes(events, market="JP"):
    """The page's quotes events of `market` (alerts go out as their own events)."""
    return [e for e in events if e["type"] == "quotes" and e["market"] == market]


def scanned(paths, market, rows, rule_id="tpl_shang_heng_shang"):
    """The latest daily scan of an enabled rule kept `rows` (listing_id, status, detail)."""
    Rules(paths).set_state(rule_id, enabled=True)
    folder = paths.profile_dir / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    run = f"r-{rule_id}"
    pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "status": pl.Utf8, "detail": pl.Utf8}, orient="row").write_parquet(folder / f"{run}.parquet")
    connect(paths.app_db, "app").execute("INSERT INTO rule_run (id, rule_id, version, rule_hash, market, snapshot_id, mode, day, "
                                         "start, end, pool, counts, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        run, rule_id, 1, "h", market, "s", "date", "2026-09-30", None, None, "null", "{}", "2026-09-30T16:00:00"))


def closes(paths, lid):
    """The listing's closes as the exchange printed them and as formulas read them (adjusted for splits)."""
    from wb.engine.l1.frame import build_frame
    store = Store(paths, lid.split(":")[0])
    return build_frame(store, lid.split(":")[0], paths.profile, {"close", "raw_close"}, [lid], snap=store.latest()).sort("date")


def test_a_box_top_from_the_scan_is_its_frozen_edge_in_todays_prices_and_the_distance_is_how_far_above_it(live):
    # the scan works in adjusted prices: after Toyota's 1:5 split its box top is 5x the price on the screen; its
    # watch row keeps the break line (the edge × (1 + tol), the template's tol 0.015): the edge is what the page shows
    lv, src, events, paths = live
    c = closes(paths, "JP:7203")
    scanned(paths, "JP", [("JP:7203", "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])
    row = next(r for r in lv.state("JP")["rows"] if r["listing_id"] == "JP:7203")
    assert row["top"] == pytest.approx(c["raw_close"][-1] * 1.02)
    assert row["to_top"] == pytest.approx(1 / 1.02 - 1)                       # 2% below the top: -1.96%
    assert (row["rule"], row["watched"], row["stage"]) == ("涨一段后短期窄幅横盘再突破（上横上）", True, "中箱")  # on a 潜伏 list and in a group; the stage by name


def test_the_close_on_a_split_day_changes_on_the_close_before_in_the_same_prices(paths):
    write(paths, 201)                                                         # Toyota's 1:5 split is the last bar
    Workspace(paths).add_item(Workspace(paths).groups()[0]["id"], "JP:7203")
    lv = Live(lambda m: paths, source_for=lambda p, m: Fake())
    row = next(r for r in lv.state("JP")["rows"] if r["listing_id"] == "JP:7203")
    c = closes(paths, "JP:7203")
    assert row["last"] == c["raw_close"][-1] and row["chg"] == pytest.approx(c["close"][-1] / c["close"][-2] - 1)
    assert row["prev"] == pytest.approx(c["raw_close"][-1] / (1 + row["chg"]))


def detail(value, top):
    return json.dumps({"deepest_stage": "box", "distances": [{"id": "break_up", "value": value, "threshold": top}]})


def test_a_source_that_takes_few_stocks_gets_the_one_you_look_at_then_yours_never_a_latent_one(live):
    # spec §4.2, yours only since 10-12: a 潜伏 stock you do not watch is not asked for (the 潜伏 list shows it by the
    # whole-market quote); the stock you look at comes first, then yours
    lv, src, events, paths = live                                             # JP:7203 is in a watch group
    far, near = closes(paths, "JP:6758")["close"][-1], closes(paths, "JP:130A")["close"][-1]
    scanned(paths, "JP", [("JP:6758", "watch", detail(far, far * 1.10)), ("JP:130A", "watch", detail(near, near * 1.01))])
    src.max_listings = 2
    lv.view("JP:6758", datetime(2026, 10, 2, 9, 1, tzinfo=TK))
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert src.asked[0][0] == ["JP:6758", "JP:7203"]
    assert {r["listing_id"] for r in quotes(events)[-1]["rows"]} == {"JP:7203"}   # 盯盘's rows: yours


def test_the_page_hears_when_the_morning_ends_and_keeps_its_last_prices_over_lunch(live):
    lv, src, events, paths = live
    for hm in ((11, 29), (11, 31), (11, 45)):
        lv.tick(datetime(2026, 10, 2, *hm, tzinfo=TK))
    jp = quotes(events)
    # a late source still gives the morning's last minutes for its delay (+5) after 11:30; then the page rests
    assert [e["session"]["label"] for e in jp] == ["前场", "午休"] and len([a for a in src.asked if "JP:IDX:N225" in a[1]]) == 2
    assert next(r for r in jp[-1]["rows"] if r["listing_id"] == "JP:7203")["last"] == 110.0


def test_once_the_days_close_is_published_the_rows_are_the_close_not_the_last_poll(live):
    lv, src, events, paths = live
    lv.tick(datetime(2026, 10, 2, 15, 29, tzinfo=TK))
    evening = datetime(2026, 10, 2, 17, 0, tzinfo=TK)
    row = lambda: next(r for r in lv.state("JP", evening)["rows"] if r["listing_id"] == "JP:7203")  # noqa: E731
    assert (row()["last"], row()["live"]) == (110.0, True)                  # the day's last minutes until the close is in
    bar = Store(paths, "JP").read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date").tail(1)
    close = round(bar["close"][0] * 1.01, 1)
    Store(paths, "JP").publish({"bars_1d": bar.with_columns(pl.lit(DAY).alias("date"), *[pl.lit(close).alias(k) for k in
                                                                                        ("open", "high", "low", "close")])}, as_of=DAY)
    assert (row()["last"], row()["live"]) == (close, False)


def test_demo_data_never_reaches_a_real_source_but_the_page_still_hears_the_session(paths):
    # the e2e tests and `wb devdata` run on demo data: a weekday morning must not send them to Yahoo
    write(paths, 320)
    src, events = Fake(), []
    Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert src.asked == [] and next(e for e in events if e["market"] == "JP")["session"]["label"] == "前场"


class Turnover(Fake):
    """Minutes with their turnover (TickFlow's): 100 then 110, but traded at 105 and 108 on average."""
    def minutes(self, ids, day, indexes=()):
        return super().minutes(ids, day, indexes).with_columns(
            pl.Series("amount", [3000 * 105.0, 3000 * 108.0] * (len(ids) + len(indexes))))


def test_minutes_with_their_turnover_give_the_exact_average_price_and_merge_with_a_day_kept_without_it(paths):
    write(paths, 320)
    Workspace(paths).add_item(Workspace(paths).groups()[0]["id"], "JP:7203")
    folder = paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True)
    pl.DataFrame([("JP:6758", datetime(2026, 10, 2, 9, 0), 50.0, 50.0, 50.0, 50.0, 100.0)], schema=MINUTES,
                 orient="row").write_parquet(folder / f"date={DAY}.parquet")       # kept by a source without turnover
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: Turnover(), publish=events.append, allow_demo=True)
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    row = next(r for r in next(e for e in events if e["market"] == "JP")["rows"] if r["listing_id"] == "JP:7203")
    assert row["vwap"] == pytest.approx(106.5)                                # (315,000 + 324,000) ÷ 6,000, not 105
    bar = lv.temp_day("JP:7203", datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert bar["amount"] == pytest.approx(639_000)
    kept = pl.read_parquet(folder / f"date={DAY}.parquet")
    assert kept.filter(pl.col("listing_id") == "JP:6758")["amount"].to_list() == [None]


def test_a_cn_rows_volume_ratio_is_on_the_markets_volume_clock(paths):
    # A shares: two minutes in, 5.75% of the day has usually traded (the opening auction in the first minute) — the
    # ratio is the day so far ÷ that share ÷ the 5-day average, not × 240 ÷ 2
    from wb.realtime.clock import DEFAULT
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "CN:000001")
    class Shanghai(Fake):
        def minutes(self, ids, day, indexes=()):
            return super().minutes(ids, day, indexes).with_columns(pl.col("at") + pl.duration(minutes=30))   # 9:30, 9:31
    src, events = Shanghai(), []
    Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",)).tick(
        datetime(2026, 10, 9, 9, 32, tzinfo=ZoneInfo("Asia/Shanghai")))
    row = next(r for r in next(e for e in events if e["market"] == "CN")["rows"] if r["listing_id"] == "CN:000001")
    avg5 = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == "CN:000001").sort("date")["volume"].tail(5).mean()
    assert row["vol_ratio"] == pytest.approx(6000 / DEFAULT["CN"][1] / avg5)


class Late(Fake):
    """A source 20 minutes behind that also sends flat bars over lunch (Yahoo does for Shenzhen)."""
    def minutes(self, ids, day, indexes=()):
        rows = [(lid, datetime(day.year, day.month, day.day, h, mi), 100.0, 100.0, 100.0, 100.0, v)
                for lid in [*ids, *indexes] for h, mi, v in ((9, 0, 3000.0), (9, 1, 3000.0), (12, 0, 90000.0))]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_the_volume_ratio_counts_the_minutes_the_data_covers_and_nothing_from_lunch(paths):
    # at 9:30 a source 20 minutes behind has given two minutes: their volume is two minutes' worth, not thirty's
    write(paths, 320)
    Workspace(paths).add_item(Workspace(paths).groups()[0]["id"], "JP:7203")
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Late(), publish=events.append, allow_demo=True).tick(
        datetime(2026, 10, 2, 9, 30, tzinfo=TK))
    row = next(r for r in next(e for e in events if e["market"] == "JP")["rows"] if r["listing_id"] == "JP:7203")
    avg5 = Store(paths, "JP").read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date")["volume"].tail(5).mean()
    assert row["vol_ratio"] == pytest.approx((6000 / 2) / (avg5 / 330))
    kept = pl.read_parquet(paths.market_dir("JP") / "intraday" / f"date={DAY}.parquet")
    assert kept["at"].dt.hour().to_list().count(12) == 0



class Flaky(Fake):
    """Both stocks, then only Toyota's next minute, then nothing (a rate limit): what a busy Yahoo does."""
    def __init__(self):
        super().__init__()
        self.calls = 0

    def minutes(self, ids, day, indexes=()):
        self.calls += 1
        at = lambda k: datetime(day.year, day.month, day.day, 9, k)  # noqa: E731
        rows = {1: [(lid, at(0), 100.0, 100.0, 100.0, 100.0, 1000.0) for lid in ids],
                2: [("JP:7203", at(1), 101.0, 101.0, 101.0, 101.0, 1000.0)]}.get(self.calls, [])
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_a_poll_that_brings_less_keeps_the_days_minutes_and_one_that_brings_nothing_changes_nothing(paths):
    write(paths, 320)
    ws = Workspace(paths)
    for lid in ("JP:7203", "JP:6758"):
        ws.add_item(ws.groups()[0]["id"], lid)
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: Flaky(), publish=events.append, allow_demo=True, markets=("JP",))
    src = Flaky()
    lv.source_for = lambda p, m: src
    for mi in (1, 2, 3):
        lv.tick(datetime(2026, 10, 2, 9, mi * 2, tzinfo=TK))
    kept = pl.read_parquet(paths.market_dir("JP") / "intraday" / f"date={DAY}.parquet")
    assert kept.sort("listing_id", "at").select("listing_id", "close").rows() == [("JP:6758", 100.0), ("JP:7203", 100.0), ("JP:7203", 101.0)]
    assert len(quotes(events)) == 2                                          # the empty poll published nothing
    rows = {r["listing_id"]: r for r in lv.state("JP", datetime(2026, 10, 2, 9, 7, tzinfo=TK))["rows"]}
    assert (rows["JP:7203"]["last"], rows["JP:6758"]["last"], rows["JP:6758"]["live"]) == (101.0, 100.0, True)


def test_after_a_restart_past_the_close_the_rows_are_the_days_last_minutes_until_the_close_is_published(live):
    lv, src, events, paths = live
    lv.tick(datetime(2026, 10, 2, 15, 29, tzinfo=TK))
    again = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True)          # the app restarted
    row = next(r for r in again.state("JP", datetime(2026, 10, 2, 17, 0, tzinfo=TK))["rows"] if r["listing_id"] == "JP:7203")
    assert (row["last"], row["live"]) == (110.0, True)



RISE = """
id: rise
name: 放量大涨
markets: [JP]
stages:
  - {id: today, kind: event, when: "C > REF(C, 1) * 1.05 AND V > MA(V, 5)"}
signal_at: today.at
"""
DROP = """
id: drop
name: 大跌
markets: [JP]
stages:
  - {id: today, kind: event, when: "C < REF(C, 1) * 0.9"}
signal_at: today.at
"""


class Jump(Fake):
    """Each stock asked for: two minutes (from `minute` past 9) at `move` × its last close with a big volume."""
    def __init__(self, paths, move=1.08):
        super().__init__()
        self.paths, self.move, self.minute = paths, move, 0

    def minutes(self, ids, day, indexes=()):
        self.asked.append((sorted(ids), sorted(indexes)))
        rows = []
        for lid in ids:
            p = closes(self.paths, lid)["raw_close"][-1] * self.move
            rows += [(lid, datetime(day.year, day.month, day.day, 9, self.minute + k), p, p, p, p, 1e8) for k in (0, 1)]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def alerts_of(events):
    return [a for e in events if e["type"] == "alert" for a in e["alerts"]]


def test_a_latent_stock_meeting_its_rule_on_todays_bar_alerts_once_unconfirmed_but_not_in_the_first_minutes(paths):
    write(paths, 320)
    Rules(paths).save(RISE)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="rise")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: Jump(paths), publish=events.append, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))            # two minutes in: a rule that reads volume waits (15 by default)
    assert alerts_of(events) == []
    Workspace(paths).set_setting("live.quiet_minutes", 0)
    for mi in (4, 6):                                            # once a day
        lv.tick(datetime(2026, 10, 2, 9, mi, tzinfo=TK))
    got = [a for a in alerts_of(events) if a["kind"] != "feed"]   # (this source's minutes stop at 9:01: 行情中断 by 9:06)
    assert [(a["listing_id"], a["kind"], a["key"], a["status"]) for a in got] == [("JP:7203", "rule", "rise", "pending")]
    assert got[0]["detail"]["lines"][0].endswith("信号") and "成交节奏折算" in got[0]["text"]
    row = next(r for r in lv.state("JP", datetime(2026, 10, 2, 9, 7, tzinfo=TK))["rows"] if r["listing_id"] == "JP:7203")
    assert (row["alert"]["kind"], row["alert"]["status"]) == ("rule", "pending")
    assert f"live|{got[0]['id']}" not in {r["idem"] for r in Outbox(paths).recent()}   # in the app only (design 2026-10-10 §4)


def test_a_latent_box_top_near_the_open_is_the_auctions_alert_and_a_muted_stock_stays_quiet(paths):
    write(paths, 320)
    tops = {lid: closes(paths, lid)["close"][-1] for lid in ("JP:7203", "JP:6758")}
    scanned(paths, "JP", [(lid, "watch", detail(c, c * 1.02 * 1.015)) for lid, c in tops.items()])   # edges 2% over the close
    mine(paths, *tops)   # yours, 观察中: a box's top is the buying side (10-12)
    Alerts(paths).mute(DAY, "JP:6758")
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=1.015), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    got = alerts_of(events)                                   # opened 1.5% up: 0.5% under the top (2% up)
    assert [(a["listing_id"], a["kind"]) for a in got] == [("JP:7203", "auction")] and got[0]["text"].startswith("高开到箱顶（") and "附近" in got[0]["text"]


def test_a_line_you_drew_with_its_alert_on_speaks_when_todays_price_reaches_it(paths):
    write(paths, 320)
    c = closes(paths, "JP:6758")
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:6758")
    did = ws.save_drawing("JP:6758", "horizontalStraightLine", [{"date": str(c["date"][-1]), "price": c["raw_close"][-1] * 1.02}], alert=True)
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=1.03), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    got = alerts_of(events)                                   # opened 3% up, through the line 2% up: the auction says it
    assert [(a["listing_id"], a["kind"], a["key"]) for a in got] == [("JP:6758", "auction", did)] and "你画的线" in got[0]["text"]


def test_a_stock_beyond_its_days_limits_is_not_judged_and_says_so(paths):
    # half the price at the open: a split the factor has not caught (spec §7.2 除权检测), not a 50% fall
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="drop")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    Workspace(paths).set_setting("live.quiet_minutes", 0)
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=0.5), publish=events.append, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    lv.tick(datetime(2026, 10, 2, 9, 4, tzinfo=TK))
    got = alerts_of(events)                                   # said once (design 04: 疑似除权), never judged
    assert [(a["listing_id"], a["kind"], a["status"]) for a in got] == [("JP:7203", "ex", "none")]
    assert got[0]["text"] == "现价比昨收 -50.0%，超出当天的涨跌停幅度：疑似除权"
    assert next(r for r in quotes(events)[-1]["rows"] if r["listing_id"] == "JP:7203")["ex_rights"] is True



def test_a_rule_that_reads_only_prices_alerts_in_the_first_minutes(paths):
    # only volume-type alerts wait out the first minutes after the open (spec §7.2): a price rule does not
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="drop")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=0.88), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert [(a["listing_id"], a["kind"], a["key"]) for a in alerts_of(events)] == [("JP:7203", "rule", "drop")]


def test_a_rule_that_cannot_run_here_does_not_stop_the_quotes_or_the_other_rules(paths):
    # a rule edited to leave JP keeps its old JP scan: judging it raises, every poll
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="drop")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    Rules(paths).save(DROP.replace("markets: [JP]", "markets: [CN]"))
    Rules(paths).save(RISE.replace("id: rise", "id: rise2").replace("C > REF(C, 1) * 1.05 AND V > MA(V, 5)", "C < REF(C, 1) * 0.9"))
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="rise2")
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=0.88), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert quotes(events) and [a["key"] for a in alerts_of(events)] == ["rise2"]


def test_a_line_drawn_during_the_session_starts_from_the_price_then(paths, monkeypatch):
    # drawn under the price while the market trades: not a crossing, and a later real touch still speaks
    import wb.workspace.store as store
    monkeypatch.setattr(store, "_now", lambda: datetime(2026, 10, 2, 9, 3, tzinfo=TK).astimezone().replace(tzinfo=None).isoformat(timespec="seconds"))
    write(paths, 320)
    c = closes(paths, "JP:6758")
    prev = c["raw_close"][-1]
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:6758")
    events = []
    src = Jump(paths, move=1.03)
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    ws.save_drawing("JP:6758", "horizontalStraightLine", [{"date": str(c["date"][-1]), "price": prev * 1.01}], alert=True)
    src.move, src.minute = 1.03, 2                           # the price stays over the new line
    lv.tick(datetime(2026, 10, 2, 9, 4, tzinfo=TK))
    assert alerts_of(events) == []
    src.move, src.minute = 1.01, 4                           # and comes down to it
    lv.tick(datetime(2026, 10, 2, 9, 6, tzinfo=TK))
    assert [(a["kind"], a["text"][:4], a["detail"]["side"]) for a in alerts_of(events)] == [("touch", "已到你画", "down")]


def test_each_day_starts_its_lines_afresh(paths):
    # a line that spoke at 14:59 yesterday is not still resting at today's open
    write(paths, 320)
    c = closes(paths, "JP:6758")
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:6758")
    ws.save_drawing("JP:6758", "horizontalStraightLine", [{"date": str(c["date"][-1]), "price": c["raw_close"][-1] * 1.03}], alert=True)
    events = []
    src = Jump(paths, move=1.03)
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("JP",))
    src.minute = 0
    lv.tick(datetime(2026, 10, 1, 9, 2, tzinfo=TK))           # Thursday: touched at the open
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))           # Friday: touched again at the open
    assert [a["day"] for a in alerts_of(events)] == ["2026-10-01", "2026-10-02"]



def test_after_a_restart_a_line_drawn_today_ignores_the_minutes_before_it_was_drawn(paths, monkeypatch):
    # the replay of the day's minutes starts at the line's drawing, not at the open it was not there for
    import wb.workspace.store as store
    write(paths, 320)
    c = closes(paths, "JP:6758")
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:6758")
    monkeypatch.setattr(store, "_now", lambda: datetime(2026, 10, 2, 9, 1, tzinfo=TK).astimezone().replace(tzinfo=None).isoformat(timespec="seconds"))
    ws.save_drawing("JP:6758", "horizontalStraightLine", [{"date": str(c["date"][-1]), "price": c["raw_close"][-1] * 1.01}], alert=True)
    events = []                                                 # the open gapped 3% up, through where the line was later drawn
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=1.03), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert alerts_of(events) == []


# ---- a snapshot source (the prod profile's rt_k and Tachibana): the day so far, not minute bars ----
from wb.realtime.providers.tushare_rt import SNAPSHOT  # noqa: E402


class Snap(Fake):
    """The day so far at each poll: `quotes` [(at, last, cumulative volume)] one per call; the day's high is 6% up."""
    kind, max_listings = "snapshot", None

    def __init__(self, paths, quotes):
        super().__init__()
        self.paths, self.quotes, self.error = paths, list(quotes), None

    def snapshot(self, ids, day):
        if self.error:
            raise self.error
        at, move, cum = self.quotes.pop(0)
        rows = []
        for lid in ids:
            prev = closes(self.paths, lid)["raw_close"][-1]
            rows.append((lid, datetime(day.year, day.month, day.day, *at), prev, prev * 1.06, prev * 0.99, prev * move, cum,
                         cum * prev, prev))
        return pl.DataFrame(rows, schema=SNAPSHOT, orient="row")


def test_a_snapshot_source_gives_approximate_minutes_whose_volume_adds_up_to_the_days(paths):
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    src = Snap(paths, [((9, 0, 40), 1.01, 1000.0), ((9, 0, 55), 1.01, 1500.0), ((9, 1, 59), 1.02, 4000.0), ((9, 2, 30), 1.02, 4500.0)])
    lv = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",))
    for mi in (1, 2, 3):
        lv._polled.clear()
        lv.tick(datetime(2026, 10, 2, 9, mi, tzinfo=TK))
    kept = pl.read_parquet(paths.market_dir("JP") / "intraday" / f"date={DAY}.parquet").sort("at")
    assert kept.select(pl.col("at").dt.minute(), "volume").rows() == [(0, 1500.0), (1, 2500.0)]   # two polls in 9:00: one minute
    again = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",))   # a restart
    again.tick(datetime(2026, 10, 2, 9, 4, tzinfo=TK))
    kept = pl.read_parquet(paths.market_dir("JP") / "intraday" / f"date={DAY}.parquet").sort("at")
    assert kept["volume"].to_list() == [1500.0, 2500.0, 500.0] and kept["volume"].sum() == 4500.0


def test_a_snapshot_sources_temporary_bar_is_the_day_it_reports(paths):
    # the day's high came between two polls: the rule reads it from the snapshot, not from the prices polled
    write(paths, 320)
    Rules(paths).save(DROP.replace("id: drop", "id: high").replace("C < REF(C, 1) * 0.9", "H > REF(C, 1) * 1.05"))
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="high")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Snap(paths, [((9, 1, 30), 1.01, 1000.0)]), publish=events.append,
         allow_demo=True, markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert [(a["listing_id"], a["key"]) for a in alerts_of(events)] == [("JP:7203", "high")]


def test_a_snapshot_sources_forming_k_line_bar_is_the_day_it_reports_even_after_a_restart(paths):
    # polling began at 9:01: the minutes start there, the day's open, high and low are the snapshot's (§8.5)
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    src = Snap(paths, [((9, 1, 30), 1.01, 1000.0)])
    Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    prev = closes(paths, "JP:7203")["raw_close"][-1]
    again = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",))   # a restart, no poll since
    bar = again.temp_day("JP:7203", datetime(2026, 10, 2, 9, 3, tzinfo=TK))
    assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == pytest.approx(
        (prev, prev * 1.06, prev * 0.99, prev * 1.01, 1000.0))


def test_when_the_source_fails_the_page_says_so_and_shows_no_stale_price(paths):
    # spec §15.1: 断线期间界面明确显示"行情中断"，不显示过时价格; e.g. no rt_k permission, Tachibana not logged in
    from wb.realtime.providers.tachibana import NotLoggedIn
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    src = Snap(paths, [((9, 0, 40), 1.01, 1000.0), ((9, 4, 0), 1.02, 2000.0)])
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 9, 1, tzinfo=TK))
    src.error = NotLoggedIn("立花证券今天还没登录")
    lv.tick(datetime(2026, 10, 2, 9, 3, tzinfo=TK))
    st = lv.state("JP", datetime(2026, 10, 2, 9, 3, 30, tzinfo=TK))
    row = next(r for r in st["rows"] if r["listing_id"] == "JP:7203")
    assert st["error"] == "立花证券今天还没登录" and quotes(events)[-1]["error"] == st["error"] and row["live"] is False
    src.error = None
    lv.tick(datetime(2026, 10, 2, 9, 5, tzinfo=TK))
    assert lv.state("JP", datetime(2026, 10, 2, 9, 5, 10, tzinfo=TK))["error"] is None



def test_each_profile_routes_each_market_to_its_source(tmp_path, monkeypatch):
    # spec §4.8: the test profile's delayed Yahoo; prod: Tushare rt_k for A shares (TickFlow once its key is set: see
    # test_realtime_tickflow), Tachibana for Tokyo (no network here)
    from wb.config import get_paths
    from wb.jobs import live
    from wb.jobs.live import source_for
    monkeypatch.setattr(live, "secret_source", lambda name: None)
    test, prod = get_paths("test", tmp_path), get_paths("prod", tmp_path)
    assert [source_for(test, m).name for m in ("CN", "JP")] == ["yfinance_rt", "yfinance_rt"]
    assert [(source_for(prod, m).name, source_for(prod, m).kind) for m in ("CN", "JP")] == [("tushare_rt", "snapshot"), ("tachibana", "snapshot")]



def test_a_failed_poll_says_行情中断_only_while_prices_are_due(paths):
    # after the polling window closes, the day's last minutes and 已收盘 are right: no stale red pill all evening
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    src = Snap(paths, [((15, 25, 0), 1.01, 1000.0)])
    lv = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 15, 26, tzinfo=TK))
    src.error = RuntimeError("断线")
    lv._polled.clear()
    lv.tick(datetime(2026, 10, 2, 15, 33, tzinfo=TK))                 # the late window: said
    assert lv.state("JP", datetime(2026, 10, 2, 15, 33, 30, tzinfo=TK))["error"] == "断线"
    lv.tick(datetime(2026, 10, 2, 20, 0, tzinfo=TK))                  # the evening: not
    st = lv.state("JP", datetime(2026, 10, 2, 20, 0, 30, tzinfo=TK))
    assert st["error"] is None and st["session"]["label"] == "已收盘"


def test_a_poll_with_no_trade_yet_still_clears_an_error(paths):
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")

    class Quiet(Snap):
        def snapshot(self, ids, day):
            if self.error:
                raise self.error
            return pl.DataFrame(schema=SNAPSHOT)                         # before the day's first trade
    src = Quiet(paths, [])
    lv = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("JP",))
    src.error = RuntimeError("断线")
    lv.tick(datetime(2026, 10, 2, 9, 0, tzinfo=TK))
    src.error, lv._polled = None, {}
    lv.tick(datetime(2026, 10, 2, 9, 1, tzinfo=TK))
    assert lv.state("JP", datetime(2026, 10, 2, 9, 1, 10, tzinfo=TK))["error"] is None


def test_todays_bar_still_forming_comes_from_the_kept_minutes_until_the_close_is_in(live):
    # spec §8.5: the K-line's last bar while a day is open; its volume as it is (a process value, never scaled up)
    lv, src, events, paths = live
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    bar = lv.temp_day("JP:7203", datetime(2026, 10, 2, 9, 3, tzinfo=TK))
    assert (bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (DAY, 100.0, 111.0, 99.0, 110.0, 6000.0)
    assert lv.temp_day("JP:6758", datetime(2026, 10, 2, 9, 3, tzinfo=TK)) is None                   # not followed: no minutes
    last = Store(paths, "JP").read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date").tail(1)
    Store(paths, "JP").publish({"bars_1d": last.with_columns(pl.lit(DAY).alias("date"))}, as_of=DAY)
    assert lv.temp_day("JP:7203", datetime(2026, 10, 2, 17, 0, tzinfo=TK)) is None                  # the close is in


def test_a_minute_file_a_reader_still_holds_is_replaced_once_it_lets_go(tmp_path, monkeypatch):
    # Windows: replacing a file another thread has open (or mapped) fails for a moment; a poll must not fail on it
    import wb.files as files
    from wb.jobs import live as live_mod
    real, tries = files.os.replace, []

    def held(a, b):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(a, b)
    monkeypatch.setattr(files.os, "replace", held)
    monkeypatch.setattr(files, "WINDOWS", True)
    path = tmp_path / "date=2026-10-02.parquet"
    live_mod._write(pl.DataFrame({"x": [1]}), path)
    assert len(tries) == 3 and live_mod._read(path)["x"].to_list() == [1]


def test_a_rows_average_price_and_how_far_the_price_stands_above_it(live, paths):
    # the day's average (VWAP, the 分时图's yellow line) and the price's distance from it, in points of the
    # previous close as the 分时图's axis reads it; a snapshot source's is exact (its amount ÷ its volume)
    lv, src, events, _ = live
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    row = next(r for e in events if e["market"] == "JP" for r in e["rows"] if r["listing_id"] == "JP:7203")
    assert row["vwap"] == pytest.approx(105.0) and row["vwap_gap"] == pytest.approx((110.0 - 105.0) / row["prev"])
    snap = Snap(paths, [((9, 1, 30), 1.01, 1000.0)])
    got = []
    Live(lambda m: paths, source_for=lambda p, m: snap, publish=got.append, allow_demo=True, markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    row = next(r for r in got[-1]["rows"] if r["listing_id"] == "JP:7203")
    assert row["vwap"] == pytest.approx(row["prev"]) and row["vwap_gap"] == pytest.approx(0.01)
    closed = lv.state("JP", datetime(2026, 10, 4, 10, 0, tzinfo=TK))           # no day's prices: no average
    assert all(r["vwap"] is None and r["vwap_gap"] is None for r in closed["rows"])


def test_the_last_kept_days_of_minutes_stay_and_older_go(live, paths):
    # KEEP_DAYS (60, 10-13): the minute charts' warm-up and the multi-day 分时 (通达信: up to 20 days side by side)
    from datetime import timedelta

    from wb.jobs.live import KEEP_DAYS
    lv, src, events, _ = live
    folder = paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    days = [date(2026, 10, 1) - timedelta(days=k) for k in range(KEEP_DAYS + 5)]
    for d in days:
        pl.DataFrame(schema=MINUTES).write_parquet(folder / f"date={d}.parquet")
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    kept = sorted(f.name for f in folder.glob("date=*.parquet"))
    assert len(kept) == KEEP_DAYS and kept[-1] == "date=2026-10-02.parquet" and kept[0] == f"date={sorted(days)[-(KEEP_DAYS - 1)]}.parquet"


def test_a_source_with_history_fills_the_days_before_for_the_multi_day_view(live, paths):
    # the multi-day 分时 (test profile): yfinance keeps about a week of 1-minute bars — the previous trading days a followed stock
    # has no minutes for are fetched once each, one day a minute and only after the minute's prices (a first launch
    # never holds up the quotes and alerts); a source without history (a snapshot source) never is
    lv, src, events, _ = live
    src.history_days = 5
    real, days = src.minutes, []

    def minutes(ids, day, indexes=()):
        days.append(day)
        return real(ids, day, indexes)
    src.minutes = minutes
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    store = Store(paths, "JP")
    before = store.read("calendar").filter(pl.col("is_open") & (pl.col("date") < DAY))["date"].sort().to_list()[-5:]
    assert days == [DAY, before[-1]]              # today's prices first, then the day before it: the nearest days first
    for mi in range(3, 7):
        lv.tick(datetime(2026, 10, 2, 9, mi, tzinfo=TK))
    folder = paths.market_dir("JP") / "intraday"
    for d in before:
        assert pl.read_parquet(folder / f"date={d}.parquet").filter(pl.col("listing_id") == "JP:7203").height == 2
    n = len(days)
    lv.tick(datetime(2026, 10, 2, 9, 8, tzinfo=TK))
    assert days[n:] == [DAY]                                              # all there: this minute's poll only
    plain = Fake()
    Live(lambda m: paths, source_for=lambda p, m: plain, allow_demo=True, markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert len(plain.asked) == 1


def test_a_snapshot_whose_amount_is_in_another_unit_gives_the_minutes_average(live, paths):
    # amount ÷ volume outside the day's range (thousands of yen, say — the daily bars' vwap_range check): the average
    # comes from the minutes, never a price a thousand times off
    class Thousands(Snap):
        def snapshot(self, ids, day):
            return super().snapshot(ids, day).with_columns(pl.col("amount") / 1000)
    src, got = Thousands(paths, [((9, 1, 30), 1.01, 1000.0)]), []
    Live(lambda m: paths, source_for=lambda p, m: src, publish=got.append, allow_demo=True, markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    row = next(r for r in got[-1]["rows"] if r["listing_id"] == "JP:7203")
    assert row["vwap"] == pytest.approx(row["last"]) and row["vwap_gap"] == pytest.approx(0.0)


def test_a_history_fetch_that_fails_is_not_asked_again_and_the_days_polls_go_on(live, paths):
    # Yahoo down (or nothing for those days): each day before is asked once, one a minute at most; today is polled as ever
    lv, src, events, _ = live
    src.history_days = 5
    real, calls = src.minutes, []

    def minutes(ids, day, indexes=()):
        if day < DAY:
            calls.append(day)
            raise ConnectionError("Yahoo down")
        return real(ids, day, indexes)
    src.minutes = minutes
    lv.tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    jp = [e for e in events if e["market"] == "JP" and e["type"] == "quotes"]
    assert jp and next(r for r in jp[-1]["rows"] if r["listing_id"] == "JP:7203")["live"]          # today's poll went on
    lv.tick(datetime(2026, 10, 2, 9, 4, tzinfo=TK))
    assert len(calls) == 2 and len(set(calls)) == 2


def test_an_intraday_source_not_to_be_kept_here_is_not_polled_and_the_page_says_why(paths):
    # spec §4.10 本地存储 off for the intraday source: its minutes are never fetched (they would be kept)
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "JP:7203")
    ws.set_setting("datause.yfinance_rt.store", False)

    class Yahoo(Fake):
        name = "yfinance_rt"
    src, events = Yahoo(), []
    Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("JP",)).tick(
        datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert src.asked == [] and "本地存储" in (events[-1]["error"] or "")


class Delayed(Jump):
    delay = 20


def test_a_delayed_source_alerts_on_the_page_but_sends_nothing(paths):
    write(paths, 320)
    Rules(paths).save(RISE)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="rise")
    mine(paths, "JP:7203")   # yours: 盯盘 follows your stocks only (10-12)
    Workspace(paths).set_setting("live.quiet_minutes", 0)
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: Delayed(paths), publish=events.append, allow_demo=True, markets=("JP",))
    lv.tick(datetime(2026, 10, 2, 9, 4, tzinfo=TK))
    assert alerts_of(events) and [r for r in Outbox(paths).recent() if r["kind"] == "live"] == []


# F1 (review F, 10-13): a stock its rule already signalled at the last scan is no news on today's bar — 盯盘 says no
# 「盘中满足」 rule alert for it (on the real 10-12 replay: 002566, 002791, 002833 got one every morning at 09:31, as
# long13's signals of 09-30, 10-08, 10-09 and 10-12).
def test_a_stock_its_rule_signalled_at_the_last_scan_is_not_said_again_on_todays_bar(paths):
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "signal", None)], rule_id="drop")   # the last close met the rule already
    mine(paths, "JP:7203")
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=0.88), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert [a for a in alerts_of(events) if a["kind"] == "rule"] == []


def test_a_latent_stock_meeting_its_rule_still_alerts(paths):
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="drop")
    mine(paths, "JP:7203")
    events = []
    Live(lambda m: paths, source_for=lambda p, m: Jump(paths, move=0.88), publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 9, 2, tzinfo=TK))
    assert [(a["listing_id"], a["kind"]) for a in alerts_of(events)] == [("JP:7203", "rule")]


class LateDrop(Jump):
    """The day's last two minutes (15:28, 15:29) at `move` × each stock's last close, with a big volume."""
    def minutes(self, ids, day, indexes=()):
        self.asked.append((sorted(ids), sorted(indexes)))
        rows = []
        for lid in ids:
            p = closes(self.paths, lid)["raw_close"][-1] * self.move
            rows += [(lid, datetime(day.year, day.month, day.day, 15, 28 + k), p, p, p, p, 1e8) for k in (0, 1)]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_after_the_close_a_rule_met_on_todays_bar_is_not_said(paths):
    """You chose (10-13, review F): after the close a rule met on the day's bar is the evening scan's to decide; said at
    15:31 it is no use to anyone."""
    write(paths, 320)
    Rules(paths).save(DROP)
    scanned(paths, "JP", [("JP:7203", "watch", None)], rule_id="drop")
    mine(paths, "JP:7203")
    events, src = [], LateDrop(paths, move=0.88)
    Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True,
         markets=("JP",)).tick(datetime(2026, 10, 2, 15, 31, tzinfo=TK))
    assert src.asked and [a for a in alerts_of(events) if a["kind"] == "rule"] == []
