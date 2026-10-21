"""盯盘提醒 (design 2026-10-10): before a line is reached, not after it is confirmed. Each followed stock's lines — a box's
top for the buying side (a 潜伏 rule's, or a watched stock's own box), 持有中's fail lines for the selling
side, the lines you drew — and their alerts: 竞价 (a gap to the line at the open), 逼近 (near it at a new high above
the day's average), 触线, 持有中's reminder ten minutes before the close; the market's fall; the feed stopping and
coming back. Feishu hears the buying side of a stock 观察中 or 待研究, the selling side of one 持有中, the lines you drew
on a stock you watch, the market and the feed; the rest is said in the app."""
import threading
from datetime import date, datetime, time, timedelta
from time import monotonic, sleep
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from tests.test_live_service import closes, detail, scanned
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l2 import box, primitives
from wb.jobs.live import Live, tags
from wb.realtime.clock import DEFAULT
from wb.notify.outbox import Outbox
from wb.realtime.providers.tickflow_rt import QUOTES
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.workspace.rules import Rules
from wb.workspace.store import Workspace
from wb.workspace.watch import Watch

SH = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 10, 2)
MAO, PING, IDX = "CN:600519", "CN:000001", "CN:IDX:000001"
MAO_TOP = 1722.2761   # 600519's own 中箱 (devdata, 321 days): its top in today's prices, 0.82% over the last close


class Script:
    """The day's minutes from 9:30 as multiples of each listing's last close — (high, low, close), 1,000 shares a
    minute, opening at the close before (the first minute at `opens`, else its close); `upto` minutes have traded, or
    the source fails with `error`. A listing not scripted trades flat."""
    name, delay, poll_seconds, max_listings, approx_volume, kind = "script", 0, 30, None, False, "minutes"

    def __init__(self, paths, days: dict[str, list[tuple]], opens: dict[str, float] | None = None):
        self.paths, self.days, self.opens, self.upto, self.error = paths, days, opens or {}, 1, None

    def prev(self, lid: str) -> float:
        if ":IDX:" in lid:
            d = Store(self.paths, "CN").read("index_bars_1d").filter(pl.col("index_id") == lid).sort("date")
            return d["close"][-1] if d.height else 1000.0
        return closes(self.paths, lid)["raw_close"][-1]

    def minutes(self, ids, day, indexes=()):
        if self.error:
            raise self.error
        rows = []
        for lid in [*ids, *indexes]:
            p, seq = self.prev(lid), self.days.get(lid, [(1.0, 1.0, 1.0)] * 240)
            for k, (h, lo, c) in enumerate(seq[:self.upto]):
                at = datetime.combine(day, time(9, 30) if k < 120 else time(13, 0)) + timedelta(minutes=k % 120 if k < 120 else k - 120)
                o = self.opens.get(lid, c) if k == 0 else seq[k - 1][2]
                rows.append((lid, at, p * o, p * h, p * lo, p * c, 1000.0))
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def follow(paths, lid: str, status: str = "watching") -> None:
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], lid)
    Watch(lambda m: paths).add(lid, status=status)
    Watch(lambda m: paths).set_status(lid, status)


def run(paths, src, times: list[tuple]) -> list[dict]:
    """Tick at each (hour, minute, second) of DAY with `src` having traded through that minute; -> the alerts said."""
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    for t in times:
        now = datetime(2026, 10, 2, *t, tzinfo=SH)
        src.upto = (now.hour * 60 + now.minute - (9 * 60 + 30)) + 1 if now.hour < 12 else 120 + now.hour * 60 + now.minute - 13 * 60 + 1
        lv.tick(now)
    return [a for e in events if e["type"] == "alert" for a in e["alerts"]]


def sent(paths) -> list[str]:
    return [r["payload"]["line"] for r in reversed(Outbox(paths).recent()) if r["kind"] == "live"]


def up_to(r: float, *steps: float) -> list[tuple]:
    """Minutes closing at each of `steps` × r, each one a new high (its high its close, its low 0.2% under)."""
    return [(r * x, r * x * 0.998, r * x) for x in steps]


@pytest.fixture
def paths_(paths):
    write(paths, 321)   # with 320 days 600519's own 中箱 top is 0.06% under its last close: passed, not watched
    return paths


def test_a_watched_stock_off_the_latent_lists_is_watched_against_its_own_box_before_and_at_its_top(paths_):
    follow(paths_, MAO)
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]                  # the top over the last close
    src = Script(paths_, {MAO: up_to(r, 0.97, 0.98, 0.995, 1.001)})
    got = run(paths_, src, [(9, 31, 5), (9, 32, 5), (9, 33, 5), (9, 34, 5)])
    assert [(a["kind"], a["at"][11:16]) for a in got] == [("near", "09:32"), ("touch", "09:33")]
    near, touch = got
    assert near["text"] == "可能突破，注意看：离箱顶（中箱） 1,722.28 还差 0.5%" and near["detail"]["notify"] is True
    # the last 3 minutes' volume against what those minutes of the day usually trade (the market's volume clock), not
    # a flat share of the day: 3,000 shares in the open's three minutes, which carry DEFAULT[2] of a usual day
    avg5 = next(r for r in Live(lambda m: paths_, allow_demo=True, markets=("CN",)).state("CN")["rows"] if r["listing_id"] == MAO)["avg5"]
    assert near["detail"]["vol3"] == pytest.approx(3000 / (avg5 * DEFAULT["CN"][2]))
    assert touch["text"] == "已到箱顶（中箱） 1,722.28" and touch["detail"]["history"].startswith("碰线的日子")
    lines = sent(paths_)
    assert len(lines) == 2 and lines[0].startswith("09:32 · ") and "可能突破" in lines[0] and "均价线上" in lines[0]


def test_a_top_the_close_is_already_over_is_passed_and_its_box_not_watched(paths):
    # your 10-10 screenshot: the price at 12.46 over a 12.33 top, under its break line 12.52 — 盯盘 watches the top
    # itself (where the alert is set, ledger 2026-10-07): once a close is over it, there is nothing ahead to warn of
    write(paths, 320)   # 600519's own 中箱 top: 0.06% under its last close
    follow(paths, MAO)
    lv = Live(lambda m: paths, source_for=lambda p, m: Script(paths, {}), allow_demo=True, markets=("CN",))
    assert [x for x in lv.lines("CN", datetime(2026, 10, 2, 9, 35, tzinfo=SH)) if x["listing_id"] == MAO] == []


def test_a_latent_stock_you_do_not_watch_is_neither_asked_for_nor_judged(paths_):
    """盯盘 watches your own stocks (10-12): a 潜伏 stock not in your watch groups is not polled — so never shown at a
    price minutes old — and nothing is said as it runs through its top; the 潜伏 list shows it by the whole-market quote."""
    c = closes(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])   # its top 2% up
    src, asked = Script(paths_, {PING: up_to(1.02, 0.97, 1.001)}), []
    minutes = src.minutes
    src.minutes = lambda ids, day, indexes=(): asked.append(list(ids)) or minutes(ids, day, indexes)
    assert run(paths_, src, [(9, 31, 5), (9, 32, 5)]) == [] and asked and all(PING not in ids for ids in asked)


def test_the_latent_list_is_by_the_whole_market_quote(paths_):
    """Each 潜伏 stock you do not watch: how far it is from its box top at the day's whole-market quote (every A share,
    a minute old at most; the K-line's forming bar reads the same), the quote's time with it."""
    from wb.realtime.providers.tushare_rt import SNAPSHOT
    c = closes(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])   # its top 2% up
    raw = c["raw_close"][-1]
    folder = paths_.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(PING, datetime(2026, 10, 2, 10, 0, 3), raw, raw * 1.012, raw, raw * 1.01, 1e6, 1e7, raw)], schema=SNAPSHOT,
                 orient="row").write_parquet(folder / f"snapshot={DAY}.parquet")
    follow(paths_, MAO)                                                    # yours: on the 自选 rows, not this list
    lv = Live(lambda m: paths_, source_for=lambda p, m: Script(paths_, {}), allow_demo=True, markets=("CN",))
    rows = {r["listing_id"]: r for r in lv.latent("CN", datetime(2026, 10, 2, 10, 0, 30, tzinfo=SH))["rows"]}
    assert MAO not in rows
    r = rows[PING]
    assert r["last"] == pytest.approx(raw * 1.01) and r["chg"] == pytest.approx(0.01) and r["at"] == "2026-10-02T10:00:03"
    assert r["to_top"] == pytest.approx(1.01 / 1.02 - 1, abs=1e-3) and r["rule"]   # its top in today's prices: 2% up


def test_a_stock_with_a_line_you_marked_is_followed_watched_or_not(paths_):
    """'任何股票：你在 K 线上勾了提醒的线' (design 2026-10-10 §2): followed for that line though in none of your groups;
    a line without 提醒 is not."""
    raw = closes(paths_, PING)["raw_close"][-1]
    day = str(closes(paths_, PING)["date"][-1])
    ws = Workspace(paths_)
    ws.save_drawing(PING, "horizontalStraightLine", [{"date": day, "price": raw * 1.03}], alert=True)
    ws.save_drawing(MAO, "horizontalStraightLine", [{"date": day, "price": 1.0}])
    ids = Live(lambda m: paths_, source_for=lambda p, m: Script(paths_, {}), allow_demo=True, markets=("CN",)).followed("CN")["listing_id"].to_list()
    assert PING in ids and MAO not in ids


def test_a_stock_you_look_at_is_asked_for_but_never_judged(paths_):
    """A stock open on the K-line page is polled first for its 分时 (design 2026-10-12 §9) — only fetched: a 潜伏 stock
    you do not watch says nothing as it runs through its top."""
    c = closes(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])
    src, events = Script(paths_, {PING: up_to(1.02, 0.97, 1.001)}), []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    asked, minutes = [], src.minutes
    src.minutes = lambda ids, day, indexes=(): asked.append(list(ids)) or minutes(ids, day, indexes)
    for t in [(9, 31, 5), (9, 32, 5)]:
        now = datetime(2026, 10, 2, *t, tzinfo=SH)
        lv.view(PING, now)
        src.upto = now.minute - 30 + 1
        lv.tick(now)
    assert any(PING in ids for ids in asked)
    assert [a for e in events if e["type"] == "alert" for a in e["alerts"]] == []


def test_a_held_stocks_fail_line_warns_on_the_way_down_at_it_and_before_the_close(paths_):
    follow(paths_, PING, "holding")
    prev = closes(paths_, PING)["raw_close"][-1]
    Watch(lambda m: paths_).add_condition(PING, "fail", text="假底", op="<", price=round(prev * 0.97, 2))
    down = [(1.0, 0.985, 0.985), (0.985, 0.975, 0.975), (0.975, 0.968, 0.969)] + [(0.969, 0.969, 0.969)] * 237
    got = run(paths_, Script(paths_, {PING: down}), [(9, 30, 30), (9, 31, 5), (9, 32, 5), (14, 49, 5), (14, 50, 5)])
    assert [(a["kind"], a["at"][11:16]) for a in got] == [("near", "09:31"), ("touch", "09:32"), ("hold", "14:50")]
    assert got[0]["text"].startswith("可能跌破，注意看：离失效线（假底）") and got[1]["text"].endswith("（你的计划以收盘为准）")
    assert got[2]["text"].startswith("收盘前仍在失效线（假底）") and len(sent(paths_)) == 3


def test_a_fail_line_set_during_the_session_starts_from_the_price_then(paths_):
    # 5% up when you set a line 3% up: under the price now, so it is watched on the way down (not "already passed")
    follow(paths_, PING, "holding")
    prev = closes(paths_, PING)["raw_close"][-1]
    src = Script(paths_, {PING: [(1.05, 1.05, 1.05)] * 3 + [(1.05, 1.029, 1.029)] + [(1.029, 1.029, 1.029)] * 236})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.upto = 2
    lv.tick(datetime(2026, 10, 2, 9, 31, 5, tzinfo=SH))
    Watch(lambda m: paths_).add_condition(PING, "fail", text="回落", op="<", price=round(prev * 1.03, 2),
                                          now=datetime(2026, 10, 2, 9, 32, tzinfo=SH).astimezone().replace(tzinfo=None))
    src.upto = 4
    lv.tick(datetime(2026, 10, 2, 9, 33, 5, tzinfo=SH))
    got = [a for e in events if e["type"] == "alert" for a in e["alerts"]]
    assert [(a["kind"], a["at"][11:16], a["detail"]["side"]) for a in got] == [("touch", "09:33", "down")]


def test_a_gap_to_the_line_at_the_open_is_the_auctions_alert_and_quiets_what_it_already_said(paths_):
    # opened beyond the line: the touch is said by the auction; opened 0.5% under it: the approach is
    follow(paths_, MAO)
    c = closes(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])   # its top 2% up
    follow(paths_, PING)
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    src = Script(paths_, {MAO: [(r * 1.01, r * 1.004, r * 1.005)] * 3, PING: up_to(1.02, 0.995, 0.997, 1.001)},
                 opens={MAO: r * 1.005, PING: 1.02 * 0.995})
    got = run(paths_, src, [(9, 30, 10), (9, 31, 5), (9, 32, 5)])
    assert [(a["listing_id"], a["kind"], a["detail"]["how"]) for a in got] == [
        (PING, "auction", "near"), (MAO, "auction", "beyond"), (PING, "touch", None)]
    assert got[0]["text"].startswith("高开到箱顶") and got[1]["text"].startswith("高开在箱顶（中箱）之上：开 ")


def test_a_line_already_passed_or_out_of_todays_reach_is_not_watched(paths_):
    follow(paths_, PING, "holding")
    prev = closes(paths_, PING)["raw_close"][-1]
    w = Watch(lambda m: paths_)
    w.add_condition(PING, "fail", text="已经在下面", op="<", price=round(prev * 1.01, 2))
    w.add_condition(PING, "fail", text="跌停之下", op="<", price=round(prev * 0.88, 2))
    w.add_condition(PING, "fail", text="够得着", op="<", price=round(prev * 0.95, 2))
    lv = Live(lambda m: paths_, source_for=lambda p, m: Script(paths_, {}), allow_demo=True, markets=("CN",))
    assert [x["note"] for x in lv.lines("CN", datetime(2026, 10, 2, 9, 35, tzinfo=SH))] == ["够得着"]


def test_the_market_falling_two_percent_is_said_once_to_everyone(paths_):
    follow(paths_, PING)
    src = Script(paths_, {IDX: [(1.0, 0.975, 0.976)] * 3})
    got = run(paths_, src, [(9, 31, 5), (9, 32, 5)])
    assert [(a["kind"], a["listing_id"], a["title"]) for a in got] == [("market", IDX, "A 股 · 大盘")]
    assert got[0]["text"] == "大盘急跌：上证指数 今天 -2.4%" and sent(paths_)[0].endswith("大盘急跌：上证指数 今天 -2.4%")


def test_a_feed_stopped_for_three_minutes_is_said_and_so_is_its_return(paths_):
    follow(paths_, PING)
    src = Script(paths_, {})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    for mi, err in ((31, None), (32, RuntimeError("TickFlow 503")), (33, RuntimeError("TickFlow 503")),
                    (34, RuntimeError("TickFlow 503")), (35, RuntimeError("TickFlow 503")), (36, None)):
        src.upto, src.error = mi - 30 + 1, err
        lv.tick(datetime(2026, 10, 2, 9, mi, 5, tzinfo=SH))
    got = [a for e in events if e["type"] == "alert" for a in e["alerts"]]
    assert [(a["kind"], a["at"][11:16]) for a in got] == [("feed", "09:35"), ("feed", "09:36")]
    assert got[0]["text"] == "盘中行情中断 3 分钟，盘中提醒暂停（TickFlow 503）" and got[1]["text"] == "盘中行情恢复，盘中提醒继续"
    assert len(sent(paths_)) == 2


def test_an_alert_for_feishu_is_sent_the_moment_it_is_queued(paths_):
    # not at the scheduler's next round (every 30 seconds): a price can be through the line by then (design 2026-10-10 §5)
    follow(paths_, MAO)
    c = closes(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c["close"][-1], c["close"][-1] * 1.02 * 1.015))])   # in the app only
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    src, calls = Script(paths_, {MAO: up_to(r, 0.97, 0.98, 1.001), PING: up_to(1.02, 0.97, 0.98, 1.001)}), []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, send=lambda: calls.append(len(sent(paths_))), allow_demo=True,
              markets=("CN",))
    for mi in (31, 32):
        src.upto = mi - 30 + 1
        lv.tick(datetime(2026, 10, 2, 9, mi, 5, tzinfo=SH))
    assert calls == [1]                                    # after MAO's touch was queued; PING's touch is not for Feishu


class Quick(Script):
    """A Script with the fast path's quotes (TickFlow): {lid: (open, high, low, last)} as multiples of the last close,
    stamped `qat`; the stocks each call asked for are kept."""
    def __init__(self, paths, days, quotes, opens=None):
        super().__init__(paths, days, opens)
        self.q, self.qat, self.asked_quotes = quotes, None, []

    def quotes(self, ids):
        self.asked_quotes.append(sorted(ids))
        return pl.DataFrame([(lid, self.qat, *[self.prev(lid) * x if x else None for x in self.q[lid]], self.prev(lid), 0.0, 0.0)
                             for lid in ids if lid in self.q], schema=QUOTES, orient="row")


def test_a_stock_near_its_line_is_asked_for_quotes_every_few_seconds_and_its_high_past_the_line_is_the_touch(paths_):
    # design 2026-10-10 §5: the minutes come every 30 seconds; a price can be through the line within them
    follow(paths_, MAO)
    follow(paths_, PING)                                                     # no line near: never asked for
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    src = Quick(paths_, {MAO: up_to(r, 0.97, 0.995)}, {MAO: (0.97 * r, 1.002 * r, 0.97 * r, 1.001 * r), PING: (1, 1, 1, 1)})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.upto = 2
    lv.tick(datetime(2026, 10, 2, 9, 31, 5, tzinfo=SH))                   # the minutes: 0.5% under the line, an approach
    src.qat = datetime(2026, 10, 2, 9, 31, 7)
    for sec in (8, 9, 11, 14):                                              # every 3 seconds; until the touch
        lv.tick(datetime(2026, 10, 2, 9, 31, sec, tzinfo=SH))
    got = [a for e in events if e["type"] == "alert" for a in e["alerts"]]
    assert [(a["kind"], a["at"][11:19]) for a in got] == [("near", "09:31:00"), ("touch", "09:31:07")]
    assert src.asked_quotes == [[MAO]] and got[1]["detail"]["move2"] is None and got[1]["text"].startswith("已到箱顶")


def test_a_long_poll_does_not_hold_up_the_quotes_every_few_seconds(paths_):
    # a poll on a long watch list takes seconds (the rules on today's bar, 10-12: up to a minute): the fast path is a
    # loop of its own — a stock near its line is asked for while the poll is still under way, and its touch said then
    follow(paths_, MAO)
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    stuck, polling = threading.Event(), threading.Event()

    class Slow(Quick):
        def minutes(self, ids, day, indexes=()):
            if self.upto > 2:   # the second poll: as long as the test holds it
                polling.set()
                stuck.wait(30)
            return super().minutes(ids, day, indexes)

    src = Slow(paths_, {MAO: up_to(r, 0.97, 0.995, 0.996)}, {MAO: (0.97 * r, 1.002 * r, 0.97 * r, 1.001 * r)})
    events, now = [], [datetime(2026, 10, 2, 9, 31, 5, tzinfo=SH)]
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.upto = 2
    lv.start(every=0.01, clock=lambda: now[0])
    try:
        assert until(lambda: any(e["type"] == "quotes" for e in events))   # the first poll: 0.5% under the line
        src.upto, src.qat = 3, datetime(2026, 10, 2, 9, 31, 38)
        now[0] = datetime(2026, 10, 2, 9, 31, 40, tzinfo=SH)                 # the next poll is due, and takes long
        assert polling.wait(30)
        assert until(lambda: any(a["kind"] == "touch" for e in events if e["type"] == "alert" for a in e["alerts"]))
        assert not stuck.is_set() and src.asked_quotes == [[MAO]]             # said while the poll is still waiting
    finally:
        lv.stop()
        stuck.set()


def until(ok, seconds: float = 30) -> bool:
    end = monotonic() + seconds
    while not ok():
        if monotonic() > end:
            return False
        sleep(0.01)
    return True


def test_the_auctions_price_is_judged_from_quotes_before_the_open(paths_):
    # 9:25–9:30: the auction is done, trading not begun — the open is known five minutes before the first minute
    follow(paths_, MAO)
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    src = Quick(paths_, {MAO: [(1.004 * r, 1.0 * r, 1.003 * r)] * 3}, {MAO: (None, None, None, None)}, opens={MAO: 1.003 * r})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.qat = datetime(2026, 10, 2, 9, 25, 2)
    lv.tick(datetime(2026, 10, 2, 9, 25, 5, tzinfo=SH))                   # not matched yet: no open
    src.q, src.qat = {MAO: (1.003 * r, 1.003 * r, 1.003 * r, 1.003 * r)}, datetime(2026, 10, 2, 9, 25, 3)
    lv.tick(datetime(2026, 10, 2, 9, 25, 12, tzinfo=SH))                  # within 15 seconds of the last ask: not asked
    lv.tick(datetime(2026, 10, 2, 9, 25, 21, tzinfo=SH))
    src.upto = 1
    lv.tick(datetime(2026, 10, 2, 9, 30, 40, tzinfo=SH))                  # the first minute: said already
    got = [a for e in events if e["type"] == "alert" for a in e["alerts"]]
    assert [(a["kind"], a["at"][11:19], a["detail"]["how"]) for a in got] == [("auction", "09:25:03", "beyond")]
    assert len(src.asked_quotes) == 2


def test_a_quote_older_than_15_seconds_says_its_data_is_late(paths_):
    follow(paths_, MAO)
    r = MAO_TOP / closes(paths_, MAO)["raw_close"][-1]
    src = Quick(paths_, {MAO: up_to(r, 0.97, 0.995)}, {MAO: (0.97 * r, 1.002 * r, 0.97 * r, 1.001 * r)})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.upto = 2
    lv.tick(datetime(2026, 10, 2, 9, 31, 5, tzinfo=SH))
    src.qat = datetime(2026, 10, 2, 9, 30, 50)
    lv.tick(datetime(2026, 10, 2, 9, 31, 8, tzinfo=SH))
    touch = [a for e in events if e["type"] == "alert" for a in e["alerts"] if a["kind"] == "touch"]
    assert "行情延迟" in touch[0]["detail"]["tags"]


def test_an_open_beyond_the_days_limits_is_an_ex_rights_day_not_an_auction(paths_):
    # a 10送10 day whose factor has not come: the open shows at half the price — not 低开在失效线之下
    follow(paths_, PING, "holding")
    prev = closes(paths_, PING)["raw_close"][-1]
    Watch(lambda m: paths_).add_condition(PING, "fail", text="假底", op="<", price=round(prev * 0.97, 2))
    src = Quick(paths_, {}, {PING: (0.5, 0.5, 0.5, 0.5)})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.qat = datetime(2026, 10, 2, 9, 25, 3)
    lv.tick(datetime(2026, 10, 2, 9, 25, 5, tzinfo=SH))
    assert src.asked_quotes == [[PING]] and [e for e in events if e["type"] == "alert"] == []


def test_a_stock_on_several_latent_lists_is_watched_against_each_rules_line(paths_):
    # one stock, three rules' boxes: two tops 3% apart are both watched; a third within 0.2% of one is the same line
    c = closes(paths_, PING)["close"][-1]
    follow(paths_, PING)
    scanned(paths_, "CN", [(PING, "watch", detail(c, c * 1.02 * 1.015))], rule_id="tpl_shang_heng_shang")
    scanned(paths_, "CN", [(PING, "watch", detail(c, c * 1.05 * 1.015))], rule_id="tpl_calm_breakout")
    scanned(paths_, "CN", [(PING, "watch", detail(c, c * 1.0505 * 1.015))], rule_id="tpl_xia_heng_shang")
    lv = Live(lambda m: paths_, source_for=lambda p, m: Script(paths_, {}), allow_demo=True, markets=("CN",))
    got = sorted((round(x["price"] / closes(paths_, PING)["raw_close"][-1], 4), x["kind"]) for x in lv.lines("CN", datetime(2026, 10, 2, 9, 35, tzinfo=SH)))
    assert got == [(1.02, "box"), (1.05, "box")]


def test_the_cards_tags_say_what_to_watch_for():
    # far above the average price (尾盘 still: the next day tends to fall back — a saying, untested, as the page's
    # note puts it; review 10-21: the scrub had left it a bare claim); a new high on less
    # volume than the high before (后量不超前量); a fall on more volume than the fall before; the limit-up
    assert tags(True, vwap_gap=0.052, to_limit=0.05, weak=False, late=False, wave=None) == ["远离均价线 5 个点以上（经验说法，没有经过检验）"]
    assert tags(True, vwap_gap=0.052, to_limit=0.015, weak=True, late=True, wave="新高量不足") == [
        "大盘弱", "远离均价线 5 个点以上，尾盘还这样，次日容易回落（经验说法，没有经过检验）", "可能封板", "新高量不足：后量不超前量"]
    assert tags(False, vwap_gap=-0.06, to_limit=0.04, weak=False, late=False, wave="下跌放量") == ["下跌放量：这一波跌得比上一波量大"]
    assert tags(False, vwap_gap=-0.01, to_limit=0.04, weak=False, late=False, wave="新高量不足") == []   # not the selling side's


BOX_UP = """
id: box_up
name: 箱体上破
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: [mid, long]}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
watch_stage: big
"""


class Busy(Script):
    """A Script trading a million shares a minute: a day's volume far over the days before."""
    def minutes(self, ids, day, indexes=()):
        return super().minutes(ids, day, indexes).with_columns(pl.col("volume") * 1000)


def seven_rules(paths) -> Busy:
    """Seven 潜伏 rules: six on the boxes' default settings — five templates also read the 金品, 假底 and slant boxes — one
    crossing at 3%, on lists of their own; and a day of 600519 rising 0.06% a minute to +6%, 000001 falling."""
    rules = Rules(paths)
    rules.save(BOX_UP)
    rules.save(BOX_UP.replace("box_up", "box_up_wide").replace("[mid, long]}", "[mid, long], tol: 0.03}"))
    c = closes(paths, MAO)
    line = MAO_TOP * c["close"][-1] / c["raw_close"][-1] * 1.015            # its break line as the scan keeps it
    lists = {"box_up": [(MAO, "watch", detail(c["close"][-1], line)), (PING, "watch", None)], "box_up_wide": [(MAO, "watch", None)],
             "tpl_xia_heng_shang": [(PING, "watch", None)]}
    for rid in ("box_up", "box_up_wide", "tpl_shang_pin", "tpl_tian_pin", "tpl_xia_heng_shang", "tpl_calm_breakout", "tpl_xia_pin_turn"):
        scanned(paths, "CN", lists.get(rid, [(MAO, "watch", None), (PING, "watch", None)]), rule_id=rid)
    mao = [(1 + 0.0006 * k * 1.002, 1 + 0.0006 * k * 0.998, 1 + 0.0006 * k) for k in range(100)] + [(1.06, 1.059, 1.06)] * 140
    ping = [(1 - 0.0004 * k + 0.001, 1 - 0.0004 * k - 0.001, 1 - 0.0004 * k) for k in range(240)]
    follow(paths, MAO), follow(paths, PING)   # yours: 盯盘 follows your stocks only (10-12)
    return Busy(paths, {MAO: mao, PING: ping})


def test_a_days_alerts_on_the_rules_structures_poll_after_poll(paths_):
    # the rules on today's bar (spec §7.2) through a day: each says it on the first poll after the minute its own break
    # line is passed, once — box_up at its 中箱 top × 1.015 (+2.33%, 10:09), box_up_wide × 1.03 (+3.8%, 10:35),
    # tpl_tian_pin at its 天品's small box top 2,247.60 × 1.015 ÷ 1.3 (the adjustment) = 1,754.85 (+2.73%, 10:16) on a
    # day's volume no smaller than yesterday's
    src = seven_rules(paths_)
    times = [(9, 31, 10), (9, 40, 10), (9, 45, 10), (10, 6, 10), (10, 12, 10), (10, 18, 10), (10, 30, 10), (10, 36, 10), (13, 5, 10),
             (14, 50, 10)]
    got = run(paths_, src, times)
    assert [(a["listing_id"], a["kind"], a["key"], a["at"][11:19], round(a["price"], 2)) for a in got] == [
        (MAO, "auction", "box:box_up:CN:600519", "09:30:00", 1708.31),    # opened 0.8% under the top: the approach is said
        (MAO, "touch", "box:box_up:CN:600519", "09:44:00", 1722.66),
        (MAO, "rule", "box_up", "10:12:10", 1751.36),
        (MAO, "rule", "tpl_tian_pin", "10:18:10", 1757.51),
        (MAO, "rule", "box_up_wide", "10:36:10", 1775.96)]
    why = {a["key"]: a["detail"]["lines"] for a in got if a["kind"] == "rule"}
    assert why["box_up"] == why["box_up_wide"] == ["CN:600519 2026-10-02 信号", "  箱体：2026-08-26 至 2026-09-30，2026-09-30 成立",
                                                   "  向上突破：2026-10-02"]
    assert why["tpl_tian_pin"][:4] == ["CN:600519 2026-10-02 信号", "  小箱前两个月：2026-07-27 至 2026-09-18（40 根）",
                                       "  天品：2026-09-21 至 2026-09-30，2026-09-29 成立", "  向上突破：2026-10-02"]
    assert why["tpl_tian_pin"][-2:] == ["  ✓ 量不比昨天少：95.45 倍 ≥ 1 倍", "    公式：VOL_MULT ≥ $vol"]


def test_a_polls_rules_compute_each_structure_once_and_the_bars_before_today_once_a_day(paths_, monkeypatch):
    # every poll judges seven rules: the boxes are drawn once for the six on the same settings and once for the one at 3%;
    # the bars before today are scanned once a day — the first poll (600519 and 000001; 600519 at 3%) — and each poll
    # after it, the afternoon's too, scans its forming bar from where they left each box machine
    src = seven_rules(paths_)
    calls = []
    detect, scans = primitives.detect, box._scans
    monkeypatch.setattr(primitives, "detect", lambda bars, scales, shared: calls.extend(
        f"draw {shared.tol} {lid}" for lid in bars["listing_id"].unique(maintain_order=True)) or detect(bars, scales, shared))
    monkeypatch.setattr(box, "_scans", lambda *a: calls.append("before" if a[-1] else "bar") or scans(*a))
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=lambda e: None, allow_demo=True, markets=("CN",))
    seen = []
    for hour, minute, upto in ((10, 6, 37), (10, 7, 38), (12, 0, 120), (13, 1, 122)):   # and over lunch: kept for the afternoon
        src.upto = upto
        lv.tick(datetime(2026, 10, 2, hour, minute, 10, tzinfo=SH))
        seen.append(sorted(calls))
        calls.clear()
    drawn = [f"draw 0.015 {PING}", f"draw 0.015 {MAO}", f"draw 0.03 {MAO}"]   # each stock once a setting, not once a rule
    assert seen == [["bar"] * 3 + ["before"] * 3 + drawn, ["bar"] * 3 + drawn, [], ["bar"] * 3 + drawn]


# F3 (review F, 10-13): 盯盘's time is its prices' time. A source that keeps answering but with the same old minutes
# (frozen) or minutes 4 behind the clock was 行情中断 to the alerts, but the page's `at` — what the status bar and the
# 盯盘 header show and age (amber after 3 minutes, design 2026-10-12 §14) — was each poll's own time, always fresh.


def test_a_source_answering_with_old_minutes_is_not_fresh(paths_):
    follow(paths_, MAO)
    src = Script(paths_, {MAO: up_to(1.0, *[1.0] * 240)})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    src.upto = 15   # through 09:44
    lv.tick(datetime(2026, 10, 2, 9, 45, 5, tzinfo=SH))
    assert events[-1]["type"] == "quotes" and events[-1]["at"] == "2026-10-02T09:45:00+08:00"   # 09:44's minute ends 09:45
    lv.tick(datetime(2026, 10, 2, 9, 50, 5, tzinfo=SH))          # the source answers, still through 09:44: frozen
    quotes = [e for e in events if e["type"] == "quotes"]
    assert quotes[-1]["at"] == "2026-10-02T09:45:00+08:00"       # the prices are 09:45's, five minutes old by now
    src.upto = 21
    lv.tick(datetime(2026, 10, 2, 9, 51, 5, tzinfo=SH))          # it catches up
    assert [e for e in events if e["type"] == "quotes"][-1]["at"] == "2026-10-02T09:51:00+08:00"


# F5 (review F, 10-13): after a gap in the feed an approach minutes old is not said as if it were now. On the outage
# replay of 10-09 (TickFlow failing 09:45–09:55) 000411's 逼近 of 09:48 (10.12, 0.5% under its box top) was said — and
# queued for Feishu — at 09:55, when it stood at 10.04, out of the zone and falling.
# 600519's own 中箱 top is 0.82% over its last close: it opens 1.8% under the top (no auction alert), at 09:31 makes a
# new high 0.4% under it above the average price (an approach), then falls back 1% and stays there
APPROACH_DAY = [(1.0, 0.99, 0.999), (1.004, 0.999, 1.004)] + [(1.0, 0.99, 0.99)] * 10


def approaches(paths, outage: bool) -> list[str]:
    follow(paths, MAO)
    src = Script(paths, {MAO: APPROACH_DAY}, opens={MAO: 0.99})
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    for mi in range(30, 38):
        now = datetime(2026, 10, 2, 9, mi, 5, tzinfo=SH)
        src.upto = mi - 30 + 1
        src.error = RuntimeError("TickFlow 503") if outage and 31 <= mi <= 36 else None
        lv.tick(now)
    return [a["at"] for e in events if e["type"] == "alert" for a in e["alerts"] if a["kind"] == "near"]


def test_without_a_gap_the_approach_is_said_at_its_minute(paths_):
    assert approaches(paths_, outage=False) == ["2026-10-02T09:31:00"]


def test_after_a_gap_an_approach_the_price_has_left_is_not_said(paths_):
    assert approaches(paths_, outage=True) == []


def test_on_an_ex_date_a_held_stocks_fail_line_stands_in_todays_prices(paths_):
    """Review 10-13: on a dividend day the fail line set the day before stood the dividend too high in today's prices, so
    a stock trading flat at the exchange's reference was 'at its 失效线' from the open; the quote's 昨收 (the exchange's
    reference price — the day's corporate action comes with the evening's update) says the day's factor."""
    from wb.realtime.providers.tushare_rt import SNAPSHOT
    follow(paths_, PING, "holding")
    prev = closes(paths_, PING)["raw_close"][-1]
    Watch(lambda m: paths_).add_condition(PING, "fail", text="假底", op="<", price=round(prev * 0.97, 2))
    ref = prev * 0.95                                                     # a 5% 派息
    folder = paths_.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(PING, datetime(2026, 10, 2, 9, 31, 3), ref, ref, ref, ref, 1e6, 1e7, ref)], schema=SNAPSHOT,
                 orient="row").write_parquet(folder / f"snapshot={DAY}.parquet")
    got = run(paths_, Script(paths_, {PING: [(0.95, 0.95, 0.95)] * 240}, opens={PING: 0.95}), [(9, 30, 30), (9, 31, 5), (9, 32, 5)])
    assert got == []                                                      # flat on the day, 3% over its line


FLAT = [(0.999, 0.998, 0.999)] * 239   # 1.8% under 600519's own top all day


def test_after_the_close_an_approach_in_the_last_minute_is_not_said(paths_):
    """You chose (10-13, review F): 「可能突破」 said at 15:00 for 14:59's minute is no use — the day is over."""
    follow(paths_, MAO)
    got = run(paths_, Script(paths_, {MAO: FLAT + [(1.004, 0.999, 1.004)]}, opens={MAO: 0.99}), [(14, 58, 5), (15, 0, 30)])
    assert [a["kind"] for a in got] == []


def test_after_the_close_a_touch_in_the_last_minute_is_still_said(paths_):
    follow(paths_, MAO)
    got = run(paths_, Script(paths_, {MAO: FLAT + [(1.01, 0.999, 1.01)]}, opens={MAO: 0.99}), [(14, 58, 5), (15, 0, 30)])
    assert [(a["kind"], a["at"][11:16]) for a in got] == [("touch", "14:59")]


def test_while_the_feed_lags_nothing_is_judged_and_a_touch_in_the_gap_is_said_when_it_catches_up(paths_):
    """You chose (10-13, review F): 盘中提醒暂停 is a pause — on minutes 3 or more behind the clock nothing is judged (what
    they show is minutes old); once the data catches up a touch in the gap is said, with its time."""
    follow(paths_, MAO)
    day = [(0.999, 0.998, 0.999)] * 21 + [(1.01, 1.0, 1.01)] * 219    # 9:51 over the top
    src = Script(paths_, {MAO: day}, opens={MAO: 0.99})
    events = []
    lv = Live(lambda m: paths_, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    for mi, upto in ((50, 21), (55, 22), (56, 27)):    # through 9:50; at 9:55 still through 9:51 (4 behind); caught up
        src.upto = upto
        lv.tick(datetime(2026, 10, 2, 9, mi, 5, tzinfo=SH))
        lines = [(a["kind"], a["at"][11:16]) for e in events if e["type"] == "alert" for a in e["alerts"] if a["kind"] != "feed"]
        assert lines == ([("touch", "09:51")] if mi == 56 else []), mi
