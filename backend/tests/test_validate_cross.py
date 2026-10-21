"""越线买的日线近似 (design 2026-10-11 §6, §7): a stock in the 潜伏 list at the close is bought the next trading day
the first time its high reaches the box top the list watched — the top 盯盘 alerts at, read the evening before (a slanted
small box's a bar further along) — at the top or at an open already over it, never after a day it did not trade, never
from an evening whose close had already crossed it."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l3.rule import Plan
from wb.validate.cross import cross_signals

D = lambda i: date(2025, 1, 1) + timedelta(days=i)  # noqa: E731
CAL = [D(i) for i in range(10)]
LID = "CN:600000"
TOL = 1.015


def watch(*rows):
    """(day, top, rise, line_of) — a 潜伏 row of that evening: the box top (edge), how much it rises a day, its break line
    (top × 1.015, rising rise × 1.015), its stop line read then."""
    return pl.DataFrame([{"listing_id": LID, "date": D(d), "box": "b1", "edge": top, "rise": rise, "line": top * TOL,
                          "per_bar": rise * TOL, "line_of": of, "stop0": 9.0} for d, top, rise, of in rows])


def bars(days, o, h, c=None, adj=1.0):
    return pl.DataFrame({"listing_id": LID, "date": [D(d) for d in days], "open": [float(x) for x in o],
                         "high": [float(x) for x in h], "close": [float(x) for x in (c or o)], "adj": adj})


def cross(w, b, plan=Plan(entry="cross")):
    return cross_signals(w, b, plan, CAL)


def test_a_high_under_the_top_is_no_cross():
    b = bars([0, 1], [9.8, 9.9], [9.9, 9.99], [9.7, 9.9])
    assert cross(watch((0, 10.0, 0.0, "b1")), b).is_empty()


def test_an_open_under_the_top_fills_at_it_and_an_open_over_it_at_the_open():
    b = bars([0, 1], [9.8, 9.9], [9.9, 10.3], [9.7, 10.2])
    under = cross(watch((0, 10.0, 0.0, "b1")), b).row(0, named=True)
    assert (under["signal_date"], under["entry_price"], under["stop0"]) == (D(1), 10.0, 9.0)   # lines read the evening before
    assert under["gap"] == pytest.approx(9.9 / 10.0 - 1) and under["edge"] == 10.0 and under["line"] == pytest.approx(10.15)
    over = cross(watch((0, 10.0, 0.0, "b1")), bars([0, 1], [9.8, 10.1], [9.9, 10.3], [9.7, 10.2])).row(0, named=True)
    assert over["entry_price"] == 10.1 and over["gap"] == pytest.approx(10.1 / 10.0 - 1)


def test_closed_above_is_a_close_at_or_over_the_break_line():
    # 盘中越线不等于收盘站上 (选股流程 v1 第五步): over the top during the day is not over the break line at the close
    w = watch((0, 10.0, 0.0, "b1"))
    assert cross(w, bars([0, 1], [9.8, 9.9], [9.9, 10.3], [9.7, 10.1])).row(0, named=True)["closed_above"] is False
    assert cross(w, bars([0, 1], [9.8, 9.9], [9.9, 10.3], [9.7, 10.2])).row(0, named=True)["closed_above"] is True


def test_max_gap_leaves_an_open_too_far_over_the_top():
    w, b = watch((0, 10.0, 0.0, "b1")), bars([0, 1], [9.8, 10.5], [9.9, 10.6], [9.7, 10.5])
    assert cross(w, b, Plan(entry="cross", max_gap=0.03)).is_empty()
    assert cross(w, b, Plan(entry="cross", max_gap=0.06)).height == 1


def test_a_slanted_top_is_the_day_s_a_bar_further_along():
    s = cross(watch((0, 10.0, 0.05, "b1")), bars([0, 1], [9.8, 9.9], [9.9, 10.05], [9.7, 9.95]))
    assert s.row(0, named=True)["entry_price"] == pytest.approx(10.05) and s["edge"].to_list() == [pytest.approx(10.05)]
    assert cross(watch((0, 10.0, 0.05, "b1")), bars([0, 1], [9.8, 9.9], [9.9, 10.04], [9.7, 9.95])).is_empty()


def test_lines_are_adjusted_prices_and_fills_raw():
    # the structures are in adjusted prices (raw × factor); a day's raw top is the adjusted top ÷ that day's factor
    s = cross(watch((0, 20.0, 0.0, "b1")), bars([0, 1], [4.9, 9.9], [4.95, 10.3], [4.85, 10.2], adj=2.0))
    assert s.row(0, named=True)["entry_price"] == pytest.approx(10.0)


def test_a_day_the_stock_does_not_trade_is_no_cross_and_the_next_evening_s_row_counts():
    # in the list on day 0, halted on day 1 (the market traded): no cross on day 2 from day 0's row
    assert cross(watch((0, 10.0, 0.0, "b1")), bars([0, 2], [9.8, 9.9], [9.9, 10.3], [9.7, 10.2])).is_empty()
    s = cross(watch((0, 10.0, 0.0, "b1"), (2, 10.1, 0.0, "b1")),
              bars([0, 2, 3], [9.8, 9.9, 9.9], [9.9, 10.0, 10.3], [9.7, 9.95, 10.2]))
    assert s["signal_date"].to_list() == [D(3)] and s["entry_price"].to_list() == [10.1]


def test_an_evening_already_over_the_top_is_already_crossed():
    # 盯盘 (design 2026-10-10 §2): a box whose close is over its top (under the break line) has crossed — not watched the
    # next day, so no cross from that evening; the evening the close is back under the top counts again
    b = bars([0, 1, 2], [9.9, 10.05, 10.0], [10.1, 10.2, 10.3], [10.05, 9.9, 10.2])
    s = cross(watch((0, 10.0, 0.0, "b1"), (1, 10.0, 0.0, "b1")), b)
    assert s["signal_date"].to_list() == [D(2)] and s["entry_price"].to_list() == [10.0]


def test_a_line_is_crossed_once_its_first_day_whatever_came_of_it():
    # crossed on day 1 and back in the box at the close, still on the list: the line was crossed; a later day is not
    # another first time (one cross a line, as one signal a structure); a new box (another line) is
    w = watch((0, 10.0, 0.0, "b1"), (1, 10.0, 0.0, "b1"), (2, 10.3, 0.0, "b2"))
    s = cross(w, bars([0, 1, 2, 3], [9.8, 9.9, 9.9, 10.2], [9.9, 10.3, 10.3, 10.5], [9.7, 9.95, 9.95, 10.4]))
    assert s.select("signal_date", "line_of").rows() == [(D(1), "b1"), (D(3), "b2")]
