"""盯盘提醒的三层 (design 2026-10-10 §3): said before a line is reached, not after it is confirmed. 竞价: the day's open
already beyond the line or within 1% of it; 逼近: a minute within 1% of the line, at a new high of the day (a new low
on the selling side) and on the line's side of the day's average price, the line not reached yet; 触线: a minute's high
(low) reaches the line. A minute just as it is read — every minute the stock traded today counts for the day's high
and its average price."""
from datetime import datetime

import polars as pl
import pytest

from wb.realtime.layers import auction, judge, with_stats
from wb.realtime.providers.yfinance_rt import MINUTES

UP, DOWN = {"side": "up", "price": 10.0}, {"side": "down", "price": 10.0}


def day(rows, lid="CN:600001", prev=9.6) -> list[dict]:
    """A stock's minutes [(minute past 10:00, high, low, close, volume[, turnover])] (open = close; turnover: close ×
    volume unless given) with what the layers read."""
    m = pl.DataFrame([(lid, datetime(2026, 10, 12, 10, r[0]), r[3], r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else r[3] * r[4])
                      for r in rows], schema={**MINUTES, "amount": pl.Float64}, orient="row")
    return with_stats(m, {lid: prev}).to_dicts()


def test_the_auction_speaks_when_the_open_is_beyond_the_line_or_within_one_percent_of_it():
    assert [auction(UP, p) for p in (10.05, 10.0, 9.92, 9.85)] == ["beyond", "beyond", "near", None]
    assert [auction(DOWN, p) for p in (9.95, 10.08, 10.2)] == ["beyond", "near", None]


def test_the_layers_read_the_days_high_and_low_before_each_minute_and_its_average_price_to_it():
    m = day([(0, 9.7, 9.6, 9.7, 100), (1, 9.9, 9.7, 9.8, 300)])
    assert (m[0]["hh"], m[0]["ll"], m[1]["hh"], m[1]["ll"]) == (9.6, 9.6, 9.7, 9.6)   # the previous close before the first
    assert m[1]["vwap"] == pytest.approx((9.7 * 100 + 9.8 * 300) / 400)


def test_an_approach_is_near_the_line_at_a_new_high_above_the_average_then_the_touch():
    m = day([(0, 9.80, 9.70, 9.80, 100),        # 2% short: too far
             (1, 9.93, 9.80, 9.92, 100),        # 0.8% short, a new high, above the average: 逼近
             (2, 9.95, 9.90, 9.94, 100),        # still near: said once
             (3, 10.02, 9.94, 9.99, 100)])      # the high reaches 10: 触线
    assert [(k, r["at"].minute) for k, r in judge(UP, m)] == [("near", 1), ("touch", 3)]


def test_no_approach_without_a_new_high_or_below_the_average_price():
    no_high = day([(0, 9.95, 9.60, 9.65, 1000), (1, 9.94, 9.90, 9.93, 100)])    # within 1% but under the morning's 9.95
    below = day([(0, 9.93, 9.90, 9.92, 100, 999.0), (1, 10.0, 9.92, 9.95, 100)])  # traded at 9.99 on average: 9.92 is under it
    assert judge(UP, no_high) == [] and [k for k, _ in judge(UP, below)] == ["touch"]


def test_the_selling_side_is_the_mirror():
    m = day([(0, 10.30, 10.20, 10.25, 100), (1, 10.10, 10.05, 10.08, 100), (2, 10.0, 9.98, 9.99, 100)], prev=10.4)
    assert [(k, r["at"].minute) for k, r in judge(DOWN, m)] == [("near", 1), ("touch", 2)]


def test_a_minute_that_reaches_the_line_is_a_touch_not_an_approach():
    assert [k for k, _ in judge(UP, day([(0, 10.01, 9.90, 9.95, 100)]))] == ["touch"]
    assert judge(UP, day([(0, 9.93, 9.90, 9.92, 100)]), near=False) == []
