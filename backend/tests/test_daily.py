"""The daily cycle (spec §4.5, §7.1): from the ready window on, wait for the day's data and publish, scan the enabled
rules, queue what changed; a snapshot nobody scanned yet is scanned (after a restart too); the next morning at
08:30 re-fetch the previous day; a delay at the window's end is one notice. State survives a restart."""
import json
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from tests.test_l3_preview import TUNED
from tests.test_l3_run import ABOVE_MA
from wb.data.pipeline import DataDelayed
from wb.data.store import Staging, Store
from wb.devdata import END, write
from wb.engine.l3.rule import load_rule
from wb.engine.l3.run import run_rule
from wb.jobs.daily import Daily, wait_until
from wb.notify.outbox import Outbox
from wb.realtime.providers.yfinance_rt import YahooRealtime
from wb.workspace.briefing import BriefItems
from wb.workspace.rules import Rules
from wb.workspace.store import Workspace

SH = ZoneInfo("Asia/Shanghai")


class FakePipeline:
    """Stands in for P0's pipeline: the day's data is "published" by returning the store's latest snapshot."""
    calls: list = []
    missing: list = []   # missing_history(): datasets the catalog gained that the latest snapshot lacks

    def __init__(self, paths, market, delayed=False):
        self.paths, self.market, self.delayed = paths, market, delayed

    def update(self, day=None, *, wait=True):
        FakePipeline.calls.append(("update", self.market, day, wait))
        if self.delayed:
            raise DataDelayed(f"{self.market} {day}: data not ready by the end of the window")
        return Store(self.paths, self.market).latest()

    def refetch(self, day):
        FakePipeline.calls.append(("refetch", self.market, day))
        return None

    def incomplete(self, day):
        return []

    def complete(self, day, datasets):
        return None

    def is_trading_day(self, day):
        return day.weekday() < 5

    def missing_history(self):
        return FakePipeline.missing

    def previous_trading_day(self, day):
        from datetime import timedelta
        d = day - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d


def daily(paths, delayed=False):
    FakePipeline.calls = []
    return Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m, delayed), markets=("CN",), allow_demo=True)


def test_a_running_turn_says_what_it_is_doing_and_says_nothing_once_it_is_over(paths, monkeypatch):
    from wb.jobs import daily as daily_mod
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    d, seen = daily(paths), []
    real = daily_mod.scan

    def spy(paths, market, rules, day=None, progress=None, **kw):   # what the page would read at each step of the scan
        def said(x):
            progress(x)
            seen.append(d.progress(market))
        return real(paths, market, rules, day=day, progress=said, **kw)
    monkeypatch.setattr(daily_mod, "scan", spy)
    assert d.progress("CN") is None                                              # idle: nothing to say
    assert d.run_now("CN") and wait_until(lambda: not d.running("CN"))
    assert seen and all(p["step"] == "scan" and p["rule"] == "站上 20 日线" and (p["i"], p["n"]) == (1, 1) for p in seen)
    assert all(p["since"] <= datetime.now().isoformat() for p in seen)          # when the turn began: the time it has taken
    fractions = [p["fraction"] for p in seen]
    assert fractions == sorted(fractions) and 0 <= fractions[0] and fractions[-1] <= 1   # never back, never past the end
    assert d.progress("CN") is None and d.state("CN")["status"] == "done"       # over: nothing left to say


def test_the_scheduled_update_says_it_is_waiting_for_the_days_data(paths):
    import threading
    write(paths, 320)
    release = threading.Event()

    class Waiting(FakePipeline):   # the day's data is not there yet: update(wait=True) waits for it
        def update(self, day=None, *, wait=True):
            release.wait(10)
            return super().update(day, wait=wait)
    d = Daily(lambda m: paths, pipeline=lambda p, m: Waiting(p, m), markets=("CN",), allow_demo=True)
    d._claim("CN", "daily", lambda: d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH)))
    assert wait_until(lambda: (d.progress("CN") or {}).get("step") == "update")
    assert d.progress("CN")["waiting"] is True                                       # the page asks slowly and says so
    release.set()
    assert wait_until(lambda: not d.running("CN"))


def test_while_it_waits_for_the_days_rows_it_says_so_not_the_last_dataset(paths):
    # the calendar comes first, then the wait for the day's rows (looked at every few minutes): the page said
    # 「更新数据：交易日历」 for all of it (2026-10-08)
    import threading
    write(paths, 320)
    release, said = threading.Event(), threading.Event()

    class Waiting(FakePipeline):
        def update(self, day=None, *, wait=True):
            self.log("CN calendar via tushare: 1/1 units to fetch")
            self.log("CN 16:05 rows 5557/5480")
            said.set()
            release.wait(10)
            return super().update(day, wait=wait)
    d = Daily(lambda m: paths, pipeline=lambda p, m: Waiting(p, m), markets=("CN",), allow_demo=True)
    d._claim("CN", "daily", lambda: d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH)))
    assert said.wait(10)
    p = d.progress("CN")
    assert "等当天的数据" in p["phase"] and "5,557" in p["phase"] and p["waiting"] is True
    release.set()
    assert wait_until(lambda: not d.running("CN"))


def test_naming_the_delisted_listings_says_so(paths):
    # J-Quants' delisted listings are named once from its past lists (2026-10-08: 944, two and a half minutes): the page
    # says that, not the last dataset fetched
    import threading
    write(paths, 320)
    release, said = threading.Event(), threading.Event()

    class Naming(FakePipeline):
        def update(self, day=None, *, wait=True):
            self.log("  financials 3/3")
            self.log("  instruments: naming 944 delisted listings")
            said.set()
            release.wait(10)
            return super().update(day, wait=wait)
    d = Daily(lambda m: paths, pipeline=lambda p, m: Naming(p, m), markets=("CN",), allow_demo=True)
    d._claim("CN", "daily", lambda: d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH)))
    assert said.wait(10)
    assert d.progress("CN")["phase"] == "给 944 只退市的股票补上名称和市场（只做一次）"
    release.set()
    assert wait_until(lambda: not d.running("CN"))


def two_rules(paths):
    Rules(paths).save(TUNED)
    Rules(paths).save(TUNED.replace("id: above_ma", "id: above_ma_2").replace("name: 站上 20 日线", "name: 站上 20 日线 · 二"))
    for rid in ("above_ma", "above_ma_2"):
        Rules(paths).set_state(rid, enabled=True)


def runs_of(paths, rule_id):
    from wb.db import connect
    return connect(paths.app_db, "app").execute("SELECT COUNT(*) FROM rule_run WHERE rule_id=?", (rule_id,)).fetchone()[0]


def paused_at_the_second_rule(paths, monkeypatch, how="pause"):
    """A manual scan of two rules, paused (or cancelled) as the second begins: the first is done, the second is not."""
    from wb.jobs import daily as daily_mod
    write(paths, 320)
    two_rules(paths)
    d, real = daily(paths), daily_mod.scan

    def spy(paths, market, rules, day=None, progress=None, **kw):
        def said(x):
            if x["i"] == 2:
                getattr(d, how)(market)
            progress(x)
        return real(paths, market, rules, day=day, progress=said, **kw)
    monkeypatch.setattr(daily_mod, "scan", spy)
    assert d.run_now("CN") and wait_until(lambda: not d.running("CN"))
    monkeypatch.setattr(daily_mod, "scan", real)
    return d


def test_a_paused_scan_keeps_the_rules_it_finished_and_goes_on_from_the_next(paths, monkeypatch):
    d = paused_at_the_second_rule(paths, monkeypatch)
    st = d.state("CN")
    assert st["status"] == "paused" and "1 / 2" in st["message"]                  # how far it got
    assert (runs_of(paths, "above_ma"), runs_of(paths, "above_ma_2")) == (1, 0)
    assert "scan" not in d.due("CN", datetime(2026, 10, 2, 15, 0, tzinfo=SH))       # you paused it: no scan behind your back
    assert d.resume("CN") and wait_until(lambda: not d.running("CN"))
    st = d.state("CN")
    assert st["status"] == "done" and set(json.loads(st["scan_summary"])) == {"above_ma", "above_ma_2"}
    assert (runs_of(paths, "above_ma"), runs_of(paths, "above_ma_2")) == (1, 1)     # the first rule was not run again
    assert d.resume("CN") is False                                                 # nothing paused any more


def test_a_scan_cut_off_by_quitting_the_app_can_be_continued_after_the_restart(paths, monkeypatch):
    d = paused_at_the_second_rule(paths, monkeypatch)
    d._save("CN", status="scanning")                       # as the app leaves it when it is quit in the middle
    d2 = daily(paths)
    d2.recover()                                           # the next start
    st = d2.state("CN")
    assert st["status"] == "paused" and "关掉" in st["message"] and "1 / 2" in st["message"]
    assert d2.resume("CN") and wait_until(lambda: not d2.running("CN"))
    assert d2.state("CN")["status"] == "done" and (runs_of(paths, "above_ma"), runs_of(paths, "above_ma_2")) == (1, 1)


def test_a_cancelled_scan_leaves_today_as_it_was_and_is_not_started_again_by_itself(paths, monkeypatch):
    d = paused_at_the_second_rule(paths, monkeypatch, how="cancel_scan")
    st = d.state("CN")
    assert st["status"] == "cancelled" and "已取消" in st["message"] and st["scan_summary"] is None   # nothing scanned shown
    assert d.progress("CN") is None and d.resume("CN") is False                    # nothing to continue
    assert "scan" not in d.due("CN", datetime(2026, 10, 2, 15, 0, tzinfo=SH))       # you cancelled it: not behind your back
    assert d.run_now("CN") and wait_until(lambda: not d.running("CN"))             # scanned again when you say so
    assert d.state("CN")["status"] == "done" and set(json.loads(d.state("CN")["scan_summary"])) == {"above_ma", "above_ma_2"}


def test_a_paused_scan_can_be_cancelled_instead_of_continued(paths, monkeypatch):
    d = paused_at_the_second_rule(paths, monkeypatch)
    assert d.state("CN")["status"] == "paused"
    assert d.cancel_scan("CN") is True                                               # given up: no 继续 any more
    assert d.state("CN")["status"] == "cancelled" and d.resume("CN") is False
    assert "scan" not in d.due("CN", datetime(2026, 10, 2, 15, 0, tzinfo=SH))
    assert d.cancel_scan("CN") is False                                              # nothing left to stop


def test_what_is_due_follows_the_market_clock(paths):
    write(paths, 320)                                                                  # the data runs to Wednesday 09-30
    d = daily(paths)
    assert d.due("CN", datetime(2026, 9, 30, 15, 59, tzinfo=SH)) == ["scan"]           # before the window: the unscanned snapshot
    assert d.due("CN", datetime(2026, 9, 30, 16, 0, tzinfo=SH)) == ["update", "scan"]
    assert d.due("CN", datetime(2026, 10, 2, 9, 0, tzinfo=SH)) == ["refetch", "scan"]  # Friday morning: Thursday is missing
    assert "update" not in d.due("CN", datetime(2026, 10, 3, 17, 0, tzinfo=SH))        # a Saturday: no update


def test_a_day_off_runs_an_update_only_for_a_new_datasets_history(paths):
    # you (10-17, a Saturday): the holder lists the catalog gained would have waited for Monday's close — a day off brings
    # them, once; with nothing missing a Saturday stays quiet
    write(paths, 320)
    d, sat = daily(paths), datetime(2026, 10, 3, 17, 0, tzinfo=SH)
    try:
        assert "update" not in d.due("CN", sat)
        FakePipeline.missing = ["float_holders"]
        assert "update" in d.due("CN", sat) and "update" in d.due("CN", sat.replace(hour=9))   # any time of the day off
        d._save("CN", update_day="2026-10-03")
        assert "update" not in d.due("CN", sat + timedelta(hours=1))         # once that day
    finally:
        FakePipeline.missing = []


def test_a_day_runs_update_scan_and_queues_nothing_until_the_briefing(paths):
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    d = daily(paths)
    now = datetime(2026, 10, 2, 16, 5, tzinfo=SH)
    d.run("CN", "update", now)
    st = d.state("CN")
    assert st["status"] == "done" and st["update_day"] == "2026-10-02" and st["scanned_snapshot"] == Store(paths, "CN").latest().id
    assert "above_ma" in st["scan_summary"] and Outbox(paths).recent(100) == []     # the briefing says it in the morning
    assert d.due("CN", now) == []
    d2 = daily(paths)
    assert d2.due("CN", now) == [] and d2.state("CN")["update_day"] == "2026-10-02"


def test_a_delay_at_the_end_of_the_window_is_said_by_the_state_and_the_morning_catches_up(paths):
    write(paths, 320)
    d = daily(paths, delayed=True)
    d.run("CN", "update", datetime(2026, 10, 2, 20, 1, tzinfo=SH))
    assert d.state("CN")["status"] == "delayed" and Outbox(paths).recent(10) == []
    morning = datetime(2026, 10, 5, 8, 31, tzinfo=SH)
    assert "refetch" in d.due("CN", morning)
    FakePipeline.calls = []
    d.run("CN", "refetch", morning)
    assert FakePipeline.calls[0][0] in ("update", "refetch")
    assert END.isoformat() <= d.state("CN")["refetch_day"]


def test_demo_data_is_never_mixed_with_fetched_data_nor_sent(paths):
    write(paths, 320)                                                                  # the demo data (note "devdata")
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",))
    assert d.due("CN", datetime(2026, 9, 30, 16, 0, tzinfo=SH)) == []                  # the scheduler leaves it alone
    FakePipeline.calls = []
    assert d.run_now("CN") and wait_until(lambda: d.state("CN")["status"] == "done")
    assert FakePipeline.calls == [] and d.state("CN")["scanned_snapshot"]             # a manual run scans, fetches nothing
    assert d.state("CN")["scan_summary"] and Outbox(paths).recent(10) == []           # made-up signals never reach your bot


def test_a_market_without_data_waits_for_its_backfill(paths):
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",))
    assert d.due("CN", datetime(2026, 10, 2, 16, 0, tzinfo=SH)) == []                  # no snapshot: `wb backfill` first


def test_a_scan_waits_for_a_running_whole_market_run(paths):
    import time

    from wb.engine.l3.run import RUN_LOCK
    write(paths, 320)
    d = daily(paths)
    with RUN_LOCK:                                                                     # e.g. 重新扫描 on the 选股 page
        assert d.run_now("CN")
        time.sleep(0.5)
        assert d.state("CN")["status"] != "done"                                       # one full-market run at a time
    assert wait_until(lambda: d.state("CN")["status"] == "done")


def test_a_manual_run_scans_again_so_a_rule_enabled_since_is_included(paths):
    write(paths, 320)
    d = daily(paths)
    now = datetime(2026, 10, 2, 16, 5, tzinfo=SH)
    d.run("CN", "scan", now)                                                           # scanned, no rule enabled yet
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    d.run("CN", "scan", now)                                                           # the scheduler: already scanned
    assert "above_ma" not in d.state("CN")["scan_summary"]
    assert d.run_now("CN") and wait_until(lambda: "above_ma" in (d.state("CN")["scan_summary"] or ""))


def test_a_manual_run_whose_update_fails_still_scans_and_says_why(paths):
    write(paths, 320)
    d = daily(paths, delayed=True)
    assert d.run_now("CN", now=datetime(2026, 10, 2, 16, 5, tzinfo=SH)) and wait_until(lambda: not d.running("CN"))   # after the close
    st = d.state("CN")
    assert st["status"] == "delayed" and "not ready" in st["message"] and st["scanned_snapshot"]  # scanned what there is


def test_each_data_profile_keeps_its_own_cycle(tmp_path):
    from wb.config import get_paths
    test, prod = get_paths("test", tmp_path), get_paths("prod", tmp_path)        # both under the test's own folder
    write(test, 320)
    now = {"paths": test}
    d = Daily(lambda m: now["paths"], pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True)
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert d.state("CN")["update_day"] == "2026-10-02"
    now["paths"] = prod                                                                # 设置 → 数据档: CN switched
    assert d.state("CN")["update_day"] is None and d.state("CN")["scanned_snapshot"] is None


NEEDS_TURNOVER = TUNED.replace("id: above_ma", "id: needs_turnover").replace("name: 站上 20 日线", "name: 换手率规则") \
    .replace("C > MA(C, $n)", "C > MA(C, $n) AND TURNOVER > 3") \
    .replace("markets: [CN, JP]", "markets: [CN]")   # compiles (A 股 has 换手率), fails in the scan: the demo profile has no valuation data


def test_a_rule_that_fails_is_reported_and_the_snapshot_is_not_scanned_again_every_tick(paths):
    write(paths, 320)
    for text, rid in ((TUNED, "above_ma"), (NEEDS_TURNOVER, "needs_turnover")):
        Rules(paths).save(text)
        Rules(paths).set_state(rid, enabled=True)
    d = daily(paths)
    now = datetime(2026, 10, 2, 16, 5, tzinfo=SH)
    d.run("CN", "scan", now)
    st = d.state("CN")
    assert st["status"] == "error" and "换手率规则" in st["message"]
    summary = json.loads(st["scan_summary"])
    assert summary["above_ma"]["counts"] and "TURNOVER" in summary["needs_turnover"]["error"]     # the others still ran
    assert st["scanned_snapshot"] == Store(paths, "CN").latest().id and "scan" not in d.due("CN", now)


def test_a_failed_update_stays_in_view_and_is_tried_again_later_not_every_tick(paths):
    from datetime import timedelta
    write(paths, 320)

    class Down(FakePipeline):
        def update(self, day=None, *, wait=True):
            raise ConnectionError("network down")
    d = Daily(lambda m: paths, pipeline=lambda p, m: Down(p, m), markets=("CN",), allow_demo=True)
    now = datetime(2026, 10, 2, 16, 5, tzinfo=SH)
    d.run("CN", "scan", now)                                                            # yesterday's snapshot: scanned
    for step in d.due("CN", now):
        d.run("CN", step, now)
    st = d.state("CN")
    assert st["status"] == "error" and "network down" in st["message"]                 # not covered by the scan step
    assert d.due("CN", now + timedelta(minutes=5)) == []
    assert d.due("CN", now + timedelta(minutes=11)) == ["update"]


def test_a_manual_run_during_the_session_scans_without_fetching_or_a_delay(paths):
    write(paths, 320)
    d = daily(paths)
    FakePipeline.calls = []
    assert d.run_now("CN", now=datetime(2026, 10, 2, 10, 0, tzinfo=SH))                # a Friday, the market open
    assert wait_until(lambda: not d.running("CN"))
    assert FakePipeline.calls == [] and d.state("CN")["status"] == "done"


def test_a_delay_reads_in_chinese(paths):
    write(paths, 320)
    d = daily(paths, delayed=True)
    d.run("CN", "update", datetime(2026, 10, 2, 20, 1, tzinfo=SH))
    assert d.state("CN")["message"] == "2026-10-02 的数据到就绪窗口结束（20:00）还没齐"


def test_a_watched_stocks_fail_line_goes_into_the_next_briefing(paths):
    from wb.workspace.watch import Watch
    d = briefed(paths)
    w = Watch(lambda m: paths)
    w.add("CN:600519", now=datetime(2026, 9, 29, 17, 0))
    w.add_condition("CN:600519", "fail", "收盘跌回箱顶", op="<", price=10.0**9)
    d.run("CN", "rescan", datetime(2026, 9, 30, 16, 30, tzinfo=SH))
    assert Outbox(paths).recent(50) == []                                             # not a message of its own any more
    b = d.brief("CN", datetime(2026, 10, 1, 8, 50, tzinfo=SH))
    s = next(x for x in b["sections"] if x["title"].startswith("你的观察"))
    assert "失效条件：收盘跌回箱顶" in s["lines"][0]["text"] and s["lines"][0]["name"] == "贵州茅台"


def test_made_up_prices_never_fire_a_watched_stocks_condition(paths):
    # watch entries are shared by both data profiles: a demo close would use up a condition the real data never crossed
    from wb.workspace.watch import Watch
    write(paths, 320)
    w = Watch(lambda m: paths)
    w.add("CN:600519", now=datetime(2026, 10, 1, 17, 0))
    w.add_condition("CN:600519", "fail", "收盘跌回箱顶", op="<", price=10.0**9)
    Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",)).run(
        "CN", "scan", datetime(2026, 10, 2, 16, 5, tzinfo=SH))                             # devdata: made up
    assert w.entry("CN:600519")["conditions"][0]["fired_day"] is None
    assert [r["text"] for r in w.record("CN:600519")][0] == "加失效条件：收盘跌回箱顶"


def test_a_calendar_that_ends_says_where_it_ends_and_what_to_do():
    # the test profile's calendars come from exchange_calendars, whose holiday tables stop at a fixed year
    from wb.jobs.daily import _zh
    e = RuntimeError("CN: the calendar has no entry for 2027-01-04 (unknown is not closed; it ends at 2026-12-31); "
                     "extend the calendar source before updating")
    assert _zh(e, "CN") == ("交易日历里还没有 2027-01-04（只到 2026-12-31），不能判断那天开不开市：测试档的日历来自 exchange_calendars，"
                            "升级它，或者把这个市场切到正式档（用 Tushare / J-Quants 的日历）")


def test_a_sources_expired_certificate_is_said_as_theirs_and_not_worked_around():
    # 10-17 08:59 JST: Tushare's own certificate (*.tushare.pro, ZeroSSL) ran out; every update failed with the TLS text.
    # Said as the source's, not your network's or key's; the key is never sent over a connection that was not verified
    from wb.jobs.daily import _zh
    e = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1010)")
    said = _zh(e, "CN")
    assert "证书过期" in said and "对方" in said and "自动" in said and "SSL" not in said
    other = _zh(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in chain"), "CN")
    assert "证书" in other and "代理" in other and "过期" not in other


def test_after_a_scan_old_snapshots_files_and_run_files_are_cleared_away(paths):
    # each update rewrites the year's partition: without this the old files pile up (~10 GB a year per market)
    import polars as pl

    from wb.engine.l3.run import runs
    from wb.jobs import daily as daily_mod
    write(paths, 320)
    st = Store(paths, "CN")
    bars = st.read("bars_1d")
    for k in range(1, daily_mod.KEEP_SNAPSHOTS + 4):  # more snapshots than are kept, each rewriting a partition
        st.publish({"bars_1d": bars.tail(1).with_columns(pl.col("volume") + k)}, as_of=st.latest().as_of, note=f"t{k}")
    parts = lambda: len(list((paths.market_dir("CN")).glob("*/*/part-*.parquet")))  # noqa: E731
    before = parts()
    rules = Rules(paths)
    rules.save(ABOVE_MA)
    rules.set_state("above_ma", True, None)
    d = daily(paths)
    for day in (2, 3, 4):  # three scans on three days: one run file each
        d.run("CN", "rescan", datetime(2026, 10, day, 16, 5, tzinfo=SH))
    assert len(st.history()) == daily_mod.KEEP_SNAPSHOTS + 4 and parts() < before          # files of the older ones gone
    kept = runs(paths, "above_ma", 100)                                                    # under KEEP_RUNS: all kept
    assert len(kept) == 3 and all((paths.profile_dir / "runs" / f"{r['id']}.parquet").exists() for r in kept)


def test_run_files_are_pruned_to_the_newest_per_rule_and_market(paths):
    from wb.engine.l3.run import prune_runs, runs
    write(paths, 320)
    Rules(paths).save(ABOVE_MA)
    rule = load_rule(ABOVE_MA)
    made = [run_rule(paths, rule, "CN").id for _ in range(4)]
    other = run_rule(paths, rule, "JP").id
    assert prune_runs(paths, keep=2) == 2
    left = {r["id"] for r in runs(paths, "above_ma", 100)}
    assert left == {*made[-2:], other} and not (paths.profile_dir / "runs" / f"{made[0]}.parquet").exists()


class LateValuation(FakePipeline):
    """The day's valuation comes after the bars: still short until `late` is cleared, then published."""
    late = True

    def incomplete(self, day):
        return ["valuation"] if LateValuation.late else []

    def complete(self, day, datasets):
        if LateValuation.late:
            return None
        st = Store(self.paths, self.market)
        bars = st.read("bars_1d").tail(1)
        return st.publish({"bars_1d": bars.with_columns(bars["volume"] + 1)}, as_of=st.latest().as_of, note="valuation")


def with_valuation(paths):
    """The demo data and a PE for every bar (the demo data has no valuation)."""
    import polars as pl
    write(paths, 320)
    st = Store(paths, "CN")
    bars = st.read("bars_1d").select("listing_id", "date")
    st.publish({"valuation": bars.with_columns(pl.lit(10.0).alias("pe"), pl.lit("t").alias("source"))}, as_of=st.latest().as_of,
               note="valuation")


def test_a_rule_that_reads_late_valuation_waits_for_it_and_the_others_do_not(paths):
    with_valuation(paths)
    rules = Rules(paths)
    for text in (ABOVE_MA, ABOVE_MA.replace("id: above_ma", "id: cheap").replace('"C > MA(C, 20)"', '"C > MA(C, 20) AND PE > 0"')):
        rid = rules.save(text)[0]
        rules.set_state(rid, True, None)
    LateValuation.late = True
    d = Daily(lambda m: paths, pipeline=lambda p, m: LateValuation(p, m), markets=("CN",), allow_demo=True)
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    summary = json.loads(d.state("CN")["scan_summary"])
    assert "run_id" in summary["above_ma"] and summary["cheap"]["waiting"] == "等估值数据"      # PE: valuation
    assert d.due("CN", datetime(2026, 10, 2, 16, 10, tzinfo=SH)) == []                        # asked again every 10 min
    assert d.due("CN", datetime(2026, 10, 2, 16, 16, tzinfo=SH)) == ["complete"]
    d.run("CN", "complete", datetime(2026, 10, 2, 16, 16, tzinfo=SH))
    assert "waiting" in json.loads(d.state("CN")["scan_summary"])["cheap"]                    # still late
    LateValuation.late = False
    d.run("CN", "complete", datetime(2026, 10, 2, 16, 27, tzinfo=SH))
    st = d.state("CN")
    assert st["pending"] is None and "run_id" in json.loads(st["scan_summary"])["cheap"]


def test_valuation_that_never_comes_by_the_end_of_the_window_is_said_and_the_rule_scanned_without_it(paths):
    with_valuation(paths)
    rules = Rules(paths)
    rid = rules.save(ABOVE_MA.replace("id: above_ma", "id: cheap").replace('"C > MA(C, 20)"', '"C > MA(C, 20) AND PE > 0"'))[0]
    rules.set_state(rid, True, None)
    LateValuation.late = True
    d = Daily(lambda m: paths, pipeline=lambda p, m: LateValuation(p, m), markets=("CN",), allow_demo=True)
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    d.run("CN", "complete", datetime(2026, 10, 2, 20, 1, tzinfo=SH))
    st = d.state("CN")
    assert st["pending"] is None and "run_id" in json.loads(st["scan_summary"])["cheap"]
    assert st["message"].startswith("估值数据到就绪窗口结束（20:00）还没齐")


def test_a_scan_waiting_for_a_validation_says_so(paths):
    import threading

    from wb.engine.l3.run import RUN_LOCK
    write(paths, 320)
    d = daily(paths)
    held, release = threading.Event(), threading.Event()

    def validation():
        with RUN_LOCK:
            held.set()
            release.wait(10)
    t = threading.Thread(target=validation)
    t.start()
    held.wait(10)
    scan = threading.Thread(target=d.run, args=("CN", "rescan", datetime(2026, 10, 2, 16, 5, tzinfo=SH)))
    scan.start()
    assert wait_until(lambda: d.state("CN")["message"] == "排队中：等别的全市场运行（另一个市场的扫描、验证或校准）做完", timeout=5)
    release.set()
    t.join()
    scan.join(30)
    assert d.state("CN")["status"] == "done" and d.state("CN")["message"] is None


ALWAYS = """
id: always
name: 每天
markets: [CN]
stages:
  - {id: today, kind: event, when: "RAW_C > 0"}
signal_at: today.at
"""


def test_the_scan_confirms_the_days_intraday_alerts_it_signalled_and_revokes_the_rest(paths, monkeypatch):
    # spec §7.2: a rule alert waits for the close; the day's scan judges it (a rule not scanned keeps its alerts
    # waiting) — in the app: Feishu hears neither the rule alerts nor their verdicts (design 2026-10-10 §4)
    from wb.realtime.alerts import Alerts
    monkeypatch.setattr(YahooRealtime, "delay", 0)   # a realtime source: the close's verdicts go out
    write(paths, 320)
    Rules(paths).save(ALWAYS)
    Rules(paths).set_state("always", enabled=True)
    a = Alerts(paths)
    for lid, key in (("CN:600519", "always"), ("CN:000002", "always"), ("CN:600519", "elsewhere")):
        a.add(day=END, market="CN", listing_id=lid, kind="rule", key=key, at=datetime(2026, 9, 30, 10, 31), price=10.0,
              title=lid, text="盘中满足")
    daily(paths).run("CN", "scan", datetime(2026, 9, 30, 16, 30, tzinfo=SH))
    assert {(x["listing_id"], x["key"]): x["status"] for x in a.day(END)} == {
        ("CN:600519", "always"): "confirmed", ("CN:000002", "always"): "revoked", ("CN:600519", "elsewhere"): "pending"}
    assert [r for r in Outbox(paths).recent() if r["kind"] == "live"] == []


def test_the_days_new_signals_are_researched_up_to_your_daily_cap_and_old_researches_settled(paths, monkeypatch):
    # spec §10.3: 对每日少量候选自动触发（上限由用户设定）, off until you set it; §10.4: settled 20 trading days on
    import pytest

    from tests.test_ai_research import answer
    from wb.ai import journal
    from wb.workspace.store import Workspace
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    days = Store(paths, "CN").read("calendar")["date"].sort().to_list()
    old = journal.save(paths, {"listing_id": "CN:600519", "day": str(days[200]), "trigger": "manual", "status": "ok", "flags": [],
                               "output": json.loads(answer()), "pack": {"facts": [], "hash": "h"},
                               "created_at": f"{days[200]}T20:00:00+08:00"})
    monkeypatch.setenv("WB_SECRET_DEEPSEEK_API_KEY", "sk-test")
    cite = {"text": "见收盘", "refs": ["px.close"], "inference": False}
    sent = []

    def route(request):
        sent.append(request.url.host)
        told = "上次研究" in json.loads(request.content)["messages"][1]["content"]
        rel = {"verdict": "同向", "text": "见收盘", "refs": ["px.close"]} if told else {"verdict": "首次", "text": "", "refs": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": answer(support=[cite], against=[cite], invalidation=[cite],
                                                                                     continuity=rel)}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert Workspace(paths).settings()["research.daily"] == 0
    with pytest.raises(ValueError, match="0 to 10"):
        Workspace(paths).set_setting("research.daily", 11)
    Workspace(paths).set_setting("research.daily", 1)
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True,
              transport=httpx.MockTransport(route))
    d.run("CN", "scan", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    made = [r for r in journal.recent(paths) if r["trigger"] == "daily"]
    assert len(made) == 1 and len(sent) == 1 and made[0]["day"] == str(days[-1])
    assert journal.get(paths, old["id"])["settled_at"] is not None


def test_a_daily_research_that_fails_never_turns_a_finished_scan_into_an_error(paths, monkeypatch):
    # spec §10.3: AI that cannot run stops quietly — the scan is done either way
    from wb.workspace.store import Workspace
    write(paths, 320)
    Workspace(paths).set_setting("research.daily", 1)

    def locked(*a, **k):
        raise OSError("database is locked")
    monkeypatch.setattr("wb.ai.journal.recent", locked)
    d = daily(paths)
    d.run("CN", "scan", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert d.state("CN")["status"] == "done"


class Backfilling(FakePipeline):
    """A backfill that says how far it is, then waits for the test to let it finish."""
    go = None

    def backfill(self, start, end=None, datasets=None):
        FakePipeline.calls.append(("backfill", self.market, start, end, datasets))
        self.log("CN bars_1d via tushare: 2400/2400 units to fetch")
        self.log("  bars_1d 50/2400")
        Backfilling.go.wait(10)
        return Store(self.paths, self.market).latest()


def test_a_backfill_from_the_data_page_says_how_far_it_is_then_scans_what_it_brought(paths):
    import threading
    from datetime import date
    write(paths, 320)
    FakePipeline.calls, Backfilling.go = [], threading.Event()
    probed = []
    d = Daily(lambda m: paths, pipeline=lambda p, m: Backfilling(p, m), markets=("CN",), allow_demo=True,
              probe=lambda p, m: probed.append(m))
    assert d.backfill_now("CN", date(2016, 10, 1), datasets=["financials"])
    assert wait_until(lambda: "日线：50 / 2400" in (d.state("CN")["message"] or ""))
    assert d.state("CN")["status"] == "backfilling"
    assert not d.run_now("CN") and not d.backfill_now("CN", date(2016, 10, 1))   # one job a market at a time
    Backfilling.go.set()
    assert wait_until(lambda: not d.running("CN"))
    yesterday = datetime.now(SH).date() - timedelta(days=1)     # today is the daily update's, once its data is ready (§4.5)
    assert FakePipeline.calls[0] == ("backfill", "CN", date(2016, 10, 1), yesterday, ["financials"]) and probed == ["CN"]
    assert d.state("CN")["status"] == "done" and d.state("CN")["scanned_snapshot"]   # what it brought is scanned


def test_a_backfill_that_fails_half_way_is_resumed_before_any_update_touches_the_market(paths):
    # an update now would publish the half and lose where the backfill stopped
    class Broken(FakePipeline):
        def backfill(self, start, end=None, datasets=None):
            Staging(self.paths, self.market).put("tushare", "bars_1d", "2016-10-10", {})   # one unit in, then cut off
            raise RuntimeError("tushare: 抱歉，您每分钟最多访问该接口500次")
    write(paths, 320)
    FakePipeline.calls = []
    d = Daily(lambda m: paths, pipeline=lambda p, m: Broken(p, m), markets=("CN",), allow_demo=True)
    assert d.backfill_now("CN", date(2016, 10, 1)) and wait_until(lambda: not d.running("CN"))
    st = d.state("CN")
    assert st["status"] == "interrupted" and st["message"].startswith("补历史出错：") and "接着取" in st["message"]
    assert d.due("CN", datetime(2026, 10, 2, 16, 5, tzinfo=SH)) == []                # no update on top of it
    assert d.run_now("CN") and wait_until(lambda: not d.running("CN"))
    assert [c for c in FakePipeline.calls if c[0] == "update"] == [] and d.state("CN")["status"] == "interrupted"


def test_a_backfill_cut_off_by_quitting_the_app_is_said_at_the_next_start(paths):
    write(paths, 320)
    d = daily(paths)
    d._save("CN", status="backfilling", message="补历史（2016-10-01 起）：日线：1250 / 2400")
    Staging(paths, "CN").put("tushare", "bars_1d", "2016-10-10", {})
    d.recover()
    st = d.state("CN")
    assert st["status"] == "interrupted" and "1 份" in st["message"] and "接着取" in st["message"]
    assert d.due("CN", datetime(2026, 10, 2, 16, 5, tzinfo=SH)) == []


def test_a_job_cut_off_with_nothing_half_done_is_simply_over_at_the_next_start(paths):
    write(paths, 320)
    d = daily(paths)
    for status in ("checking", "backfilling"):   # a check stores nothing; a backfill that had published everything
        d._save("CN", status=status, message="…")
        d.recover()
        assert (d.state("CN")["status"], d.state("CN")["message"]) == ("done", None)


def test_demo_data_is_never_backfilled_into_nor_checked(paths):
    import pytest
    from datetime import date
    write(paths, 320)
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",))
    for start in (lambda: d.backfill_now("CN", date(2016, 10, 1)), lambda: d.crosscheck_now("CN")):
        with pytest.raises(ValueError, match="演示数据"):
            start()
    assert not d.running("CN")


def test_a_check_keeps_the_reason_a_market_is_delayed(paths):
    import polars as pl
    write(paths, 320)
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True,
              check=lambda pipe, n, days: pl.DataFrame({"field": ["close"], "ok": [True]}))
    d._save("CN", status="delayed", message="2026-10-02 的数据到就绪窗口结束（20:00）还没齐")
    assert d.crosscheck_now("CN") and wait_until(lambda: not d.running("CN"))
    assert (d.state("CN")["status"], d.state("CN")["message"]) == ("delayed", "2026-10-02 的数据到就绪窗口结束（20:00）还没齐")


def test_a_job_started_while_the_scheduler_decides_is_never_run_beside(paths):
    import threading
    write(paths, 320)
    FakePipeline.calls, Backfilling.go = [], threading.Event()
    d = Daily(lambda m: paths, pipeline=lambda p, m: Backfilling(p, m), markets=("CN",), allow_demo=True)
    real = d.due

    def deciding(market, now):   # a click on 补历史 lands while the tick works out what is due
        steps = real(market, now)
        assert d.backfill_now(market, date(2016, 10, 1))
        return steps or ["scan"]
    d.due = deciding
    d.tick(datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    import time
    time.sleep(0.5)
    assert d.state("CN")["status"] == "backfilling"                                    # the tick's scan did not start
    Backfilling.go.set()
    assert wait_until(lambda: not d.running("CN"))


def test_the_check_runs_after_the_scan_so_a_slow_source_never_holds_up_the_days_notices(paths):
    write(paths, 320)
    seen = []
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True,
              check=lambda pipe, n, days: seen.append(d.state("CN")["scan_at"]))
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert seen and seen[0] is not None


def test_the_data_sources_are_probed_when_the_app_starts(paths):
    # spec §4.9: at start and on the day's first update
    write(paths, 320)
    probed = []
    Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True,
          probe=lambda p, m: probed.append(m)).startup()
    assert probed == ["CN"]


def test_a_check_against_the_independent_source_runs_in_the_markets_turn_and_leaves_its_state(paths):
    import polars as pl
    write(paths, 320)
    asked = []

    def check(pipe, n, days):
        asked.append((pipe.market, n, days))
        return pl.DataFrame({"field": ["close", "vol"], "ok": [True, False]})
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True, check=check)
    d.run("CN", "scan", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    before = d.state("CN")
    assert d.crosscheck_now("CN") and wait_until(lambda: not d.running("CN"))
    assert asked == [("CN", 30, 5)]
    after = d.state("CN")
    assert after["status"] == before["status"] == "done" and after["message"] == "抽样核对：2 项里 1 项一致，不一致的列在数据页"


def test_the_days_update_asks_what_the_plan_covers_and_checks_a_sample_against_the_independent_source(paths):
    # spec §4.9: probed on the first update of a day; §4.6: every daily publish is sampled against an independent source
    write(paths, 320)
    probed, checked = [], []
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True,
              probe=lambda p, m: probed.append(m), check=lambda pipe, n, days: checked.append((n, days)))
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert probed == ["CN"] and checked == [(30, 1)]


def test_a_probe_or_a_check_that_fails_never_fails_the_update(paths):
    write(paths, 320)

    def down(*a):
        raise OSError("network down")
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True, probe=down, check=down)
    d.run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert d.state("CN")["status"] == "done"


def test_a_manual_run_says_whether_it_fetches_or_only_scans_again(paths):
    # 立即更新 during the session, on demo data or with a 补历史 to finish: what there is is scanned again, nothing fetched
    write(paths, 320)
    d = daily(paths)
    assert d.fetches("CN", datetime(2026, 10, 2, 16, 5, tzinfo=SH))                    # a Friday after the close
    assert not d.fetches("CN", datetime(2026, 10, 2, 10, 0, tzinfo=SH))                # the market open
    d._save("CN", status="interrupted")
    assert not d.fetches("CN", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert not Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",)).fetches(
        "CN", datetime(2026, 10, 2, 16, 5, tzinfo=SH))                                # demo data


def briefed(paths, transport=None):
    """Demo data scanned on END (Wednesday 9-30), the briefing on (a test profile is off by itself)."""
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    BriefItems(lambda m: paths).add("above_ma")
    Workspace(paths).set_setting("brief.CN", "on")
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True, transport=transport)
    d.run("CN", "scan", datetime(2026, 9, 30, 16, 5, tzinfo=SH))
    return d


def briefs(paths):
    return [n for n in Outbox(paths).recent(50) if n["kind"] == "brief"]


def test_the_briefing_is_made_at_its_time_once_a_day_and_never_after_the_close(paths):
    d = briefed(paths)
    assert not d.brief_when("CN", datetime(2026, 10, 1, 8, 49, tzinfo=SH), False)
    assert d.brief_when("CN", datetime(2026, 10, 1, 8, 50, tzinfo=SH), False)
    d.run("CN", "brief", datetime(2026, 10, 1, 8, 50, tzinfo=SH))
    [b] = briefs(paths)
    assert b["payload"]["title"] == "A 股 · 10-01 周四 开盘前" and b["payload"]["card"]["header"]["template"] == "blue"
    assert not d.brief_when("CN", datetime(2026, 10, 1, 9, 0, tzinfo=SH), False)    # once a day, a restart too
    assert not daily(paths).brief_when("CN", datetime(2026, 10, 1, 9, 0, tzinfo=SH), False)
    assert d.brief_when("CN", datetime(2026, 10, 2, 14, 59, tzinfo=SH), False)      # the next day, still before the close
    assert not d.brief_when("CN", datetime(2026, 10, 2, 15, 0, tzinfo=SH), False)   # at the close: no more


def test_a_briefing_made_after_the_open_is_marked_late_and_one_whose_close_is_missing_warns(paths):
    d = briefed(paths)
    b = d.brief("CN", datetime(2026, 10, 2, 10, 0, tzinfo=SH))
    assert b["title"] == "A 股 · 10-02 周五 开盘前（补发）"
    assert b["warning"].startswith("10-01 的数据没取到")                            # the demo data ends on 9-30


def test_the_briefing_waits_for_the_morning_correction_until_ten_minutes_before_the_open(paths):
    d = briefed(paths)
    assert not d.brief_when("CN", datetime(2026, 10, 1, 8, 55, tzinfo=SH), True)
    assert d.brief_when("CN", datetime(2026, 10, 1, 9, 20, tzinfo=SH), True)
    gate = threading.Event()
    assert d._claim("CN", "daily", gate.wait)                                         # a long correction scan holds the turn
    d.tick(datetime(2026, 10, 1, 9, 20, tzinfo=SH))
    assert len(briefs(paths)) == 1                                                    # made beside it, not after it
    gate.set()


def test_a_market_set_off_on_demo_data_or_on_a_day_without_trading_gets_no_briefing(paths):
    d = briefed(paths)
    assert not d.brief_when("CN", datetime(2026, 10, 3, 8, 50, tzinfo=SH), False)    # a Saturday
    Workspace(paths).set_setting("brief.CN", "auto")                                 # the test profile: off by itself
    assert not d.brief_when("CN", datetime(2026, 10, 1, 8, 50, tzinfo=SH), False)
    Workspace(paths).set_setting("brief.CN", "on")
    demo = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",))
    assert not demo.brief_when("CN", datetime(2026, 10, 1, 8, 50, tzinfo=SH), False)


def test_the_briefing_goes_to_your_bot_as_one_card_and_is_given_up_at_the_close(paths, monkeypatch):
    posted = []
    bot = httpx.MockTransport(lambda q: posted.append(json.loads(q.content)) or httpx.Response(200, json={"code": 0}))
    d = briefed(paths, bot)
    assert [n["kind"] for n in Outbox(paths).recent(50)] == []                       # a scan queues nothing by itself
    d.run("CN", "brief", datetime(2026, 10, 1, 8, 50, tzinfo=SH))
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    assert d.notify(datetime(2026, 10, 1, 8, 51, tzinfo=SH)) == 1
    assert posted[0]["card"]["header"]["title"]["content"] == "A 股 · 10-01 周四 开盘前"
    d.run("CN", "brief", datetime(2026, 10, 2, 8, 50, tzinfo=SH))
    assert d.notify(datetime(2026, 10, 2, 15, 1, tzinfo=SH)) == 0                     # past the close: not sent stale
    assert Outbox(paths).stats()["given_up"] == 1 and len(posted) == 1


def test_two_senders_at_once_send_each_notice_once(paths, monkeypatch):
    # an intraday alert is sent the moment it is queued while the scheduler's round may be sending (design 2026-10-10
    # §5): one sender at a time, or both would post what neither had marked sent yet
    import time
    posted = []

    def slow(q):
        time.sleep(0.2)                                                               # the other sender arrives meanwhile
        posted.append(json.loads(q.content))
        return httpx.Response(200, json={"code": 0})
    write(paths, 320)
    d = Daily(lambda m: paths, pipeline=lambda p, m: FakePipeline(p, m), markets=("CN",), allow_demo=True, transport=httpx.MockTransport(slow))
    now = datetime(2026, 10, 2, 10, 31, 5, tzinfo=SH)
    Outbox(paths).enqueue("live", "live|1", {"title": "平安银行 000001 · A 股", "line": "10:31 · 可能突破，注意看"}, "CN", "2026-10-02", now=now)
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    senders = [threading.Thread(target=d.notify, args=(now,)) for _ in range(2)]
    for t in senders:
        t.start()
    for t in senders:
        t.join()
    assert len(posted) == 1


def test_the_close_verdicts_of_a_delayed_source_are_not_sent(paths):
    from wb.realtime.alerts import Alerts
    write(paths, 320)
    Rules(paths).save(ALWAYS)
    Rules(paths).set_state("always", enabled=True)
    Alerts(paths).add(day=END, market="CN", listing_id="CN:600519", kind="rule", key="always",
                      at=datetime(2026, 9, 30, 10, 31), price=10.0, title="CN:600519", text="盘中满足")
    daily(paths).run("CN", "scan", datetime(2026, 9, 30, 16, 30, tzinfo=SH))      # the test profile: yfinance, 20 min late
    assert [r for r in Outbox(paths).recent() if r["kind"] == "live"] == []


def test_the_danger_history_is_ready_before_the_scan_starts(paths, monkeypatch):
    # computed first, under the scan's lock: a page opened during the evening scan finds it, and the two peaks never overlap
    from wb import danger
    from wb.jobs import daily as daily_mod
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    seen, real = [], daily_mod.scan

    def watching(p, market, *a, **kw):
        st = Store(p, market)
        seen.append((st.derived / st.latest().id / f"danger-v{danger.VERSION}.parquet").exists())
        return real(p, market, *a, **kw)
    monkeypatch.setattr(daily_mod, "scan", watching)
    daily(paths).run("CN", "update", datetime(2026, 10, 2, 16, 5, tzinfo=SH))
    assert seen == [True]
