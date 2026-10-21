"""Data-layer review 2026-10-13: a day without limits (an A-share new listing's first days) has no 一个板 — Tushare gives a
placeholder limit (1,000,000), LIMIT_PCT is unknown there (spec §4.4), not 1,000,000 ÷ 昨收; and a TSE limit-down price is
never under 1 yen (the table's width over a 2-yen close made it −28)."""
from datetime import date, timedelta

import polars as pl

from tests.test_l1_library import frame, values
from wb.charts.series import daily_series
from wb.datasets import conform


def test_limit_pct_is_unknown_on_a_day_without_limits():
    cn = frame(close=[52.88, 50.0], up_limit=[1_000_000.0, 58.17], down_limit=[0.01, 47.59], pre_close=[45.05, 52.88])
    got = values("LIMIT_PCT", cn)
    assert got[0] is None and abs(got[1] - 0.1) < 1e-3


def test_a_tse_limit_down_price_is_never_under_one_yen():
    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(2)]
    daily = conform(pl.DataFrame({"listing_id": "JP:1757", "date": days, "open": [2.0, 1.0], "high": [2.0, 2.0], "low": [2.0, 1.0],
                                  "close": [2.0, 1.0], "volume": [1e6, 1e6], "adj_factor": 1.0, "share_factor": 1.0,
                                  "suspended": False}), "bars_1d")
    day, _ = daily_series(daily, "JP", "raw")
    assert day["down_limit"][-1] == 1.0 and day["up_limit"][-1] == 32.0
