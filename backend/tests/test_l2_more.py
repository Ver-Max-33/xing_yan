from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from tests.bars import bars
from tests.test_l2_derived import boxes
from wb.engine.l2.more import left_pressure, macd_divergences, swings

D = lambda i: date(2024, 1, 1) + timedelta(days=i)


def test_swings_alternate_and_are_known_at_confirmation():
    path = [10, 10.5, 11, 11.5, 12, 11.4, 10.8, 10.2, 10.6, 11.1, 11.7, 12.3, 12.9]
    s = swings(bars(path), threshold=0.05)
    # the start is a low (confirmed once price is 5% above it), then the top at bar 4, the low at bar 7;
    # the last leg up is not a swing yet: its top is not confirmed
    assert s["direction"].to_list() == ["up", "down"]
    down = s.row(1, named=True)
    assert down["start"] == D(4) and down["end"] == D(7)
    assert down["known_at"] == D(9)                                 # 5% off the low first reached on bar 9
    assert down["change"] == pytest.approx((10.2 * 0.996) / (12 * 1.004) - 1)


def test_bullish_divergence_lower_low_higher_dif():
    piv = pl.DataFrame({"listing_id": "CN:600000", "idx": [3, 10], "date": [D(3), D(10)], "kind": ["low", "low"],
                        "price": [9.0, 8.8], "known_at": [D(5), D(12)]})
    dif = np.full(15, 0.0)
    dif[3], dif[10] = -0.30, -0.12
    b = pl.DataFrame({"listing_id": "CN:600000", "date": [D(i) for i in range(15)], "dif": dif})
    d = macd_divergences(b, piv)
    assert d["kind"].to_list() == ["bull"] and d["known_at"][0] == D(12)


def at(*days):
    return pl.DataFrame({"listing_id": ["CN:600000"] * len(days), "date": [D(d) for d in days]})


def test_left_pressure_is_the_nearest_ended_box_above_the_close():
    b = boxes(("A", "mid", D(0), D(25), D(40), D(45), "下破离箱", 12.0, 11.0),   # above: pressure
              ("B", "mid", D(50), D(70), D(80), D(85), "下破离箱", 14.0, 13.0),   # higher, farther
              ("C", "mid", D(90), D(110), None, None, None, 10.2, 9.6))          # the current box
    closes = [11.5] * 20 + [11.2] + [11.5] * 24 + [10.5] * 45 + [10.0] * 30
    df = bars(closes).with_columns(pl.Series("date", [D(i) for i in range(len(closes))]),
                                   pl.Series("volume", [3000.0 if i == 20 else 1000.0 for i in range(len(closes))]))
    p = left_pressure(b, df, at(119)).row(0, named=True)
    assert p["pressure_box_id"] == "A" and p["space"] == pytest.approx(11.0 / 10.0 - 1)
    # 41 bars thick; its one down bar (bar 20) has 3000 against a mean of 43000 / 41
    assert p["pressure_bars"] == 41 and p["pressure_heavy_down"] == pytest.approx(3000 / (43000 / 41))
    assert p["one_year_high"] is None  # fewer than a year of bars: unknown, not a number


def flat(n=100):
    return bars([10.0] * n).with_columns(pl.Series("date", [D(i) for i in range(n)]))


def test_a_close_back_inside_a_box_it_fell_out_of_has_no_space_left():
    b = boxes(("X", "mid", D(0), D(25), D(40), D(45), "下破离箱", 10.6, 9.8),      # fell out of it, back inside
              ("A", "mid", D(50), D(70), D(80), D(85), "下破离箱", 12.0, 11.0))
    p = left_pressure(b, flat(), at(99)).row(0, named=True)
    assert p["pressure_box_id"] == "X" and p["space"] == 0.0                       # buying there, it is all trapped supply


def test_a_box_the_price_still_ranges_in_is_not_left_pressure():
    # boxes cut for length or left upward that hold the close are the current range, not trapped supply
    b = boxes(("L", "short", D(60), D(70), D(85), D(85), "超长", 10.3, 9.7),
              ("U", "mid", D(10), D(20), D(40), D(45), "上破离箱", 10.4, 9.9),
              ("A", "mid", D(50), D(55), D(58), D(60), "下破离箱", 12.0, 11.0))
    p = left_pressure(b, flat(), at(99)).row(0, named=True)
    assert p["pressure_box_id"] == "A" and p["space"] == pytest.approx(0.10)


def test_equally_near_boxes_report_the_longer_one():
    b = boxes(("S", "short", D(30), D(35), D(40), D(45), "下破离箱", 12.0, 11.0),
              ("M", "mid", D(10), D(25), D(40), D(45), "下破离箱", 12.5, 11.0))
    p = left_pressure(b, flat(), at(99)).row(0, named=True)
    assert p["pressure_box_id"] == "M" and p["pressure_bars"] == 31


def test_pressure_is_seen_as_of_each_day_within_the_look_back():
    b = boxes(("A", "mid", D(0), D(25), D(40), D(45), "下破离箱", 12.0, 11.0),
              ("B", "mid", D(250), D(270), D(280), D(290), "下破离箱", 13.0, 12.5))
    df = bars([10.0] * 300).with_columns(pl.Series("date", [D(i) for i in range(300)]),
                                         pl.Series("high", [10.0 + i / 1000 for i in range(300)]))
    p = left_pressure(b, df, at(44, 100, 299), lookback=200).sort("date")
    # on 44 A has not ended; on 299 A (ended on bar 40) is more than 200 bars back
    assert p["pressure_box_id"].to_list() == [None, "A", "B"]
    assert p["half_year_high"].to_list() == [None, None, pytest.approx(10.298)]   # the 120 bars before the day
    assert p["one_year_high"].to_list() == [None, None, pytest.approx(10.298)]
