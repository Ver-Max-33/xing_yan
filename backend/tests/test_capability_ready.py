from datetime import datetime
from zoneinfo import ZoneInfo

from wb.data.capability import load_caps, route, save_probes
from wb.data.providers.base import Probe
from wb.data.ready import is_ready, wait_ready
from wb.db import connect
from wb.markets import load_market


def test_caps_roundtrip_and_route(paths):
    conn = connect(paths.app_db, "app")
    save_probes(conn, "tushare", "CN", [Probe("bars_1d", True, rate_per_min=50),
                                        Probe("index_bars_1d", False, detail="没有权限")])
    caps = load_caps(conn, "CN")
    assert caps[("tushare", "bars_1d")].rate_per_min == 50
    assert route("test", "CN", "bars_1d", caps) == "tushare"
    assert route("test", "CN", "index_bars_1d", caps) == "yfinance"  # free tier has no index_daily
    assert route("prod", "CN", "index_bars_1d", caps) is None  # prod never switches source


def test_a_source_that_cannot_be_reached_while_probing_keeps_what_was_known(paths, monkeypatch):
    # a network blip at the day's first probe is not "the plan lacks it": the source is still tried, the blip is its last error
    import httpx

    from wb.data import capability as C
    conn = connect(paths.app_db, "app")
    save_probes(conn, "tushare", "CN", [Probe("bars_1d", True, rate_per_min=500)])

    class Down:
        def probe(self):
            raise httpx.ConnectError("network down")
    monkeypatch.setattr(C, "make_provider", lambda name, market, caps=None: Down())
    C.probe_market(conn, "prod", "CN")
    caps = load_caps(conn, "CN")
    assert caps[("tushare", "bars_1d")].available and route("prod", "CN", "bars_1d", caps) == "tushare"
    assert "network down" in C.last_errors(conn, "CN")[("tushare", "bars_1d")]["error"]


def test_never_probed_is_tried():
    assert route("prod", "JP", "bars_1d", {}) == "jquants"


def test_is_ready_needs_stable_count():
    assert not is_ready(5300, 5400, None)
    assert not is_ready(5300, 5400, 5200)
    assert is_ready(5300, 5400, 5300)
    assert not is_ready(5000, 5400, 5000)  # below 98%


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        from datetime import timedelta

        self.t += timedelta(seconds=s)


def test_wait_ready_polls_until_stable():
    tz = ZoneInfo("Asia/Tokyo")
    c = Clock(datetime(2026, 10, 1, 15, 0, tzinfo=tz))
    counts = iter([(3000, 3900), (3890, 3900), (3890, 3900)])
    assert wait_ready(lambda: next(counts), load_market("JP"), now=c.now, sleep=c.sleep, log=lambda m: None)
    assert c.t == datetime(2026, 10, 1, 16, 42, tzinfo=tz)  # 16:30 start, :40 all there, looked at again 2 minutes on


def test_a_day_already_out_is_confirmed_in_two_minutes_not_ten():
    # the update began at 16:05 with every row out (Tushare, 2026-10-08): a second look 10 minutes later only waited
    tz = ZoneInfo("Asia/Shanghai")
    c = Clock(datetime(2026, 10, 8, 16, 5, tzinfo=tz))
    counts = iter([(5557, 5480), (5557, 5480)])
    assert wait_ready(lambda: next(counts), load_market("CN"), now=c.now, sleep=c.sleep, log=lambda m: None)
    assert c.t == datetime(2026, 10, 8, 16, 7, tzinfo=tz)


def test_wait_ready_gives_up_at_window_end():
    tz = ZoneInfo("Asia/Shanghai")
    c = Clock(datetime(2026, 10, 9, 19, 45, tzinfo=tz))
    assert not wait_ready(lambda: (0, 5400), load_market("CN"), now=c.now, sleep=c.sleep, log=lambda m: None)


def test_make_provider_loads_all_modules_even_if_one_was_imported():
    import wb.data.providers.yfinance_test  # noqa: F401 - registers "yfinance" first
    from wb.data.capability import make_provider

    assert make_provider("xcals", "JP").name == "xcals"
