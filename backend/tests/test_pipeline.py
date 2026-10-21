from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from wb import events
from wb.data.pipeline import DataDelayed, Pipeline
from wb.data.providers.base import Probe, Provider, Unavailable

MON = date(2026, 9, 28)
DAYS = [MON + timedelta(days=i) for i in range(5)]  # Mon..Fri, all open


class FakeJP(Provider):
    """Stands in for jquants: calendar, instruments, bars by date (with optional split events)."""
    name = "jquants"
    market = "JP"
    complete_list = True
    unit_kinds = {"calendar": "all", "instruments": "all", "bars_1d": "date"}

    def __init__(self, fail_at: date | None = None, splits: dict | None = None, closes: dict | None = None):
        self.calls = []
        self.fail_at, self.splits, self.closes = fail_at, splits or {}, closes or {}

    def fetch(self, dataset, unit, start, end):
        self.calls.append((dataset, unit))
        if dataset == "calendar":
            days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            return {"calendar": pl.DataFrame({"date": days, "is_open": [d.weekday() < 5 for d in days],
                                              "source": "fake"})}
        if dataset == "instruments":
            return {"instruments": pl.DataFrame({"listing_id": ["JP:7203", "JP:130A"], "market": "JP",
                                                 "code": ["7203", "130A"], "status": "L", "source": "jquants"})}
        d = date.fromisoformat(unit)
        if d == self.fail_at:
            self.fail_at = None
            raise ConnectionError("network down")
        rows = [("JP:7203", 1000.0), ("JP:130A", 500.0), ("JP:9999", 10.0)]  # 9999 delisted later
        close = {lid: self.closes.get((lid, d), c) for lid, c in rows}
        bars = pl.DataFrame({"listing_id": [r[0] for r in rows], "date": d,
                             "open": list(close.values()), "high": list(close.values()),
                             "low": list(close.values()), "close": list(close.values()),
                             "volume": 100.0, "suspended": False, "source": "jquants"})
        acts = [(lid, r) for (lid, ex), r in self.splits.items() if ex == d]
        actions = pl.DataFrame({"listing_id": [a[0] for a in acts], "ex_date": [d] * len(acts),
                                "kind": "split", "ratio": [a[1] for a in acts], "source": "jquants"},
                               schema={"listing_id": pl.Utf8, "ex_date": pl.Date, "kind": pl.Utf8,
                                       "ratio": pl.Float64, "source": pl.Utf8})
        return {"bars_1d": bars, "corp_actions": actions}

    def probe(self):
        return [Probe("bars_1d", True)]

    def ready_count(self, day, instruments):
        return 3, 3


class FakeTopix(Provider):
    """Stands in for JPX's TOPIX list (the current list only), recording the window it is asked for."""
    name = "jpx_topix"
    market = "JP"
    unit_kinds = {"index_members": "all"}

    def __init__(self, down: bool = False):
        self.windows, self.down = [], down

    def fetch(self, dataset, unit, start, end):
        if self.down:
            raise Unavailable("not on this plan")
        self.windows.append((start, end))
        return {"index_members": pl.DataFrame({"index_id": ["JP:IDX:TOPIX"], "date": [end], "listing_id": ["JP:7203"],
                                               "weight": [1.0], "segment": ["TOPIX Core30"], "source": ["fake"]})}

    def probe(self):
        return [Probe("index_members", True)]


AFTER_CLOSE = datetime(2026, 10, 9, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # after every test day's session


def pipe(paths, prov, clock=lambda: AFTER_CLOSE, topix=None):
    paths = type(paths)(paths.root, "prod")
    return Pipeline(paths, "JP", providers={"jquants": prov, "jpx_topix": topix or FakeTopix()}, log=lambda m: None,
                    clock=clock)


def test_backfill_publishes_everything(paths):
    p = pipe(paths, FakeJP())
    snap = p.backfill(DAYS[0], DAYS[-1])
    assert snap.as_of == DAYS[-1]
    bars = p.store.read("bars_1d")
    assert bars.height == 15 and set(bars["adj_factor"]) == {1.0}
    inst = p.store.read("instruments")
    # 9999 appears in bars but not in the (complete) current list -> retained as delisted
    assert inst.filter(pl.col("listing_id") == "JP:9999")["status"][0] == "D"


def test_interrupted_backfill_resumes_without_refetch(paths):
    prov = FakeJP(fail_at=DAYS[2])
    p = pipe(paths, prov)
    with pytest.raises(ConnectionError):
        p.backfill(DAYS[0], DAYS[-1])
    prov.calls.clear()
    p2 = pipe(paths, prov)
    p2.backfill(DAYS[0], DAYS[-1])
    fetched_days = [u for ds, u in prov.calls if ds == "bars_1d"]
    assert fetched_days == [d.isoformat() for d in DAYS[2:]]
    assert p2.store.read("bars_1d").height == 15  # no duplicates


class SideBySide(FakeJP):
    """A source that may be asked several days at once (Tushare: per-minute quotas, not one call at a time)."""
    workers = 3

    def __init__(self, **kw):
        super().__init__(**kw)
        import threading
        self.lock, self.now, self.most = threading.Lock(), 0, 0

    def fetch(self, dataset, unit, start, end):
        import time
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
        try:
            time.sleep(0.05)
            return super().fetch(dataset, unit, start, end)
        finally:
            with self.lock:
                self.now -= 1


def test_a_source_with_workers_fetches_days_side_by_side(paths):
    prov = SideBySide()
    p = pipe(paths, prov)
    p.backfill(DAYS[0], DAYS[-1])
    assert prov.most == 3                                   # three days at once, never more
    assert p.store.read("bars_1d").height == 15


def test_side_by_side_a_failure_keeps_the_days_before_it_and_resumes_there(paths):
    # as one at a time: the days before the failed one are kept, the rest is fetched on the next run
    prov = SideBySide(fail_at=DAYS[2])
    p = pipe(paths, prov)
    with pytest.raises(ConnectionError):
        p.backfill(DAYS[0], DAYS[-1])
    prov.calls.clear()
    p2 = pipe(paths, prov)
    p2.backfill(DAYS[0], DAYS[-1])
    assert sorted(u for ds, u in prov.calls if ds == "bars_1d") == [d.isoformat() for d in DAYS[2:]]
    assert p2.store.read("bars_1d").height == 15


def test_split_arriving_later_recomputes_history(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[2])
    prov = FakeJP(splits={("JP:7203", DAYS[3]): 2.0}, closes={("JP:7203", DAYS[3]): 500.0})
    p2 = pipe(paths, prov)
    p2.update(DAYS[3], wait=False)
    t = p2.store.read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date")
    assert t["adj_factor"].to_list() == [1.0, 1.0, 1.0, 2.0]
    hfq = (t["close"] * t["adj_factor"]).to_list()
    assert hfq == [1000.0, 1000.0, 1000.0, 1000.0]


def test_update_skips_non_trading_day(paths):
    p = pipe(paths, FakeJP())
    p.update(date(2026, 10, 3), wait=False)  # Saturday
    assert p.store.read("bars_1d").height == 0


def test_update_refuses_to_publish_without_bars(paths):
    class Empty(FakeJP):
        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            if dataset == "bars_1d":
                out["bars_1d"] = out["bars_1d"].head(0)
            return out

    seen = []
    events.clear()
    events.on("data.delayed")(seen.append)
    p = pipe(paths, Empty())
    with pytest.raises(DataDelayed):
        p.update(DAYS[0], wait=False)
    assert seen and seen[0]["date"] == DAYS[0]
    assert p.store.read("bars_1d").height == 0
    events.clear()


def test_update_waits_for_readiness(paths):
    class Slow(FakeJP):
        def __init__(self):
            super().__init__()
            self.polls = iter([(1, 3), (3, 3), (3, 3)])

        def ready_count(self, day, instruments):
            return next(self.polls)

    clock = {"t": datetime(2026, 9, 28, 16, 0, tzinfo=ZoneInfo("Asia/Tokyo"))}

    def sleep(s):
        clock["t"] += timedelta(seconds=s)

    p = pipe(paths, Slow(), clock=lambda: clock["t"])
    snap = p.update(DAYS[0], wait=True, sleep=sleep)
    assert snap.as_of == DAYS[0]
    assert clock["t"].strftime("%H:%M") == "16:42"   # :30 one of three, :40 all there, settled two minutes on


def test_refetch_publishes_only_corrections(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])
    assert pipe(paths, FakeJP()).refetch(DAYS[1]) is None
    seen = []
    events.clear()
    events.on("data.corrected")(seen.append)
    snap = pipe(paths, FakeJP(closes={("JP:130A", DAYS[1]): 505.0})).refetch(DAYS[1])
    assert snap is not None and "订正" in snap.note and seen[0]["rows"] == 1
    events.clear()


def test_unavailable_dataset_is_skipped(paths):
    class NoBars(FakeJP):
        def fetch(self, dataset, unit, start, end):
            if dataset == "bars_1d":
                raise type("Denied", (Unavailable,), {})("outside subscription")
            return super().fetch(dataset, unit, start, end)

    p = pipe(paths, NoBars())
    p.backfill(DAYS[0], DAYS[-1])
    assert "bars_1d" in p.unavailable
    assert p.store.read("instruments").height == 2


def test_refetch_refuses_to_discard_pending_backfill(paths):
    prov = FakeJP(fail_at=DAYS[2])
    with pytest.raises(ConnectionError):
        pipe(paths, prov).backfill(DAYS[0], DAYS[-1])
    with pytest.raises(RuntimeError, match="not published"):
        pipe(paths, FakeJP()).refetch(DAYS[0])


def test_backfill_as_of_is_last_day_with_bars(paths):
    class NotYet(FakeJP):
        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            if dataset == "bars_1d" and unit == DAYS[-1].isoformat():  # today's data not published yet
                out["bars_1d"] = out["bars_1d"].head(0)
                out["corp_actions"] = out["corp_actions"].head(0)
            return out

    snap = pipe(paths, NotYet()).backfill(DAYS[0], DAYS[-1])
    assert snap.as_of == DAYS[-2]


def test_backfill_during_trading_hours_excludes_today(paths):
    # 14:00 JST on DAYS[-1]: the session is still running, so today's bar is not final yet.
    p = pipe(paths, FakeJP(), clock=lambda: datetime(2026, 10, 2, 14, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    snap = p.backfill(DAYS[0], DAYS[-1])
    assert snap.as_of == DAYS[-2]
    assert p.store.read("bars_1d")["date"].max() == DAYS[-2]


def test_update_without_wait_refuses_an_unfinished_session(paths):
    p = pipe(paths, FakeJP(), clock=lambda: datetime(2026, 10, 2, 14, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    with pytest.raises(DataDelayed, match="not finished"):
        p.update(DAYS[-1], wait=False)


def test_instrument_names_are_nfkc_normalised(paths):
    # JPX writes Latin letters full-width ("Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ"); search and display want one form.
    class Named(FakeJP):
        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            if dataset == "instruments":
                out["instruments"] = out["instruments"].with_columns(
                    pl.Series("name", ["トヨタ自動車", "Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ"]),
                    pl.Series("name_en", ["TOYOTA MOTOR", None], pl.Utf8))
            return out

    p = pipe(paths, Named())
    p.backfill(DAYS[0], DAYS[1])
    names = dict(p.store.read("instruments").select("listing_id", "name").iter_rows())
    assert names["JP:130A"] == "Veritas In Silico" and names["JP:7203"] == "トヨタ自動車"


def test_a_delisted_listing_gets_its_name_from_the_source_once(paths):
    # J-Quants lists only today's issues: one the bars name that no current list has is a placeholder until the source
    # names it as it was listed on its last day; a failure there keeps the placeholder and publishes all the same
    class Describes(FakeJP):
        def __init__(self, fail=False):
            super().__init__()
            self.asked, self.fail = [], fail

        def describe(self, last):
            self.asked.append(dict(last))
            if self.fail:
                raise ConnectionError("network down")
            return pl.DataFrame({"listing_id": ["JP:9999"], "market": "JP", "code": ["9999"], "name": ["ニッセン"],
                                 "board": ["東証一部"], "status": "D", "source": "jquants"})

    def row(p):
        return p.store.read("instruments").filter(pl.col("listing_id") == "JP:9999").row(0, named=True)

    p = pipe(paths, Describes(fail=True))
    assert p.backfill(DAYS[0], DAYS[1]) is not None
    assert row(p)["source"] == "placeholder" and row(p)["status"] == "D"
    prov = Describes()
    p = pipe(paths, prov)
    p.update(DAYS[2])
    assert prov.asked == [{"JP:9999": DAYS[2]}]   # its last bar so far
    assert (row(p)["name"], row(p)["board"], row(p)["status"], row(p)["source"]) == ("ニッセン", "東証一部", "D", "jquants")
    p.update(DAYS[3])
    assert len(prov.asked) == 1   # named: not asked again


def _issues(p, snap):
    return {(r[0], r[1], r[2]) for r in p.store.conn.execute(
        "SELECT check_name, listing_id, date FROM quality_issue WHERE snapshot_id=?", (snap.id,))}


def test_update_after_an_empty_answer_fetches_the_day_again(paths):
    # A source hiccup (or running a few minutes early) must not stick: the next run asks again.
    class Late(FakeJP):
        answers = 0

        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            if dataset == "bars_1d":
                Late.answers += 1
                if Late.answers == 1:
                    out["bars_1d"] = out["bars_1d"].head(0)
            return out

    with pytest.raises(DataDelayed):
        pipe(paths, Late()).update(DAYS[0], wait=False)
    snap = pipe(paths, Late()).update(DAYS[0], wait=False)
    assert snap.as_of == DAYS[0] and Late.answers == 2


def test_update_catches_up_days_missed_since_the_last_snapshot(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[1])
    snap = pipe(paths, FakeJP()).update(DAYS[3], wait=False)  # the run for DAYS[2] never happened
    assert snap.as_of == DAYS[3]
    assert sorted(set(p.store.read("bars_1d")["date"])) == DAYS[:4]


def _no_sleep(seconds):
    raise AssertionError(f"waited {seconds:.0f}s")


def test_update_for_a_past_day_does_not_wait_for_the_window(paths):
    # 09:00 the next morning: yesterday's data is final, there is nothing to wait for
    p = pipe(paths, FakeJP(), clock=lambda: datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    assert p.update(DAYS[-2], wait=True, sleep=_no_sleep).as_of == DAYS[-2]


def test_update_after_the_window_still_takes_ready_data(paths):
    # 21:00, after the polling window: the data is there, so take it instead of reporting a delay
    p = pipe(paths, FakeJP(), clock=lambda: datetime(2026, 10, 2, 21, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    assert p.update(DAYS[-1], wait=True, sleep=_no_sleep).as_of == DAYS[-1]


def test_update_refuses_a_day_the_calendar_does_not_cover(paths):
    # unknown is not closed: past the calendar's last day nothing may be skipped silently
    class ShortCalendar(FakeJP):
        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            if dataset == "calendar":
                out["calendar"] = out["calendar"].filter(pl.col("date") < DAYS[-1])
            return out

    with pytest.raises(RuntimeError, match=f"calendar has no entry for {DAYS[-1]} .*it ends at {DAYS[-2]}"):
        pipe(paths, ShortCalendar()).update(DAYS[-1], wait=False)


def test_daily_update_checks_against_the_previous_published_day(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[0])
    # +80% with no corporate action: same-source checks need yesterday's row, which is published
    snap = pipe(paths, FakeJP(closes={("JP:130A", DAYS[1]): 900.0})).update(DAYS[1], wait=False)
    assert ("adj_jump", "JP:130A", DAYS[1].isoformat()) in _issues(p, snap)


def test_the_days_bar_is_checked_against_the_minutes_kept_that_day(paths):
    # 盘中 kept a minute source's minutes with their turnover (TickFlow's): the day's published bar is their witness
    from datetime import datetime as dt
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[0])
    folder = p.paths.market_dir("JP") / "intraday"
    folder.mkdir(parents=True)
    d = DAYS[1]
    pl.DataFrame({"listing_id": "JP:7203", "at": [dt(d.year, d.month, d.day, 9, 0), dt(d.year, d.month, d.day, 15, 29)],
                  "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "volume": [100.0, 100.0],
                  "amount": [100_000.0, 100_000.0]}).write_parquet(folder / f"date={d}.parquet")
    snap = pipe(paths, FakeJP()).update(d, wait=False)
    assert ("intraday", "JP:7203", d.isoformat()) in _issues(p, snap)                 # 200 shares against the bar's 100


def test_corrected_split_ratio_recomputes_history(paths):
    p = pipe(paths, FakeJP(splits={("JP:7203", DAYS[2]): 2.0}, closes={("JP:7203", d): 200.0 for d in DAYS[2:]}))
    p.backfill(DAYS[0], DAYS[-1])
    # the source corrects the event to 1:5; the earlier history must follow
    fixed = FakeJP(splits={("JP:7203", DAYS[2]): 5.0}, closes={("JP:7203", d): 200.0 for d in DAYS[2:]})
    pipe(paths, fixed).refetch(DAYS[2])
    bars = p.store.read("bars_1d").filter(pl.col("listing_id") == "JP:7203").sort("date")
    assert (bars["close"] * bars["adj_factor"]).to_list() == [1000.0] * 5


def test_refetch_corrects_other_daily_datasets_too(paths):
    class WithValuation(FakeJP):
        unit_kinds = {**FakeJP.unit_kinds, "valuation": "date"}

        def __init__(self, pe=10.0, **kw):
            super().__init__(**kw)
            self.pe = pe

        def fetch(self, dataset, unit, start, end):
            if dataset == "valuation":
                return {"valuation": pl.DataFrame({"listing_id": ["JP:7203"], "date": [date.fromisoformat(unit)],
                                                   "pe": [self.pe], "source": ["jquants"]})}
            return super().fetch(dataset, unit, start, end)

    p = pipe(paths, WithValuation())
    p.backfill(DAYS[0], DAYS[-1])
    snap = pipe(paths, WithValuation(pe=12.5)).refetch(DAYS[1])
    assert snap is not None
    v = p.store.read("valuation").filter(pl.col("date") == DAYS[1])
    assert v["pe"].to_list() == [12.5]


def test_cn_share_factor_is_unknown_when_dividends_are_not_routed(paths):
    from wb.config import Paths
    from wb.data.capability import save_probes
    from wb.db import connect

    class FakeCN(FakeJP):
        name, market = "tushare", "CN"

        def fetch(self, dataset, unit, start, end):
            out = super().fetch(dataset, unit, start, end)
            for k, df in out.items():
                if "listing_id" in df.columns:
                    out[k] = df.with_columns(pl.col("listing_id").str.replace("JP:7203", "CN:600519")
                                             .str.replace("JP:130A", "CN:000001").str.replace("JP:9999", "CN:600000"))
            out.pop("corp_actions", None)
            return out

    prod = Paths(paths.root, "prod")
    save_probes(connect(prod.app_db, "app"), "tushare", "CN", [Probe("corp_actions", False)])
    p = Pipeline(prod, "CN", providers={"tushare": FakeCN()}, log=lambda m: None, clock=lambda: AFTER_CLOSE)
    p.backfill(DAYS[0], DAYS[-1])
    assert p.store.read("bars_1d")["share_factor"].null_count() == 15


def test_fetch_stays_inside_the_plans_window(paths):
    # the J-Quants free plan ends 12 weeks back: days after its latest date are not asked for (asking
    # would only produce a misleading "not available on this plan")
    prov = FakeJP()
    p = pipe(paths, prov)
    p.caps[("jquants", "bars_1d")] = Probe("bars_1d", True, DAYS[0], DAYS[2])
    p.fetch(DAYS[0], DAYS[-1], ["calendar", "bars_1d"])
    assert [u for ds, u in prov.calls if ds == "bars_1d"] == [d.isoformat() for d in DAYS[:3]]
    assert "bars_1d" not in p.unavailable


def test_update_without_a_bars_source_and_refetch_without_data_say_why(paths):
    class NoBars(FakeJP):
        unit_kinds = {"calendar": "all", "instruments": "all"}

    with pytest.raises(RuntimeError, match="日线"):
        pipe(paths, NoBars()).update(DAYS[-1])
    with pytest.raises(RuntimeError, match="还没有快照"):
        pipe(paths, FakeJP()).refetch(DAYS[-1])


def test_the_daily_cycle_reads_trading_days_from_the_stored_calendar_without_fetching(paths):
    prov = FakeJP()
    p = pipe(paths, prov)
    assert p.is_trading_day(DAYS[0]) is None and p.previous_trading_day(DAYS[0]) is None   # no calendar yet
    p.backfill(DAYS[0], DAYS[-1])
    calls = len(prov.calls)
    assert p.is_trading_day(DAYS[2]) is True and p.previous_trading_day(DAYS[2]) == DAYS[1]
    assert p.previous_trading_day(DAYS[-1] + timedelta(days=3)) == DAYS[-1]                 # Monday: last Friday
    assert p.is_trading_day(DAYS[-1] + timedelta(days=1)) is None                           # past the stored calendar: unknown
    assert len(prov.calls) == calls                                                         # stored only, no fetch


def test_index_members_come_with_the_backfill(paths):
    topix = FakeTopix()
    p = pipe(paths, FakeJP(), topix=topix)
    p.backfill(DAYS[0], DAYS[-1])
    assert topix.windows == [(DAYS[0], DAYS[-1])] and p.store.read("index_members").height == 1


def test_a_weekly_dataset_never_stored_comes_with_its_history_on_the_next_monday(paths):
    # added to the catalog after the first backfill (index members): the whole history, not only that week's window
    pipe(paths, FakeJP(), topix=FakeTopix(down=True)).backfill(DAYS[0], DAYS[-1])
    topix = FakeTopix()
    later = MON + timedelta(days=7)
    pipe(paths, FakeJP(), topix=topix).update(later, wait=False)
    assert topix.windows == [(DAYS[0], later)]                        # from the first stored bar
    topix2 = FakeTopix()
    week_on = datetime(2026, 10, 16, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    pipe(paths, FakeJP(), clock=lambda: week_on, topix=topix2).update(later + timedelta(days=7), wait=False)
    assert topix2.windows == [(later + timedelta(days=1), later + timedelta(days=7))]   # stored now: the week


class LateValuation(FakeJP):
    """Valuation by date for every traded listing — except the `late` day's, which has one listing until cleared."""
    unit_kinds = {**FakeJP.unit_kinds, "valuation": "date"}

    def __init__(self, late=None, **kw):
        super().__init__(**kw)
        self.late = late

    def fetch(self, dataset, unit, start, end):
        if dataset != "valuation":
            return super().fetch(dataset, unit, start, end)
        self.calls.append((dataset, unit))
        d = date.fromisoformat(unit)
        lids = ["JP:7203"] if d == self.late else ["JP:7203", "JP:130A", "JP:9999"]
        return {"valuation": pl.DataFrame({"listing_id": lids, "date": [d] * len(lids), "pe": [10.0] * len(lids),
                                           "source": ["jquants"] * len(lids)})}


def test_a_dataset_that_comes_later_than_the_bars_is_named_and_completed_when_it_arrives(paths):
    # spec §4.5: readiness per dataset — valuation often arrives after the bars; a rule that does not read it need not wait
    prov = LateValuation(late=DAYS[-1])
    p = pipe(paths, prov)
    p.backfill(DAYS[0], DAYS[-2])
    p.update(DAYS[-1], wait=False)
    assert p.incomplete(DAYS[-1]) == ["valuation"] and p.incomplete(DAYS[-2]) == []     # one of three listings
    assert p.complete(DAYS[-1], ["valuation"]) is None                                   # still late: nothing new
    prov.late = None
    snap = p.complete(DAYS[-1], ["valuation"])
    assert snap is not None and p.incomplete(DAYS[-1]) == []
    assert p.store.read("valuation").filter(pl.col("date") == DAYS[-1]).height == 3


class WithFinancials(FakeJP):
    """FakeJP with the day's financial statements (J-Quants: by disclosure day, with the time)."""
    unit_kinds = {**FakeJP.unit_kinds, "financials": "date"}
    REPORTS = {DAYS[1]: [("JP:7203", "13:55:00"), ("JP:130A", "15:30:00")], DAYS[2]: [("JP:9999", None)]}

    def fetch(self, dataset, unit, start, end):
        if dataset != "financials":
            return super().fetch(dataset, unit, start, end)
        self.calls.append((dataset, unit))
        rows = self.REPORTS.get(date.fromisoformat(unit), [])
        return {"financials": pl.DataFrame({"listing_id": [r[0] for r in rows], "period_end": [date(2026, 6, 30)] * len(rows),
                                            "period_type": ["Q1"] * len(rows), "ann_date": [date.fromisoformat(unit)] * len(rows),
                                            "ann_time": [r[1] for r in rows], "net_profit": [1.0] * len(rows),
                                            "source": ["jquants"] * len(rows)},
                                           schema_overrides={"ann_time": pl.Utf8, "period_end": pl.Date, "ann_date": pl.Date})}


def test_financials_are_known_from_the_first_close_after_their_announcement(paths):
    # spec §4.7: a report announced during the session is read at that day's close; one at or after the close — or
    # without a time, as A-share reports come — from the next trading day's
    p = pipe(paths, WithFinancials())
    p.backfill(DAYS[0], DAYS[-1])
    known = dict(p.store.read("financials").select("listing_id", "known_at").iter_rows())
    assert known == {"JP:7203": DAYS[1], "JP:130A": DAYS[2], "JP:9999": DAYS[3]}


def test_a_report_periods_units_are_the_quarter_ends_whose_reports_can_still_come(paths):
    # an annual report is due by the end of April; late filers and new listings bring a period's first figures later
    # (real data: 130 companies' FY2024 figures first came out 130–190 days after the year end): a day's update asks
    # the periods ending in the 190 days before it, a backfill every one in its range
    from wb.data.pipeline import quarter_ends
    assert quarter_ends(date(2026, 9, 28), date(2026, 10, 2)) == [date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
    assert quarter_ends(date(2026, 6, 15), date(2026, 6, 15)) == [date(2025, 12, 31), date(2026, 3, 31)]
    assert quarter_ends(date(2025, 1, 1), date(2025, 12, 31))[0] == date(2024, 6, 30)


def test_a_days_statements_posted_after_the_update_come_with_the_next_mornings_refetch(paths):
    # J-Quants' summaries by disclosure day can fill in after the day's update has run: the refetch of that day asks
    # again (a report-period source re-reads its periods every day and is not asked again)
    prov = WithFinancials()
    prov.REPORTS = {DAYS[1]: WithFinancials.REPORTS[DAYS[1]]}
    p = pipe(paths, prov)
    p.backfill(DAYS[0], DAYS[-1])
    prov.REPORTS = WithFinancials.REPORTS
    assert p.refetch(DAYS[2]) is not None
    known = dict(p.store.read("financials").select("listing_id", "known_at").iter_rows())
    assert known == {"JP:7203": DAYS[1], "JP:130A": DAYS[2], "JP:9999": DAYS[3]}
    assert p.refetch(DAYS[2]) is None                                                   # asked again: nothing new


def test_a_backfill_of_one_dataset_adds_its_history_and_keeps_the_bars_day(paths):
    # financial history added after the bars (J-Quants: a request a day of it): only that dataset is fetched, and the
    # snapshot's day stays the last day that has bars
    prov = WithFinancials()
    p = pipe(paths, prov)
    p.backfill(DAYS[0], DAYS[2])
    prov.calls.clear()
    snap = p.backfill(DAYS[0], DAYS[-1], datasets=["financials"])
    assert snap.as_of == DAYS[2] and {ds for ds, _ in prov.calls} <= {"calendar", "financials"}
    assert p.store.read("financials").height == 3 and p.store.read("bars_1d")["date"].max() == DAYS[2]


def test_a_source_not_to_be_stored_here_is_not_fetched_and_says_why(paths):
    # spec §4.10 本地存储 off: what it would bring is never fetched (all that is fetched is kept here)
    from wb.workspace.store import Workspace
    Workspace(type(paths)(paths.root, "prod")).set_setting("datause.jquants.store", False)
    prov = WithFinancials()
    p = pipe(paths, prov)
    with pytest.raises(RuntimeError, match="本地存储"):
        p.update(DAYS[0], wait=False)
    assert prov.calls == []


class BrokenValuation(FakeJP):
    """Valuation that fails at fetch time (a changed field, a server error)."""
    unit_kinds = {**FakeJP.unit_kinds, "valuation": "date"}

    def fetch(self, dataset, unit, start, end):
        if dataset == "valuation":
            raise ValueError("valuation: 字段 PER 不见了")
        return super().fetch(dataset, unit, start, end)


def test_a_datasets_last_fetch_error_is_kept_for_the_settings_page(paths):
    # spec §4.9: the data and settings page lists, per dataset and market, … 最近一次错误
    from wb.data.capability import last_errors
    p = pipe(paths, BrokenValuation())
    with pytest.raises(ValueError, match="PER"):
        p.backfill(DAYS[0], DAYS[-1])
    e = last_errors(p.store.conn, "JP")[("jquants", "valuation")]
    assert "PER" in e["error"] and e["unit"] == DAYS[0].isoformat() and e["at"]
    assert ("jquants", "bars_1d") not in last_errors(p.store.conn, "JP")


def test_a_backfill_or_a_refetch_never_runs_on_a_source_kept_from_storing_here(paths):
    # spec §4.10: a backfill would publish a snapshot without the bars, and the next update would start after it
    from wb.workspace.store import Workspace
    prov = WithFinancials()
    pipe(paths, prov).backfill(DAYS[0], DAYS[1])
    Workspace(type(paths)(paths.root, "prod")).set_setting("datause.jquants.store", False)
    p, before = pipe(paths, prov), pipe(paths, prov).store.latest().id
    prov.calls.clear()
    for run in (lambda: p.backfill(DAYS[0], DAYS[-1]), lambda: p.refetch(DAYS[1])):
        with pytest.raises(RuntimeError, match="本地存储"):
            run()
    assert prov.calls == [] and p.store.latest().id == before


def test_a_weekly_dataset_stored_before_the_catalog_gained_a_column_comes_again_on_any_day(paths, monkeypatch):
    # design 2026-10-12 §10: the industry rows' 申万二级 columns come with the next update, not the next Monday; once
    # stored with them, the dataset waits for Monday again
    import wb.datasets as D
    pipe(paths, FakeJP()).backfill(DAYS[0], DAYS[-1])
    ds = D.DATASETS["index_members"]
    monkeypatch.setitem(D.DATASETS, "index_members", D.Dataset(ds.name, ds.key, {**ds.columns, "extra": pl.Utf8}, ds.partition))
    tue = MON + timedelta(days=8)
    topix = FakeTopix()
    pipe(paths, FakeJP(), topix=topix).update(tue, wait=False)
    assert topix.windows                                                  # a Tuesday, refetched
    wed = FakeTopix()
    pipe(paths, FakeJP(), topix=wed).update(tue + timedelta(days=1), wait=False)
    assert wed.windows == []                                              # stored with the column now


class FlowCN(FakeJP):
    """FakeJP's bars as 沪深 stocks, with money flow and unlocks when `flow` is on (the catalog gained them)."""
    name, market = "tushare", "CN"

    def __init__(self, flow=True, scale=1.0, **kw):
        super().__init__(**kw)
        self.flow, self.scale = flow, scale
        self.unit_kinds = {**FakeJP.unit_kinds, **({"moneyflow": "date", "unlocks": "all", "float_holders": "period"} if flow else {})}
        self.starts = {}

    def fetch(self, dataset, unit, start, end):
        if dataset == "float_holders":
            self.calls.append((dataset, unit))
            self.starts.setdefault(dataset, set()).add(start)
            return {"float_holders": pl.DataFrame({"listing_id": ["CN:600519"], "period_end": [date.fromisoformat(unit)],
                                                   "ann_date": [date.fromisoformat(unit) + timedelta(days=30)], "holder": ["UBSAG"],
                                                   "holder_type": ["一般企业"], "float_ratio": [0.6], "source": ["tushare"]})}
        if dataset == "moneyflow":
            self.calls.append((dataset, unit))
            lids = ["CN:600519", "CN:000001", "CN:600000"]
            return {"moneyflow": pl.DataFrame({"listing_id": lids, "date": [date.fromisoformat(unit)] * 3,
                                               **{f"{s}_{k}": [25.0 * (self.scale if s == "buy" else 1.0)] * 3
                                                  for s in ("buy", "sell") for k in ("sm", "md", "lg", "elg")},
                                               "source": ["tushare"] * 3})}
        if dataset == "unlocks":
            self.calls.append((dataset, end.isoformat()))
            return {"unlocks": pl.DataFrame({"listing_id": ["CN:600519"], "float_date": [end + timedelta(days=20)], "holder": ["某股东"],
                                             "share_type": ["定增股份"], "ann_date": [end], "float_share": [1e6], "float_ratio": [6.0],
                                             "source": ["tushare"]})}
        out = super().fetch(dataset, unit, start, end)
        for k, df in out.items():
            if "listing_id" in df.columns:
                out[k] = df.with_columns(pl.col("listing_id").str.replace("JP:7203", "CN:600519").str.replace("JP:130A", "CN:000001")
                                         .str.replace("JP:9999", "CN:600000"))
            if k == "bars_1d":
                out[k] = out[k].with_columns(pl.lit(100.0).alias("amount"))
        out.pop("corp_actions", None)
        return out


def cn(paths, prov):
    from wb.config import Paths
    from wb.data.capability import save_probes
    from wb.db import connect
    prod = Paths(paths.root, "prod")
    save_probes(connect(prod.app_db, "app"), "tushare", "CN", [Probe("corp_actions", False)])
    return Pipeline(prod, "CN", providers={"tushare": prov}, log=lambda m: None, clock=lambda: AFTER_CLOSE)


def test_money_flow_comes_with_its_whole_history_once_then_day_by_day(paths):
    cn(paths, FlowCN(flow=False)).backfill(DAYS[0], DAYS[2])                 # stored before the catalog had money flow
    prov = FlowCN()
    p = cn(paths, prov)
    p.update(DAYS[3], wait=False)
    assert sorted(u for ds, u in prov.calls if ds == "moneyflow") == [d.isoformat() for d in DAYS[:4]]   # from the first bar
    prov2 = FlowCN()
    cn(paths, prov2).update(DAYS[4], wait=False)
    assert [u for ds, u in prov2.calls if ds == "moneyflow"] == [DAYS[4].isoformat()]
    assert p.store.read("moneyflow").height == 15 and p.store.read("unlocks").height == 2   # one unlock row per update's window


def test_float_holders_come_by_report_period_their_history_once(paths):
    # 多个外资持股 (10-17): the report periods whose lists can come in the half year before (as 财务); the first update after
    # the catalog gained them asks from the first bar, later ones from the day after the last snapshot
    cn(paths, FlowCN(flow=False)).backfill(DAYS[0], DAYS[2])
    prov = FlowCN()
    p = cn(paths, prov)
    p.update(DAYS[3], wait=False)
    assert sorted(u for ds, u in prov.calls if ds == "float_holders") == ["2026-03-31", "2026-06-30", "2026-09-30"]
    assert prov.starts["float_holders"] == {DAYS[0]} and p.store.read("float_holders").height == 3
    prov2 = FlowCN()
    cn(paths, prov2).update(DAYS[4], wait=False)
    assert prov2.starts["float_holders"] == {DAYS[4]}                       # asked again each day: late filers, new periods


def test_a_dataset_the_catalog_gained_comes_with_its_history_on_a_day_off_too(paths):
    # you (10-17, a Saturday): 多个外资持股 could not be read — the new dataset waited for Monday's update. An update on a day
    # off brings what the catalog gained (its whole history, to the last published day) and nothing else
    cn(paths, FlowCN(flow=False)).backfill(DAYS[0], DAYS[2])
    prov = FlowCN()
    p = cn(paths, prov)
    assert p.missing_history() == ["moneyflow", "float_holders"]
    p.update(DAYS[4] + timedelta(days=1), wait=False)                      # Saturday
    latest = p.store.latest()
    assert latest.manifest.get("moneyflow") and latest.manifest.get("float_holders") and latest.as_of == DAYS[2]
    assert sorted(u for ds, u in prov.calls if ds == "moneyflow") == [d.isoformat() for d in DAYS[:3]]
    assert not [u for ds, u in prov.calls if ds == "bars_1d"]               # the days missed wait for a trading day
    assert p.missing_history() == []
    assert cn(paths, FlowCN(flow=False)).missing_history() == []           # a source that has none: nothing missing


def test_a_broken_day_of_money_flow_is_refused_whole(paths):
    from wb.data.pipeline import QualityError
    cn(paths, FlowCN()).backfill(DAYS[0], DAYS[3])
    with pytest.raises(QualityError, match="资金流向"):
        cn(paths, FlowCN(scale=0.5)).update(DAYS[4], wait=False)


def test_the_calendar_is_kept_three_months_ahead(paths):
    # 解禁前 counts 20 open days to an unlock: across 国庆 or 春节 that is 33–37 calendar days, past a 30-day calendar
    p = cn(paths, FlowCN())
    p.backfill(DAYS[0], DAYS[2])
    p.update(DAYS[3], wait=False)
    assert p.store.read("calendar")["date"].max() >= DAYS[3] + timedelta(days=90)
