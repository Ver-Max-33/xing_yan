"""K 线 for any A share (design 2026-10-12 §9): a stock opened on the K-line page during the session is polled first for
10 minutes (at most 10 of them, never past what the source takes) and gets its minutes within a tick — only fetched,
never alerted; today's forming bar of any stock that has traded comes from the day's whole-market snapshot, before
the auction's match and when suspended there is none, on a closed day or once the close is published neither."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.jobs.live import Live
from wb.realtime.alerts import Alerts
from wb.realtime.providers.tushare_rt import SNAPSHOT
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.workspace.store import Workspace

CST = ZoneInfo("Asia/Shanghai")
AS_OF, DAY = date(2026, 9, 30), date(2026, 10, 1)
WATCHED, OTHER = "CN:600519", "CN:000001"


def at(h, m, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=CST)


class Fake:
    """Two minutes for each stock asked for (a crossing of 10.5 for OTHER), indexes too; `asked`: each call's ids."""
    name, delay, poll_seconds, approx_volume = "fake", 0, 60, False

    def __init__(self, max_listings=297):
        self.asked, self.max_listings = [], max_listings

    def minutes(self, ids, day, indexes=()):
        self.asked.append(list(ids))
        rows = [(lid, datetime(day.year, day.month, day.day, 9, 30 + k), 10.0, 11.0, 9.0, 10.0 + k, 3000.0)
                for lid in [*ids, *indexes] for k in (0, 1)]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


@pytest.fixture
def live(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    ahead = [AS_OF + timedelta(days=k) for k in range(1, 12)]
    st.publish({"calendar": pl.DataFrame({"date": ahead, "is_open": [d.weekday() < 5 for d in ahead], "source": ["f"] * len(ahead)})}, as_of=AS_OF)
    ws = Workspace(paths)
    ws.add_item(ws.groups()[0]["id"], WATCHED)
    src = Fake()
    events = []
    lv = Live(lambda m: paths, source_for=lambda p, m: src, publish=events.append, allow_demo=True, markets=("CN",))
    lv.tape.fill = False
    return lv, src, events, paths


def snapshot(paths, rows):
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=SNAPSHOT, orient="row").write_parquet(folder / f"snapshot={DAY}.parquet")


def test_a_viewed_stock_is_polled_first_and_drops_out_after_10_minutes(live):
    lv, src, _, _ = live
    src.max_listings = 1                                           # the followed list alone fills the source
    lv.view(OTHER, at(10, 0))
    lv.tick(at(10, 0, 5))
    assert src.asked[-1] == [OTHER]                                # yours waits a poll; the one you look at comes first
    lv.tick(at(10, 11))
    assert src.asked[-1] == [WATCHED]                              # ten minutes on: it drops out


def test_at_most_10_viewed_stocks_and_never_more_than_the_source_takes(live):
    lv, src, _, _ = live
    ids = [f"CN:{600100 + k}" for k in range(12)]
    for k, lid in enumerate(ids):
        lv.view(lid, at(10, 0, k))
    assert lv._viewed("CN", at(10, 1)) == list(reversed(ids))[:10]    # the latest first
    src.max_listings = 5
    n = len(src.asked)
    lv.tick(at(10, 1))
    assert src.asked[n] == list(reversed(ids))[:5]                 # the poll: what the source takes, the latest first
    assert src.asked[n + 1:] == [list(reversed(ids))[5:10]]        # the rest looked at: their minutes once, in one call


def test_a_viewed_stock_never_alerts(live):
    lv, src, events, paths = live
    Workspace(paths).save_drawing(OTHER, "horizontalStraightLine", [{"date": "2026-09-30", "price": 10.5}])   # 提醒 not ticked
    lv.view(OTHER, at(10, 0))
    lv.tick(at(10, 0, 5))
    assert OTHER in src.asked[-1]
    rows = next(e for e in events if e["type"] == "quotes")["rows"]
    assert OTHER not in {r["listing_id"] for r in rows}            # not one of yours: not in 盯盘's list
    assert [a for a in Alerts(paths).day(DAY, "CN") if a["listing_id"] == OTHER] == []


def test_a_viewed_stock_gets_its_minutes_within_one_tick_once_a_day(live):
    lv, src, events, paths = live
    lv.tick(at(10, 0, 0))                                          # the poll: yours
    assert src.asked == [[WATCHED]]
    lv.view(OTHER, at(10, 0, 10))
    lv.tick(at(10, 0, 13))                                         # the poll is not due for 47 s: fetched alone now
    assert src.asked[-1] == [OTHER]
    kept = pl.read_parquet(paths.market_dir("CN") / "intraday" / f"date={DAY}.parquet")
    assert kept.filter(pl.col("listing_id") == OTHER).height == 2
    assert kept.filter(pl.col("listing_id") == WATCHED).height == 2   # the merge kept yours
    assert events[-1]["type"] == "quotes"                          # the page hears it
    lv.tick(at(10, 0, 16))
    assert src.asked.count([OTHER]) == 1                           # once a day


def last_close(paths, lid):
    bars = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == lid).sort("date")
    return bars["close"][-1], bars["adj_factor"][-1]


def test_the_forming_bar_of_any_stock_comes_from_the_days_snapshot(live):
    lv, _, _, paths = live
    prev = last_close(paths, OTHER)[0]
    snapshot(paths, [(OTHER, datetime(2026, 10, 1, 10, 0, 3), 11.1, 11.4, 11.0, 11.3, 2.5e6, 2.8e7, prev),
                     (WATCHED, datetime(2026, 10, 1, 10, 0, 3), 1500.0, 1520.0, 1490.0, 1510.0, 1e5, 1.5e8, 1495.0)])
    bar = lv.temp_day(OTHER, at(10, 0, 30))
    assert bar == {"date": DAY, "open": 11.1, "high": 11.4, "low": 11.0, "close": 11.3, "volume": 2.5e6, "amount": 2.8e7,
                   "pre_close": prev, "adj_factor": None, "share_factor": None}   # an ordinary day: yesterday's factors
    lv.tick(at(10, 0, 31))                                         # yours has minutes too: the snapshot still wins
    assert lv.temp_day(WATCHED, at(10, 0, 40))["close"] == 1510.0


def test_no_forming_bar_before_the_auction_match_or_when_suspended(live):
    lv, _, _, paths = live
    assert lv.temp_day(OTHER, at(9, 20)) is None                   # nothing yet today
    snapshot(paths, [(WATCHED, datetime(2026, 10, 1, 9, 25), 1500.0, 1500.0, 1500.0, 1500.0, 1e4, 1.5e7, 1495.0)])
    assert lv.temp_day(OTHER, at(9, 26)) is None                   # the snapshot holds only stocks that traded
    assert lv.temp_day(WATCHED, at(9, 26))["open"] == 1500.0        # the auction's match: the bar starts


def test_forming_bar_survives_a_restart(live):
    lv, src, _, paths = live
    snapshot(paths, [(OTHER, datetime(2026, 10, 1, 10, 0, 3), 11.1, 11.4, 11.0, 11.3, 2.5e6, 2.8e7, 11.0)])
    again = Live(lambda m: paths, source_for=lambda p, m: src, allow_demo=True, markets=("CN",))
    assert again.temp_day(OTHER, at(10, 5))["close"] == 11.3


def test_no_forming_bar_on_a_closed_day_or_after_the_close_is_published(live):
    lv, _, _, paths = live
    snapshot(paths, [(OTHER, datetime(2026, 10, 1, 15, 0, 2), 11.1, 11.4, 11.0, 11.3, 2.5e6, 2.8e7, 11.0)])
    assert lv.temp_day(OTHER, at(15, 30))["close"] == 11.3          # after the close, before the day is in: the day's last
    assert lv.temp_day(OTHER, datetime(2026, 10, 3, 10, 0, tzinfo=CST)) is None   # a Saturday
    st = Store(paths, "CN")
    last = st.read("bars_1d").filter(pl.col("listing_id") == OTHER).sort("date").tail(1)
    st.publish({"bars_1d": last.with_columns(pl.lit(DAY).alias("date"))}, as_of=DAY)
    assert lv.temp_day(OTHER, at(17, 0)) is None                   # the close is in: the bar is the real one


# ---- the minute K-line's minutes (10-12) ----------------------------------------------------------------------------------
class Ranged(Fake):
    """A source that gives a stock's days of minutes in one call (TickFlow's minutes_between), each day two minutes."""
    def __init__(self):
        super().__init__()
        self.ranges = []

    def minutes_between(self, ids, first, last):
        self.ranges.append((list(ids), first, last))
        days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
        rows = [(lid, datetime(d.year, d.month, d.day, 9, 30 + k), 10.0, 11.0, 9.0, 10.0 + k, 3000.0) for lid in ids for d in days
                if d.weekday() < 5 for k in (0, 1)]
        return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_a_minute_k_lines_days_are_the_kept_ones_and_those_missing_come_in_one_call_once(live):
    lv, _, _, paths = live
    src = Ranged()
    lv.source_for = lambda p, m: src
    got = lv.minutes_of(OTHER, 3, at(10, 0, 30))            # 9-29, 9-30 and today, 10-01 (the calendar's last three)
    assert len(src.ranges) == 1 and src.ranges[0][0] == [OTHER]
    assert sorted(set(got["at"].dt.date().to_list()))[:2] == [date(2026, 9, 29), date(2026, 9, 30)]
    again = lv.minutes_of(OTHER, 3, at(10, 1))
    assert len(src.ranges) == 1 and again.height == got.height   # kept: never asked again


def test_a_past_day_kept_only_in_part_comes_whole_once(live):
    """盯盘 stopped at 13:30 (the app closed, 10-12): that day's minutes end at 13:29, kept mid-minute (3,800 shares of
    87,900). A minute K-line asks for the whole day once and keeps it — not a day cut short on a partial bar."""
    lv, _, _, paths = live

    def day(upto):   # 9-30's minutes from 9:30, before `upto`, 5,000 shares each
        ats = [datetime(2026, 9, 30, h, m) + timedelta(minutes=k) for h, m in ((9, 30), (13, 0)) for k in range(120)]
        return pl.DataFrame([(OTHER, a, 10.0, 10.0, 10.0, 10.0, 5000.0) for a in ats if a < upto], schema=MINUTES, orient="row")
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    cut = datetime(2026, 9, 30, 13, 29)
    day(datetime(2026, 9, 30, 13, 30)).with_columns(pl.when(pl.col("at") == cut).then(300.0).otherwise(pl.col("volume")).alias("volume")) \
        .write_parquet(folder / f"date={AS_OF}.parquet")
    src = Ranged()
    src.minutes_between = lambda ids, first, last: (src.ranges.append((list(ids), first, last)), day(datetime(2026, 9, 30, 15, 0)))[1]
    lv.source_for = lambda p, m: src
    got = lv.minutes_of(OTHER, 2, at(10, 0, 30)).filter(pl.col("at").dt.date() == AS_OF)   # 9-30 and today
    assert [r[1:] for r in src.ranges] == [(AS_OF, AS_OF)]
    assert got.height == 240 and got["at"].max() == datetime(2026, 9, 30, 14, 59) and got.filter(pl.col("at") == cut)["volume"][0] == 5000.0
    lv.minutes_of(OTHER, 2, at(10, 1))
    assert len(src.ranges) == 1   # whole now: kept


def test_once_the_day_is_over_its_minutes_come_whole_for_a_stock_first_opened_then(live):
    """In the session today's minutes are the poll's (盯盘's stocks, those looked at); after the close the poll stops — a
    stock first opened in the evening gets today's whole day once, not a chart without today."""
    lv, _, _, paths = live
    src = Ranged()
    lv.source_for = lambda p, m: src
    lv.minutes_of(OTHER, 1, at(10, 0))   # trading: the poll's
    assert src.ranges == []
    got = lv.minutes_of(OTHER, 1, at(16, 0))   # an hour after the close
    assert [r[1:] for r in src.ranges] == [(DAY, DAY)] and got["at"].dt.date().unique().to_list() == [DAY]
    lv.minutes_of(OTHER, 1, at(16, 1))
    assert len(src.ranges) == 1   # once


def test_a_minute_k_line_on_demo_data_never_asks_a_real_source(paths):
    """As the poll: the app never sends `wb devdata`'s market to a real source — its minute K-line shows the days kept."""
    write(paths, 320)
    src = Ranged()
    lv = Live(lambda m: paths, source_for=lambda p, m: src, markets=("CN",))
    lv.minutes_of(OTHER, 3, at(10, 0, 30))
    assert src.ranges == []


def test_the_minute_k_lines_last_minute_moves_with_the_whole_market_quote(live):
    lv, _, _, paths = live
    lv.source_for = lambda p, m: Ranged()
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(OTHER, datetime(2026, 10, 1, 9, 30), 11.0, 11.1, 10.9, 11.0, 5000.0)], schema=MINUTES, orient="row").write_parquet(
        folder / f"date={DAY}.parquet")
    snapshot(paths, [(OTHER, datetime(2026, 10, 1, 9, 31, 40), 11.0, 11.4, 10.9, 11.35, 2.5e6, 2.8e7, 11.0)])
    m = lv.minutes_of(OTHER, 1, at(9, 31, 50)).filter(pl.col("at").dt.date() == DAY).sort("at")
    assert m["at"].to_list() == [datetime(2026, 10, 1, 9, 30), datetime(2026, 10, 1, 9, 31)]   # the quote's minute: 9:31
    assert m["close"][-1] == 11.35                                                              # its price, between polls


def test_on_an_ex_date_the_forming_bar_carries_todays_factor_from_the_quotes_close_before(live):
    """Review 10-13: the day's corporate action comes with the evening's update, so in the session the exchange's 昨收 in
    the whole-market quote (its reference price after a 送转 / dividend) is what says the factor changed."""
    lv, _, _, paths = live
    prev, adj = last_close(paths, OTHER)
    snapshot(paths, [(OTHER, datetime(2026, 10, 1, 10, 0, 3), 9.0, 9.2, 8.9, 9.1, 2.5e6, 2.3e7, prev * 0.9)])  # a 10% 派息
    bar = lv.temp_day(OTHER, at(10, 0, 30))
    assert bar["pre_close"] == pytest.approx(prev * 0.9) and bar["adj_factor"] == pytest.approx(adj / 0.9)



# ---- past days' minutes fetched ahead (10-13, you chose) -----------------------------------------------------------------
def prefetch_until(lv, src, start, turns=40):
    for k in range(turns):
        lv.prefetch_tick(start + timedelta(seconds=10 * k))
    return src.ranges


def test_after_the_close_the_whole_markets_past_minutes_are_fetched_ahead(live, monkeypatch):
    """You chose (10-13): the minute charts' history is there before a chart is opened — after the close (or before the
    open) the whole market's minutes of the last trading days, newest day first, a batch of stocks a call; never while
    the market trades (盯盘's budget)."""
    import wb.jobs.live as live_mod
    lv, _, _, paths = live
    src = Ranged()
    lv.source_for = lambda p, m: src
    monkeypatch.setattr(live_mod, "PREFETCH_BATCH", 2)
    monkeypatch.setattr(live_mod, "PREFETCH_DAYS", 1)                  # a day a call: each call's day is the window's
    traded = set(Store(paths, "CN").read("bars_1d").filter((pl.col("date") == AS_OF) & (pl.col("volume") > 0))["listing_id"])
    lv.prefetch_tick(at(10, 0))
    assert src.ranges == []                                             # trading: nothing
    asked = prefetch_until(lv, src, at(15, 30))
    assert all(len(ids) <= 2 for ids, *_ in asked) and asked[0][1:] == (AS_OF, AS_OF)
    assert traded <= {x for ids, first, _ in asked if first == AS_OF for x in ids}
    kept = pl.read_parquet(paths.market_dir("CN") / "intraday" / f"date={AS_OF}.parquet")
    assert traded <= set(kept["listing_id"]) and asked[-1][1] < AS_OF   # then the days before


def test_fetching_ahead_goes_back_the_kept_days_only_and_can_be_turned_off(live, monkeypatch):
    import wb.jobs.live as live_mod
    lv, _, _, paths = live
    src = Ranged()
    lv.source_for = lambda p, m: src
    monkeypatch.setattr(live_mod, "KEEP_DAYS", 2)
    monkeypatch.setattr(live_mod, "PREFETCH_DAYS", 1)
    days = {first for _, first, _ in prefetch_until(lv, src, at(15, 30), turns=60)}
    assert days == {date(2026, 9, 29), AS_OF}
    src2 = Ranged()
    lv.source_for = lambda p, m: src2
    Workspace(paths).set_setting("minutes.prefetch", False)
    for d in (date(2026, 9, 29), AS_OF):
        (paths.market_dir("CN") / "intraday" / f"date={d}.parquet").unlink()
    assert prefetch_until(lv, src2, at(16, 30)) == []


def test_a_long_history_comes_in_parts_the_source_can_give(live):
    """≤ 5,000 bars a call (about 20 days of 1-minute bars): a stock's 40 days missing come in parts of at most 15."""
    lv, _, _, paths = live
    src = Ranged()
    lv.source_for = lambda p, m: src
    lv.minutes_of(OTHER, 40, at(16, 0))
    open_days = Store(paths, "CN").read("calendar").filter(pl.col("is_open"))["date"].to_list()
    spans = [sum(first <= d <= last for d in open_days) for _, first, last in src.ranges]
    assert len(spans) >= 3 and max(spans) <= 15 and sum(spans) >= 40


def test_a_failing_source_is_asked_again_ten_minutes_later_not_every_turn(live):
    """The fetch-ahead runs all evening: a source refusing (its limit, an outage) is tried again 10 minutes later — its
    batch asked again then — not every 3 seconds, a log line each."""
    lv, _, _, paths = live

    class Down(Ranged):
        def minutes_between(self, ids, first, last):
            self.ranges.append((list(ids), first, last))
            raise RuntimeError("TickFlow 503")
    src = Down()
    lv.source_for = lambda p, m: src
    for k in range(20):
        lv.prefetch_tick(at(15, 30) + timedelta(seconds=10 * k))
    assert len(src.ranges) == 1
    lv.prefetch_tick(at(15, 41))
    assert len(src.ranges) == 2 and src.ranges[1] == src.ranges[0]   # the same batch, asked again
