"""市场看板的每分钟记录 (design 2026-10-12 §8.2–§8.4): only the live thread asks TickFlow for the whole market — a row a
minute while the market trades, every 10 s while a page wants it, the closing windows refreshed until the print is
in — keeps the latest quote as the day's snapshot file, fetches today's limit prices before the open, fills the
minutes it missed from 1-minute bars; a failure is the board's own, never 盯盘's."""
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from wb.config import get_paths
from wb.data.store import Store
from wb.devdata import write
from wb.jobs.live import Live
from wb.market.events import events
from wb.market.tape import MarketTape
from wb.realtime.providers.tickflow_rt import QUOTES, TickFlowError
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.realtime.session import session_at
from wb.workspace.store import Workspace

CST = ZoneInfo("Asia/Shanghai")
YDAY, TODAY = date(2026, 9, 30), date(2026, 10, 1)
IDS = ("CN:600519", "CN:000001")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_SECRET_TICKFLOW_API_KEY", "k")
    p = get_paths("prod", tmp_path)
    Workspace(p).set_setting("profile.CN", "prod")
    write(p, 320)
    st = Store(p, "CN")
    ahead = [YDAY + timedelta(days=k) for k in range(1, 12)]
    st.publish({"calendar": pl.DataFrame({"date": ahead, "is_open": [d.weekday() < 5 for d in ahead], "source": ["fake"] * len(ahead)})},
               as_of=YDAY)
    return p


def closes(p) -> dict[str, float]:
    b = Store(p, "CN").read("bars_1d").sort("date")
    return {lid: b.filter(pl.col("listing_id") == lid)["close"][-1] for lid in IDS}


class FakeTF:
    """TickFlow for the tape: the whole market (the two demo stocks at `mult` × their close, a third that has not
    traded, one stamped the day before), three index quotes, and 1-minute bars of any day."""
    name, delay, poll_seconds, max_listings, approx_volume = "tickflow_rt", 0, 30, 297, False

    def __init__(self, p, day=TODAY):
        self.prev, self.day, self.calls, self.fail, self.now, self.mult = closes(p), day, [], False, None, 1.02
        self.bars_prev = {lid: c for lid, c in self.prev.items()}

    def whole_market(self):
        self.calls.append("market")
        if self.fail:
            raise TickFlowError("TickFlow 500：Internal Server Error")
        at = self.now.replace(tzinfo=None)
        rows = [(lid, at, p, p * self.mult, p, p * self.mult, p, 1000.0, 1e6) for lid, p in self.prev.items()]
        rows += [("CN:300001", at, None, None, None, 9.9, 10.0, 0.0, 0.0),                       # auction not matched
                 ("CN:300002", datetime.combine(self.day - timedelta(days=1), time(15, 0)), 5.0, 5.1, 4.9, 5.0, 5.0, 1e5, 5e5)]
        return pl.DataFrame(rows, schema=QUOTES, orient="row")

    def quotes(self, ids):
        at = self.now.replace(tzinfo=None)
        return pl.DataFrame([(i, at, 3800.0, 3815.0, 3790.0, 3810.0, 3800.0, 0.0, 0.0) for i in ids], schema=QUOTES, orient="row")

    def minutes(self, ids, day, indexes=()):
        self.calls.append(("minutes", tuple(sorted(ids)), day))
        rows = []
        for lid in [*ids, *indexes]:
            base = 3800.0 if ":IDX:" in lid else self.prev.get(lid, 10.0)
            for k in range(240):
                t = (datetime.combine(day, time(9, 30)) + timedelta(minutes=k)) if k < 120 else (datetime.combine(day, time(13, 0)) + timedelta(minutes=k - 120))
                px = base * (1 + 0.0001 * k)
                rows.append((lid, t, px, px, px, px, 100.0))
        return pl.DataFrame(rows, schema=MINUTES, orient="row").with_columns(pl.lit(1e5).alias("amount"))


def sess(p, h, m, s=0, day=TODAY):
    cal = Store(p, "CN").read("calendar")
    opens = dict(zip(cal["date"], cal["is_open"], strict=True))
    now = datetime(day.year, day.month, day.day, h, m, s, tzinfo=CST)
    return session_at("CN", now, lambda d: opens.get(d)), now


def limits_of(p):
    return pl.DataFrame({"listing_id": list(IDS), "exchange": ["SH", "SZ"], "up_limit": [v * 1.1 for v in closes(p).values()],
                         "down_limit": [v * 0.9 for v in closes(p).values()]}).vstack(
        pl.DataFrame({"listing_id": ["CN:920001"], "exchange": ["BJ"], "up_limit": [13.0], "down_limit": [7.0]}))


def tape_for(p, **kw):
    published = []
    kw.setdefault("fetch_limits", lambda day: limits_of(p))
    kw.setdefault("run", lambda f: f())
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("fill", False)                                  # the fill tests turn it on
    return MarketTape(lambda m: p, lambda pp, m: None, published.append, **kw), published


def tick(tape, p, src, h, m, s=0, day=TODAY):
    sn, now = sess(p, h, m, s, day)
    src.now = now
    tape.tick("CN", sn, src, now)


def test_rows_come_once_a_minute_and_every_10_s_while_wanted(paths):
    tape, published = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 10, 0, 5)
    tick(tape, paths, src, 10, 0, 20)                             # the same minute, nobody watching: no call
    assert src.calls.count("market") == 1
    tape.want("CN", datetime(2026, 10, 1, 10, 0, 25, tzinfo=CST))
    tick(tape, paths, src, 10, 0, 30)
    tick(tape, paths, src, 10, 0, 35)                             # 5 s after the last: not yet
    tick(tape, paths, src, 10, 0, 45)
    assert src.calls.count("market") == 3
    tape.want("CN", datetime(2026, 10, 1, 10, 0, 45, tzinfo=CST))
    tick(tape, paths, src, 10, 1, 30)                             # wanted 45 s ago: once a minute again
    tick(tape, paths, src, 10, 1, 35)
    assert src.calls.count("market") == 4
    rows = tape.rows("CN", TODAY)
    assert [r["i"] for r in rows] == [30, 31]                      # 10:00 is the 30th minute, refreshed in place
    assert rows[0]["at"] == datetime(2026, 10, 1, 10, 0, 45) and rows[0]["sh_last"] == 3810.0
    assert published[-1] == {"type": "board", "market": "CN", "at": "2026-10-01T10:01:30"}
    assert tape.latest("CN")["board"]["traded"] == 2


def test_the_closing_row_is_refreshed_until_1505_and_lunch_until_1135(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    for hm in ((11, 30, 5), (11, 30, 15), (11, 30, 30), (11, 36, 0)):
        tick(tape, paths, src, *hm)
    assert src.calls.count("market") == 2 and [r["i"] for r in tape.rows("CN", TODAY)] == [120]
    for hm in ((15, 0, 5), (15, 0, 15), (15, 0, 30), (15, 4, 55), (15, 5, 10)):
        tick(tape, paths, src, *hm)
    assert src.calls.count("market") == 5
    last = tape.rows("CN", TODAY)[-1]
    assert (last["i"], last["at"]) == (240, datetime(2026, 10, 1, 15, 4, 55))


def test_the_auction_result_is_a_board_without_a_row(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 9, 20)                                 # the reference price moves: nothing yet
    assert src.calls.count("market") == 0
    tick(tape, paths, src, 9, 26)
    assert src.calls.count("market") == 1 and tape.latest("CN")["board"]["at"] == "2026-10-01T09:26:00"
    assert tape.rows("CN", TODAY) == []


def test_limits_are_fetched_from_0900_until_complete(paths):
    asked = []
    tape, _ = tape_for(paths, fetch_limits=lambda day: asked.append(day) or limits_of(paths))
    src = FakeTF(paths)
    for hm in ((8, 59), (9, 0), (9, 2), (9, 5), (9, 10), (10, 0)):
        tick(tape, paths, src, *hm)
    assert asked == [TODAY, TODAY]                                # twice the same: complete, never asked again
    assert (paths.market_dir("CN") / "tape" / f"limits={TODAY}.parquet").exists()
    assert tape.facts("CN", TODAY).limits == "today"
    again, _ = tape_for(paths, fetch_limits=lambda day: pytest.fail("kept on disk: not asked"))
    tick(again, paths, src, 10, 1)
    assert again.facts("CN", TODAY).limits == "today"


def test_nothing_is_fetched_when_the_board_cannot_be_live(paths, monkeypatch):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    assert tape.why_not("CN") is None
    assert "日股" in tape.why_not("JP")
    Workspace(paths).set_setting("market.tape", False)
    assert "设置" in tape.why_not("CN")
    tick(tape, paths, src, 10, 0)
    Workspace(paths).set_setting("market.tape", True)
    Workspace(paths).set_setting("datause.tickflow_rt.store", False)
    assert "数据使用" in tape.why_not("CN")
    tick(tape, paths, src, 10, 1)
    Workspace(paths).set_setting("datause.tickflow_rt.store", True)
    monkeypatch.delenv("WB_SECRET_TICKFLOW_API_KEY")
    assert "key" in tape.why_not("CN")
    tick(tape, paths, src, 10, 2)
    assert src.calls == []
    test = MarketTape(lambda m: get_paths("test", paths.root), lambda pp, m: None, lambda e: None)
    assert "测试档" in test.why_not("CN")


def test_a_failed_market_quote_is_the_boards_error_not_the_feeds(paths):
    src = FakeTF(paths)
    src.fail = True
    lv = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("CN",))
    lv.tape.fill = False
    src.now = datetime(2026, 10, 1, 10, 0, 5, tzinfo=CST)
    lv.tick(src.now)
    assert lv.tape.latest("CN")["error"] == "最近一次 10:00：TickFlow 500：Internal Server Error"   # when it was tried, said so
    assert lv.state("CN", src.now)["error"] is None                # 盯盘 has its prices: no 行情中断


def test_the_live_state_says_when_the_whole_market_quote_is_from(paths):
    """盯盘's state carries the whole-market quote's time beside its own poll's — the K-line's forming bar and 观察 read
    that quote — so the status bar can say when either stops (the 10-12 rule: a price not updated for 3 minutes is said)."""
    src = FakeTF(paths)
    lv = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("CN",))
    lv.tape.fill = False
    assert lv.state("CN", datetime(2026, 10, 1, 9, 50, tzinfo=CST))["quote_at"] is None   # nothing fetched yet
    src.now = datetime(2026, 10, 1, 10, 0, 5, tzinfo=CST)
    lv.tick(src.now)
    assert lv.state("CN", src.now)["quote_at"] == lv.tape.latest("CN")["at"] == "2026-10-01T10:00:05"


def test_the_volume_clock_is_read_once_a_day(paths, monkeypatch):
    # the clock reads every kept day of minutes (about 20 files): once a day, not at every fetch (each 10 s)
    import wb.market.tape as tm
    reads, real = [], tm.clock_for
    monkeypatch.setattr(tm, "clock_for", lambda p, m: reads.append(m) or real(p, m))
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 10, 0, 5)
    tape.want("CN", sess(paths, 10, 0, 6)[1])
    tick(tape, paths, src, 10, 0, 16)
    tick(tape, paths, src, 10, 1, 5)
    assert src.calls.count("market") == 3 and reads == ["CN"]


def test_the_record_keeps_its_minute_while_the_poll_is_slow(paths):
    # 盯盘's poll on a long watch list takes a minute or more (10-12: 60 s, its rules on today's bar); the record runs in
    # a loop of its own, so the minute's row is never held up behind it
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], "CN:600519")
    src = FakeTF(paths)
    stuck, polled, returned, boards = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    def minutes(ids, day, indexes=()):
        polled.set()
        stuck.wait(60)
        returned.set()
        return pl.DataFrame(schema=MINUTES)
    src.minutes = minutes
    src.now = datetime(2026, 10, 1, 10, 0, 5, tzinfo=CST)
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=lambda e: e.get("type") == "board" and boards.set(),
              allow_demo=True, markets=("CN",))
    lv.tape.fill = False
    lv.start(every=0.01, clock=lambda: src.now)
    try:
        assert polled.wait(30) and boards.wait(30)                  # 盯盘 stuck in its poll; the record goes on
        assert not returned.is_set()
        assert [r["i"] for r in lv.tape.rows("CN", TODAY)] == [30]   # 10:00's row
    finally:
        lv.stop()
        stuck.set()


def test_a_restart_keeps_the_rows_and_events(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 10, 0, 5)
    tick(tape, paths, src, 10, 1, 5)
    before = tape.rows("CN", TODAY)
    again, _ = tape_for(paths)
    tick(again, paths, src, 10, 1, 30)                            # the same minute after a restart: refreshed, not added
    after = again.rows("CN", TODAY)
    assert [r["i"] for r in after] == [30, 31] and after[0] == before[0]
    f = again.facts("CN", TODAY)
    assert events(after, f.indexes.get("CN:IDX:000001"), f.top_ids, {}) == events(before, f.indexes.get("CN:IDX:000001"), f.top_ids, {})


def test_the_snapshot_file_holds_only_stocks_traded_today(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 10, 0, 5)
    snap = pl.read_parquet(paths.market_dir("CN") / "intraday" / f"snapshot={TODAY}.parquet")
    assert sorted(snap["listing_id"].to_list()) == sorted(IDS)
    assert snap.columns == ["listing_id", "at", "open", "high", "low", "last", "volume", "amount", "pre_close"]


def test_a_missing_yesterday_is_filled_from_minutes_outside_trading(paths):
    tape, _ = tape_for(paths, fill=True)
    src = FakeTF(paths)
    tick(tape, paths, src, 8, 30)                                 # before the open: fill the day before
    assert ("minutes", tuple(sorted(IDS)), YDAY) in src.calls
    rows = tape.rows("CN", YDAY)
    assert len(rows) == 240 and rows[0]["i"] == 1 and rows[-1]["i"] == 240
    assert rows[-1]["traded"] == 2 and rows[-1]["sh_last"] == pytest.approx(3800.0 * (1 + 0.0001 * 239))
    tick(tape, paths, src, 8, 31)
    assert sum(1 for c in src.calls if c[0] == "minutes") == 1    # once a day


def test_limits_found_in_the_session_are_confirmed_a_minute_later(paths):
    asked = []
    tape, _ = tape_for(paths, fetch_limits=lambda day: asked.append(day) or limits_of(paths))
    src = FakeTF(paths)
    tick(tape, paths, src, 10, 24, 26)                            # the app opened at 10:24: the list is long since out
    assert tape.facts("CN", TODAY).limits == "none"
    tick(tape, paths, src, 10, 25, 26)
    assert asked == [TODAY, TODAY] and tape.facts("CN", TODAY).limits == "today"


def test_yesterday_filled_before_the_open_is_paced_after_the_close_not(paths):
    # an app opened at 9:20 fills the day before at ten calls a minute: unpaced, its 56 calls would hold 盯盘's first
    # polls at the open behind the shared K-line limiter; after the close nothing waits on it
    for (h, m), paced in (((9, 20), [6.0]), ((16, 0), [])):
        slept = []
        tape, _ = tape_for(paths, batch=1, sleep=slept.append, fill=True)
        tick(tape, paths, FakeTF(paths), h, m)
        assert len(tape.rows("CN", YDAY)) == 240 and slept == paced, (h, m)
        (paths.market_dir("CN") / "tape" / f"date={YDAY}.parquet").unlink()


def test_a_gap_after_a_restart_is_filled(paths):
    tape, _ = tape_for(paths, fill=True)
    src = FakeTF(paths)
    tape._fill[("CN", YDAY)] = "done"                             # the day before is not this test's
    for hm in ((9, 0), (9, 1)):
        tick(tape, paths, src, *hm)                               # today's limits complete before the open
    for k in range(42):                                           # the app ran 9:31–10:12
        tick(tape, paths, src, 9 + (31 + k) // 60, (31 + k) % 60)
    before = tape.rows("CN", TODAY)
    assert [r["i"] for r in before] == list(range(1, 43))
    again, _ = tape_for(paths, fill=True, batch=1)                # restarted at 10:24
    again._fill[("CN", YDAY)] = "done"
    tick(again, paths, src, 10, 24, 26)
    rows = again.rows("CN", TODAY)
    assert [r["i"] for r in rows] == list(range(1, 55))           # 10:13–10:23 filled
    assert rows[:42] == before                                    # the recorded minutes stay as they were
    assert all(r["limit_up"] is not None for r in rows)


def test_a_fill_waits_for_todays_limits_and_redoes_the_rows_recorded_without_them(paths):
    slept = []
    tape, _ = tape_for(paths, batch=1, sleep=slept.append, fill=True)
    src = FakeTF(paths)
    tape._fill[("CN", YDAY)] = "done"
    tick(tape, paths, src, 10, 30, 5)                             # the app opened at 10:30: limits not yet confirmed
    assert not any(c[0] == "minutes" for c in src.calls)          # no fill yet: its rows would have no limit numbers
    assert [r["i"] for r in tape.rows("CN", TODAY)] == [60] and tape.rows("CN", TODAY)[0]["limit_up"] is None
    tick(tape, paths, src, 10, 31, 5)                             # confirmed: the morning is filled with them
    rows = tape.rows("CN", TODAY)
    assert [r["i"] for r in rows] == list(range(1, 62))
    assert all(r["limit_up"] is not None for r in rows)           # 10:30's row too, recorded before they were known
    assert slept == [6.0]                                         # two calls (batch of 1), ten a minute while trading


def test_without_limits_the_fill_goes_ahead_after_10_minutes(paths):
    def down(day):
        raise RuntimeError("Tushare 500")
    tape, _ = tape_for(paths, fetch_limits=down, fill=True)
    src = FakeTF(paths)
    tape._fill[("CN", YDAY)] = "done"
    tick(tape, paths, src, 10, 30, 5)
    tick(tape, paths, src, 10, 39, 5)
    assert not any(c[0] == "minutes" for c in src.calls)
    tick(tape, paths, src, 10, 40, 5)                             # the gap is still worth its other numbers
    rows = tape.rows("CN", TODAY)
    assert [r["i"] for r in rows] == list(range(1, 71)) and rows[0]["limit_up"] is None

# ---- a start at lunch or after the close (design 2026-10-12-market-page-redesign §5) --------------------------------------
def test_a_start_at_lunch_fetches_once_and_the_board_is_1130s(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 12, 10)                                  # the app opened at lunch: nothing in memory
    assert src.calls.count("market") == 1 and tape.latest("CN")["board"]["at"] == "2026-10-01T11:30:00"
    assert tape.rows("CN", TODAY) == []                              # no record row from it
    tick(tape, paths, src, 12, 10, 20)
    assert src.calls.count("market") == 1                            # not again: it has today's board


def test_a_late_fetch_that_fails_is_tried_again_a_minute_later(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    src.fail = True
    tick(tape, paths, src, 16, 30)
    tick(tape, paths, src, 16, 30, 30)
    assert src.calls.count("market") == 1
    src.fail = False
    tick(tape, paths, src, 16, 31, 5)
    assert src.calls.count("market") == 2 and tape.latest("CN")["board"]["at"] == "2026-10-01T15:00:00"


def test_no_late_fetch_on_a_closed_day_or_before_the_auction(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths, date(2026, 10, 3))
    tick(tape, paths, src, 12, 0, day=date(2026, 10, 3))             # a Saturday
    tick(tape, paths, src, 9, 10)                                     # before the auction
    assert src.calls.count("market") == 0


# F2 (review F, 10-13): the record's row 120 is the morning's close, kept in the lunch window; the afternoon's first
# fetch (13:00, the market page open) must not put 13:00's market in its place — on the user's real 10-12 record row 120
# reads 13:00:56 (10-09's, filled from 1-minute bars, reads 11:30), so the next day's 「昨天 11:30」 is yesterday's 13:01.
def test_the_mornings_closing_row_is_not_replaced_by_the_afternoons_first_fetch(paths):
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 11, 30, 5)
    tick(tape, paths, src, 11, 34, 55)                            # the lunch window: the morning's close, refreshed
    src.mult = 1.05                                               # the afternoon opens higher
    tape.want("CN", datetime(2026, 10, 1, 13, 0, 10, tzinfo=CST))  # the page open at 13:00
    tick(tape, paths, src, 13, 0, 20)
    tick(tape, paths, src, 13, 1, 5)
    rows = {r["i"]: r for r in tape.rows("CN", TODAY)}
    assert rows[120]["at"] == datetime(2026, 10, 1, 11, 34, 55)   # still the morning's close
    assert rows[121]["at"] == datetime(2026, 10, 1, 13, 1, 5)
    assert tape.latest("CN")["board"]["at"] == "2026-10-01T13:01:05"


# F4 (review F, 10-13): a whole-market answer without one trade of today (TickFlow answering `[]` in a blip) is not an
# empty market: the board, the day's snapshot (the K-line's forming bars) and the record keep what they had — on the
# outage replay of 10-09 six rows of zeros (traded 0, 涨停 unknown) went into the record and the page showed them fresh.
class Blip(FakeTF):
    empty = False

    def whole_market(self):
        return pl.DataFrame(schema=QUOTES) if self.empty else super().whole_market()


def test_an_answer_without_todays_trades_changes_nothing(paths):
    tape, _ = tape_for(paths)
    src = Blip(paths)
    tick(tape, paths, src, 10, 0, 5)
    good = tape.latest("CN")["board"]
    src.empty = True
    tick(tape, paths, src, 10, 1, 5)
    assert [r["i"] for r in tape.rows("CN", TODAY)] == [30]          # no row of zeros
    assert tape.latest("CN")["board"] == good and tape.latest("CN")["error"]
    snap = paths.market_dir("CN") / "intraday" / f"snapshot={TODAY}.parquet"
    assert pl.read_parquet(snap).height == 2                          # the K-line's forming bars are still there
    src.empty = False
    tick(tape, paths, src, 10, 2, 5)
    assert [r["i"] for r in tape.rows("CN", TODAY)] == [30, 32] and tape.latest("CN")["error"] is None


def test_a_price_beyond_todays_limits_is_left_out_and_counted(paths):
    # 10-13 09:25: twice the auction's price for 1,750 stocks — +96% industries, 1,750 limit-ups on the board, and the
    # same prices in the snapshot the K-line's forming bar reads
    tape, _ = tape_for(paths)
    src = FakeTF(paths)
    tick(tape, paths, src, 9, 5)                                  # today's limits fetched
    src.mult = 2.0                                                # high and last twice the close: beyond 1.1 × it
    tick(tape, paths, src, 9, 26)
    b = tape.latest("CN")["board"]
    assert (b["impossible"], b["traded"], b["limits"]["limit_up"]) == (2, 0, None)   # nothing traded: not counted
    assert tape.quote("CN").filter(pl.col("listing_id").is_in(list(IDS))).is_empty()
    snap = pl.read_parquet(paths.market_dir("CN") / "intraday" / f"snapshot={TODAY}.parquet")
    assert snap.filter(pl.col("listing_id").is_in(list(IDS))).is_empty()
    src.mult = 1.02
    tick(tape, paths, src, 10, 0, 5)
    b = tape.latest("CN")["board"]
    assert (b["impossible"], b["traded"]) == (0, 2)
