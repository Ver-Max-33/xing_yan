"""价格行为分析 (design 2026-10-20 §3.3, §4): the 100 daily bars PA reads — 前复权 anchored on day 0 in cents with the
exchange's own volume, 涨跌停 marks from the raw prices, the next trading day's limits from the stock's own ratio, and
the workbench's previous plan on the frame."""
import polars as pl
import pytest

from tests.pa_fixtures import bar_on, pa_market
from wb.data.store import Store
from wb.pa.frame import NotEnoughBars, PreviousPlan, daily_input, limit_mark, next_limits, with_previous


def test_100_bars_qfq_on_day_0_in_cents_and_raw_volume(paths):
    pa_market(paths)
    raw = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == "CN:600000").sort("date")
    inp = daily_input(paths, "CN:600000")
    f = inp.frame
    assert len(f.bars) == 100 and f.timeframe == "1d" and f.symbol == "600000" and inp.day == raw["date"][-1]
    assert f.bars[0].close == inp.k1_close == raw["close"][-1]                       # anchored: K1 is the real price
    before = raw.row(119, named=True)                                                  # the last bar before the ex-date
    assert bar_on(f, before["date"]).close == round(before["close"] / 1.25, 2)        # older bars scaled down
    assert all(round(b.close, 2) == b.close for b in f.bars) and f.bars[0].volume == raw["volume"][-1]
    assert f.limit_marks[4] == "涨停" and f.limit_marks[2] == "一字涨停" and len(f.limit_marks) == 100


def test_next_day_limits_on_the_beijing_exchange_round_toward_the_close():
    # 北交所 (the data, 920000 on 2025-01-10, 前收 19.16: 24.90 / 13.42): limit-up floored, limit-down ceiled — so in all of
    # its 125,770 days at 30% in 2025–2026, where half up missed half of them (24.91 / 13.41 here)
    assert next_limits("920000", 19.16, 24.9, 19.16) == (24.9, 13.42)
    assert next_limits("830799", 19.16, None, None) == (24.9, 13.42)         # no data: the board's 30%, the same rounding


def test_next_day_limits_follow_the_stocks_own_ratio_and_round_half_up():
    assert next_limits("600000", 10.05, 11.0, 10.0) == (11.06, 9.05)          # 10.05 × 1.1 = 11.055 → 11.06
    assert next_limits("300001", 10.0, None, None) == (12.0, 8.0)             # no data: the board's 20%
    assert next_limits("600001", 10.0, 10.5, 10.0) == (10.5, 9.5)             # an ST-style 5% ratio in the data


def test_limit_marks():
    assert limit_mark(11.0, 11.0, 10.2, 11.0, 9.0) == "涨停" and limit_mark(11.0, 11.0, 11.0, 11.0, 9.0) == "一字涨停"
    assert limit_mark(9.0, 9.5, 9.0, 11.0, 9.0) == "跌停" and limit_mark(10.0, 10.5, 9.5, 11.0, 9.0) == ""


def test_a_stock_with_99_bars_is_not_enough(paths):
    pa_market(paths, bars=99)
    with pytest.raises(NotEnoughBars, match="只有 99 根"):
        daily_input(paths, "CN:600000")


def test_the_previous_plan_rides_on_the_frame(paths):
    pa_market(paths)
    prev = PreviousPlan("p1", "2026-09-28T20:00:00+08:00", {"order_type": "限价单", "order_direction": "做多",
                        "entry_price": 9.5, "stop_loss_price": 9.0, "take_profit_price": 10.0, "invalidation_condition": "x"}, 2)
    f = with_previous(daily_input(paths, "CN:600000"), prev).frame
    assert f.previous_plan_known and f.previous_plan_bars == 2 and dict(f.previous_plan)["entry_price"] == 9.5
    assert daily_input(paths, "CN:600000").frame.previous_plan == ()               # known: there is none


def test_next_day_limits_of_a_stock_under_one_yuan():                       # final review (Minor 1, promoted)
    assert next_limits("600000", 0.45, 0.50, 0.45) == (0.50, 0.41)          # 0.50 ÷ 0.45 is 11% up: the rule's 10%
    assert next_limits("600000", 0.45, 0.47, 0.45) == (0.47, 0.43)          # 4.4%: an ST stock's 5%
