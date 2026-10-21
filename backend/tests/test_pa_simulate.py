"""价格行为分析 (design 2026-10-20 §5.2): one plan settled by A-share rules — a waiting order for three of the stock's own
days, a market buy only at the start day's open, T+1, the stop before the target on the same day, a sealed limit-down
holding the stop to the next open, 20 days at most, lines in adjusted prices and fills at that day's real one."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.pa.settle import net_return, simulate

D0 = date(2026, 9, 30)                       # day 0, a Wednesday; the plan starts at the next open
START = date(2026, 10, 1)
LIMIT = {"order_type": "限价单", "order_direction": "做多", "entry_price": 9.5, "stop_loss_price": 9.0,
         "take_profit_price": 10.0, "take_profit_price_2": 10.5}
BREAKOUT = {**LIMIT, "order_type": "突破单", "entry_price": 11.0, "stop_loss_price": 10.0, "take_profit_price": 12.0,
            "take_profit_price_2": 13.0}
MARKET = {**LIMIT, "order_type": "市价单", "entry_price": 10.0, "stop_loss_price": 9.5, "take_profit_price": 10.5,
          "take_profit_price_2": 11.0}
DAY0 = (10.0, 10.0, 10.0, 10.0)
FILL = (9.6, 9.7, 9.4, 9.6)                  # a limit buy at 9.50 fills, nothing else happens


def bars(rows) -> pl.DataFrame:
    """The listing's rows from day 0 on, consecutive weekdays: (open, high, low, close[, {adj, up, down, date}])."""
    out, d = [], D0
    for o, h, lo, c, *more in rows:
        x = more[0] if more else {}
        d = x.get("date", d)
        out.append({"date": d, "open": o, "high": h, "low": lo, "close": c, "adj": x.get("adj", 1.0),
                    "up": x.get("up", 99.0), "down": x.get("down", 0.01)})
        d += timedelta(days=3 if d.weekday() == 4 else 1)
    return pl.DataFrame(out)


def sim(plan, rows, target="tp1", live=True, **kw):
    return simulate(plan, bars(rows), day0=D0, start=START, target=target, live=live, **kw)


def test_a_limit_buy_fills_on_the_dip_and_takes_profit_next_day():
    t = sim(LIMIT, [DAY0, (10.0, 10.1, 9.4, 9.6), (9.7, 10.05, 9.6, 10.0)])
    assert (t.status, t.entry_price, t.exit_price, t.reason, t.days) == ("done", 9.5, 10.0, "tp1", 2)
    assert t.entry_date == START and t.r == pytest.approx(1.0) and not t.at_open
    assert sim(LIMIT, [DAY0, (10.0, 10.1, 9.4, 9.6), (9.7, 10.05, 9.6, 10.0)], target="tp2", live=True).status == "holding"


def test_t_plus_1_no_sale_on_the_buy_day_even_through_the_stop():
    t = sim(LIMIT, [DAY0, (9.6, 9.7, 8.8, 8.9), (8.9, 9.0, 8.7, 8.8)])
    assert t.reason == "stop" and t.exit_date == date(2026, 10, 2) and t.at_open and t.exit_price == 8.9  # open 8.9 ≤ stop 9.0


def test_a_day_touching_stop_and_target_counts_as_stop():
    t = sim(LIMIT, [DAY0, FILL, (9.6, 10.2, 8.9, 9.5)])
    assert (t.reason, t.exit_price, t.r) == ("stop", 9.0, pytest.approx(-1.0))
    assert sim(LIMIT, [DAY0, FILL, (10.1, 10.3, 9.9, 10.2)]).exit_price == 10.1                 # opened above the target


def test_a_stop_on_a_sealed_limit_down_day_waits_for_the_next_open():
    t = sim(LIMIT, [DAY0, FILL, (8.6, 8.6, 8.6, 8.6, {"down": 8.6}), (8.3, 8.6, 8.2, 8.4)])
    assert (t.reason, t.exit_date, t.exit_price, t.at_open, t.days) == ("stop", date(2026, 10, 5), 8.3, True, 3)


def test_breakout_not_filled_at_limit_up_or_on_a_one_price_day_and_window_of_three():
    sealed = (11.0, 11.0, 11.0, 11.0, {"up": 11.0})
    assert sim(BREAKOUT, [DAY0, sealed, (10.5, 10.8, 10.4, 10.7), (10.6, 10.9, 10.5, 10.8)]).status == "not_bought"
    t = sim(BREAKOUT, [DAY0, (10.5, 10.8, 10.4, 10.7), (10.9, 11.2, 10.8, 11.1), (11.1, 11.3, 11.0, 11.2)])
    assert (t.status, t.entry_date, t.entry_price) == ("holding", date(2026, 10, 2), 11.0)
    assert sim(BREAKOUT, [DAY0, (10.5, 10.8, 10.4, 10.7), (11.3, 11.5, 11.2, 11.4)]).entry_price == 11.3   # gapped above
    late = [DAY0, (10.5, 10.6, 10.4, 10.5), (10.5, 10.6, 10.4, 10.5), (10.5, 10.6, 10.4, 10.5), (10.9, 11.2, 10.8, 11.1)]
    assert sim(BREAKOUT, late).status == "not_bought"                             # the 4th day is past the window


def test_market_order_buys_the_start_open_but_not_a_limit_up_open_or_after_a_halt():           # Review Focus 5
    t = sim(MARKET, [DAY0, (10.1, 10.3, 9.9, 10.2), (10.2, 10.6, 10.1, 10.5)])
    assert (t.status, t.entry_price, t.reason, t.exit_price) == ("done", 10.1, "tp1", 10.5)
    assert sim(MARKET, [DAY0, (11.0, 11.0, 10.6, 10.8, {"up": 11.0})]).status == "not_bought"
    assert sim(MARKET, [DAY0, (10.1, 10.3, 9.9, 10.2, {"date": date(2026, 10, 2)})]).status == "not_bought"   # halted on the 1st


def test_an_ex_date_inside_the_window_compares_in_adjusted_prices():                          # Review Focus 5
    t = sim(LIMIT, [DAY0, (8.0, 8.1, 7.5, 7.9, {"adj": 1.25}), (7.9, 8.1, 7.8, 8.0, {"adj": 1.25})])  # 7.6 raw × 1.25 = 9.5
    assert t.entry_price == 7.6 and t.entry_adj == 9.5 and t.exit_price == 8.0 and t.reason == "tp1"    # 8.0 × 1.25 = 10.0


def test_void_waiting_holding_gone_and_twenty_days():
    assert sim(BREAKOUT, [DAY0, (10.5, 10.6, 9.9, 10.2)]).status == "void"                   # the stop came first
    assert sim(BREAKOUT, [DAY0, (10.5, 10.8, 10.4, 10.7)]).status == "waiting"
    assert sim(BREAKOUT, [DAY0, (10.5, 10.8, 10.4, 10.7)], live=False).status == "not_bought"    # it stopped trading
    assert sim(LIMIT, [DAY0, FILL, (9.6, 9.8, 9.3, 9.7)]).status == "holding"
    t = sim(LIMIT, [DAY0, FILL, (9.6, 9.8, 9.3, 9.7)], live=False)
    assert (t.status, t.reason, t.exit_price, t.days) == ("gone", "gone", 9.7, 2)
    t = sim(LIMIT, [DAY0, FILL] + [(9.6, 9.8, 9.3, 9.7)] * 19)
    assert (t.status, t.reason, t.exit_price, t.days) == ("done", "days", 9.7, 20)
    assert sim({**LIMIT, "order_direction": "做空"}, [DAY0]).status == "short"
    assert sim({**LIMIT, "order_type": "不下单"}, [DAY0]).status == "no_plan" and sim(None, [DAY0]).status == "no_plan"
    assert sim({**LIMIT, "entry_price": None}, [DAY0, FILL]).status == "no_plan"            # an order without its price


def test_net_return_charges_costs():
    t = sim(LIMIT, [DAY0, (10.0, 10.1, 9.4, 9.6), (9.7, 10.05, 9.6, 10.0)])
    bench = pl.DataFrame({"date": [D0, START, date(2026, 10, 2)], "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    got = net_return([("k", t, "cross"), ("w", sim(BREAKOUT, [DAY0, (10.5, 10.8, 10.4, 10.7)]), "cross")], bench)
    assert got["k"] < 10.0 / 9.5 - 1 and got["k"] > 10.0 / 9.5 - 1 - 0.005 and got["w"] is None


def test_a_plan_that_ended_unbought_has_its_end_day():                          # final review, Important 8
    assert (lambda t: (t.status, t.exit_date))(sim(BREAKOUT, [DAY0, (10.5, 10.6, 9.9, 10.2)])) == ("void", START)
    quiet = (10.5, 10.6, 10.4, 10.5)
    t = sim(BREAKOUT, [DAY0, quiet, quiet, quiet])
    assert (t.status, t.exit_date) == ("not_bought", date(2026, 10, 5))          # the window's last day
    t = sim(MARKET, [DAY0, (10.1, 10.3, 9.9, 10.2, {"date": date(2026, 10, 2)})])
    assert (t.status, t.exit_date) == ("not_bought", START)                       # halted on the day it would have bought
    assert sim(BREAKOUT, [DAY0, quiet]).exit_date is None                         # still waiting
