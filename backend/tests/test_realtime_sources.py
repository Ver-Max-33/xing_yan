"""Realtime sources (spec §4.8, §3.5): registered in REALTIME; yfinance (the test profile's, ~20 minutes late) gives
today's minute bars of stocks and indexes in one call; tickers follow Yahoo (7203.T, 600519.SS, 000001.SZ); a day
the source has not reached yet is empty, not yesterday's."""
from datetime import date, datetime

import pandas as pd
import pytest

from wb import registry
from wb.realtime.providers.yfinance_rt import YahooRealtime, yahoo_ticker

DAY = date(2026, 10, 2)


def frame(rows: dict[str, list[tuple]]):
    """yf.download's shape: one column block per ticker, a tz-aware minute index in whole seconds (as it really comes)."""
    idx = pd.DatetimeIndex(sorted({pd.Timestamp(t, tz="Asia/Tokyo") for rs in rows.values() for t, *_ in rs})).as_unit("s")
    cols = {}
    for tk, rs in rows.items():
        by = {pd.Timestamp(t, tz="Asia/Tokyo"): r for t, *r in rs}
        for k, name in enumerate(("Open", "High", "Low", "Close", "Volume")):
            cols[(tk, name)] = [by[i][k] if i in by else float("nan") for i in idx]
    return pd.DataFrame(cols, index=idx)


def test_yahoo_tickers():
    assert [yahoo_ticker(x) for x in ("JP:7203", "JP:130A", "CN:600519", "CN:000001", "CN:300750", "CN:830799")] == [
        "7203.T", "130A.T", "600519.SS", "000001.SZ", "300750.SZ", None]                  # 北交所: not on Yahoo


def test_todays_minutes_come_in_one_call_and_another_day_is_left_out():
    calls = []

    def download(tickers, day):
        calls.append((tickers, day))
        return frame({"7203.T": [("2026-10-01 15:24", 2850, 2851, 2849, 2850, 1000),
                                 ("2026-10-02 09:00", 2860, 2866, 2858, 2865, 50000), ("2026-10-02 09:01", 2865, 2870, 2864, 2869, 30000)],
                      "^N225": [("2026-10-02 09:00", 39800, 39850, 39790, 39840, 0)]})
    src = YahooRealtime("JP", download=download)
    m = src.minutes(["JP:7203"], DAY, indexes=["JP:IDX:N225"])
    assert calls == [(["7203.T", "^N225"], DAY)]                               # one day asked for, not five
    assert m.filter(m["listing_id"] == "JP:7203").select("at", "open", "close", "volume").rows() == [
        (datetime(2026, 10, 2, 9, 0), 2860.0, 2865.0, 50000.0), (datetime(2026, 10, 2, 9, 1), 2865.0, 2869.0, 30000.0)]
    assert m.filter(m["listing_id"] == "JP:IDX:N225").height == 1
    assert src.minutes(["JP:7203"], date(2026, 10, 5)).is_empty()


def test_sources_are_registered():
    registry.load_all()
    yahoo = registry.REALTIME["yfinance_rt"]
    assert yahoo.delay == 20 and yahoo.max_listings == 50
    # measured 2026-10-02: Shenzhen minutes add up to 1.3–1.5x the day's volume, Tokyo's miss both auctions (62%)
    assert yahoo.approx_volume is True


def test_yahoo_keeps_about_a_week_of_minutes_for_the_days_before():
    # the multi-day 分时 can start with the days before the app first ran (Yahoo: ~7 days of 1-minute bars)
    from wb.realtime.providers.yfinance_rt import YahooRealtime
    assert YahooRealtime.history_days == 5
