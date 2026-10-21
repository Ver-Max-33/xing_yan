"""The day's volume estimated on the market's volume clock (design 2026-10-10 §6): the share of a day's volume that has
usually traded by the end of each minute, from the market index's kept minutes (the median of at least 5 days), else a
default measured on a year of A-share minutes; without either, linear (a market with no minute volume to read)."""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from wb.realtime.clock import DEFAULT, MIN_DAYS, clock_for, from_minutes, share
from wb.realtime.providers.yfinance_rt import MINUTES

CN_SESSIONS = [(datetime(2026, 1, 1, 9, 30).time(), datetime(2026, 1, 1, 11, 30).time()),
               (datetime(2026, 1, 1, 13, 0).time(), datetime(2026, 1, 1, 15, 0).time())]


def index_day(day: date, volumes: list[float]) -> pl.DataFrame:
    """CN:IDX:000001's minutes of a day: the morning's 120 then the afternoon's 120, by their start."""
    starts = [datetime.combine(day, CN_SESSIONS[0][0]) + timedelta(minutes=k) for k in range(120)] + \
             [datetime.combine(day, CN_SESSIONS[1][0]) + timedelta(minutes=k) for k in range(120)]
    return pl.DataFrame({"listing_id": "CN:IDX:000001", "at": starts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": volumes}, schema=MINUTES)


def test_the_default_a_share_clock_starts_heavy_and_ends_whole():
    c = DEFAULT["CN"]
    assert len(c) == 240 and all(b >= a for a, b in zip(c, c[1:], strict=False)) and c[-1] == 1.0
    assert 0.03 < c[0] < 0.05 and 0.6 < c[119] < 0.68      # the open's minute heavy; the morning about two thirds
    assert share(c, 30, 240) > 2 * 30 / 240                  # by 10:00 well over the linear eighth


def test_the_clock_is_the_median_of_the_index_s_days():
    heavy = [5.0] * 30 + [1.0] * 180 + [3.0] * 30           # mornings and closes busier
    days = [index_day(date(2026, 9, d), heavy) for d in range(1, 1 + MIN_DAYS)]
    c = from_minutes(pl.concat(days), "CN:IDX:000001")
    total = sum(heavy)
    assert len(c) == 240 and c[29] == pytest.approx(150 / total) and c[-1] == pytest.approx(1.0)


def test_too_few_days_of_the_index_fall_back_to_the_default_and_a_market_without_one_is_linear(paths):
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True)
    for d in range(1, MIN_DAYS):                                    # one day short
        index_day(date(2026, 9, d), [1.0] * 240).write_parquet(folder / f"date=2026-09-{d:02d}.parquet")
    assert clock_for(paths, "CN") == DEFAULT["CN"]
    index_day(date(2026, 9, MIN_DAYS), [1.0] * 240).write_parquet(folder / f"date=2026-09-{MIN_DAYS:02d}.parquet")
    assert clock_for(paths, "CN")[119] == pytest.approx(0.5)        # now its own: even days, half by noon
    assert clock_for(paths, "JP") is None and share(None, 30, 330) == pytest.approx(30 / 330)
