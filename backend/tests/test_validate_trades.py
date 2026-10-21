"""交易计划的撮合 (design 2026-10-09 §5): each signal traded by its plan on its stock's own daily bars — the order of a
day (the open: sales waiting and targets at or under it; the day: targets reached; the close: 失效线, 卖出条件, 不创新高,
到期), T+1, the limits, a halt, the data's end and a stock that stopped trading — on hand-made bars, one rule a test."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l3.rule import Plan, PlanExit, PlanLine
from wb.validate.trades import trade

D = lambda i: date(2025, 1, 1) + timedelta(days=i)  # noqa: E731
LID = "CN:600000"


def bars(o, h, l, c, *, up=None, dn=None, flags=None, live=True, adj=None):  # noqa: E741
    n = len(c)
    o, h, l, c = ([float(x) for x in v] for v in (o, h, l, c))  # noqa: E741
    return pl.DataFrame({"listing_id": LID, "date": [D(i) for i in range(n)], "open": o, "high": h, "low": l, "close": c,
                         "adj": [float(x) for x in adj or [1.0] * n],
                         "up": [float(x) for x in up or [round(x * 1.1, 2) for x in c[:1] + c[:-1]]],
                         "down": [float(x) for x in dn or [round(x * 0.9, 2) for x in c[:1] + c[:-1]]], "ul": False, "ll": False, "live": live,
                         "exit0": flags or [False] * n})


def sig(day=0, stop=None, take=None, next_ok=True):
    return pl.DataFrame({"sid": [0], "listing_id": [LID], "signal_date": [D(day)], "next_ok": [next_ok], "stop0": [stop],
                         "take0": [take]}, schema_overrides={"stop0": pl.Float64, "take0": pl.Float64})


def flat(n, px=10.0):
    return [px] * n


def one(signals, b, plan, t_plus_1=True, relative=False):
    return trade(signals, b, plan, t_plus_1=t_plus_1, relative=relative).row(0, named=True)


def sold(r):
    return [(f["date"], round(f["price"], 4), round(f["share"], 4), f["why"]) for f in r["fills"]]


CLOSE = Plan(entry="close", days=60)
OPEN = Plan(entry="open", days=60)


def test_a_close_entry_buys_the_signal_close_and_an_open_entry_the_next_open():
    b = bars([10, 10.2, 10.4, 10.6], [10.1, 10.3, 10.5, 10.7], [9.9, 10.1, 10.3, 10.5], [10, 10.2, 10.4, 10.6])
    r = one(sig(), b, Plan(entry="close", days=2))
    assert (r["status"], r["entry_date"], r["entry_price"]) == ("done", D(0), 10.0)
    assert sold(r) == [(D(2), 10.4, 1.0, "days")] and r["held"] == 2           # the close of the 2nd day after the signal
    r = one(sig(), b, Plan(entry="open", days=2))
    assert (r["entry_date"], r["entry_price"]) == (D(1), 10.2) and sold(r) == [(D(2), 10.4, 1.0, "days")]


def test_a_close_under_the_stop_sells_at_the_close_or_the_next_open():
    c = [10, 10, 9.4, 9.0, 9.0]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    assert sold(one(sig(stop=9.5), b, Plan(entry="close", stop=[PlanLine(line="x")], fill="close"))) == [(D(2), 9.4, 1.0, "stop0")]
    assert sold(one(sig(stop=9.5), b, Plan(entry="close", stop=[PlanLine(line="x")], fill="open"))) == [(D(3), 9.0, 1.0, "stop0")]


def test_t_plus_1_an_open_entry_s_first_close_under_the_stop_sells_the_next_open():
    o, c = [10, 10, 9.6, 9.5], [10, 9.4, 9.5, 9.6]
    b = bars(o, [10.1, 10.1, 9.7, 9.7], [9.9, 9.3, 9.4, 9.4], c)
    r = one(sig(stop=9.5), b, Plan(entry="open", stop=[PlanLine(line="x")], fill="close"))
    assert r["entry_date"] == D(1) and sold(r) == [(D(2), 9.6, 1.0, "stop0")]    # bought on day 1: sold at day 2's open


def test_a_target_sells_its_share_at_the_open_above_it_or_at_the_line():
    b = bars([10, 10.0, 11.5], [10.1, 10.6, 11.6], [9.9, 9.9, 11.4], [10, 10.0, 11.5])
    p = Plan(entry="close", take=[PlanLine(line="x", share=0.5)], days=2)
    assert sold(one(sig(take=10.5), b, p)) == [(D(1), 10.5, 0.5, "take0"), (D(2), 11.5, 0.5, "days")]
    b = bars([10, 10.8, 11.0], [10.1, 10.9, 11.1], [9.9, 10.7, 10.9], [10, 10.8, 11.0])
    assert sold(one(sig(take=10.5), b, p))[0] == (D(1), 10.8, 0.5, "take0")   # opened over it: at the open


def test_a_target_reached_on_an_open_entry_s_first_day_sells_the_next_open():
    b = bars([10, 10.0, 10.3], [10.1, 10.6, 10.4], [9.9, 9.9, 10.2], [10, 10.1, 10.3])
    r = one(sig(take=10.5), b, Plan(entry="open", take=[PlanLine(line="x")]))
    assert r["entry_date"] == D(1) and sold(r) == [(D(2), 10.3, 1.0, "take0")]


def test_a_day_reaching_the_target_and_closing_under_the_stop_takes_the_target_first():
    b = bars([10, 10.0], [10.1, 10.6], [9.9, 9.2], [10, 9.3])
    p = Plan(entry="close", stop=[PlanLine(line="x")], take=[PlanLine(line="x", share=0.5)], fill="close")
    assert sold(one(sig(stop=9.5, take=10.5), b, p)) == [(D(1), 10.5, 0.5, "take0"), (D(1), 9.3, 0.5, "stop0")]


def test_two_items_on_one_close_never_sell_more_than_is_left():
    c = [10, 10.0, 9.4]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c, flags=[False, False, True])
    p = Plan(entry="close", stop=[PlanLine(line="x")], exit=[PlanExit(when="x", share=0.5)], fill="close")
    r = one(sig(stop=9.5), b, p)
    assert sold(r) == [(D(2), 9.4, 1.0, "stop0")] and r["status"] == "done"


def test_an_exit_sells_its_share_and_the_rest_waits():
    c = [10, 10.5, 10.4, 10.6, 10.7]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c, flags=[False, False, True, False, False])
    r = one(sig(), b, Plan(entry="close", exit=[PlanExit(when="x", share=0.5)], days=4))
    assert sold(r) == [(D(2), 10.4, 0.5, "exit0"), (D(4), 10.7, 0.5, "days")]



def test_the_last_day_sells_at_its_close_whatever_the_fill():
    # the days-th day is known before its close: sold at that close, not the next open (fill is for what only the close says)
    c = [10, 10.2, 10.4, 10.6]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    assert sold(one(sig(), b, Plan(entry="close", days=2, fill="open"))) == [(D(2), 10.4, 1.0, "days")]

def test_stall_sells_after_that_many_closes_without_a_new_high_since_entry():
    c = [10, 10.5, 10.4, 10.3, 10.45, 10.6]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    assert sold(one(sig(), b, Plan(entry="close", stall=3))) == [(D(4), 10.45, 1.0, "stall")]


def test_a_limit_up_buy_is_not_filled_and_a_halt_is_not_bought():
    b = bars([10, 11.0], [10, 11.0], [10, 11.0], [10, 11.0], up=[10.0, 11.0])
    assert one(sig(), b, CLOSE)["status"] == "limit"                 # the signal day closed at its limit up
    assert one(sig(), b, OPEN)["status"] == "limit"                  # opened at the limit up
    b = bars([10, 10.2], [10.1, 10.3], [9.9, 10.1], [10, 10.2])
    r = one(sig(next_ok=False), b, OPEN)
    assert r["status"] == "halt" and r["fills"] == []                # not traded on the market's next day: never bought later


def test_a_sale_at_the_limit_down_waits_for_the_next_open_and_again_at_a_limit_down_open():
    c = [10, 9.0, 8.1, 8.0]
    b = bars([10, 9.5, 8.1, 8.3], [10.1, 9.6, 8.1, 8.4], [9.9, 9.0, 8.1, 7.9], c, dn=[9.0, 9.0, 8.1, 7.29])
    r = one(sig(stop=9.5), b, Plan(entry="close", stop=[PlanLine(line="x")], fill="close"))
    assert sold(r) == [(D(3), 8.3, 1.0, "stop0")]                    # day 1 closed at the limit down, day 2 opened there


def test_the_data_s_end_with_shares_left_is_open_and_a_stock_that_stopped_trading_is_sold_at_its_last_close():
    c = [10, 10.2, 10.4]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    assert one(sig(), b, CLOSE)["status"] == "open"
    r = one(sig(), bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c, live=False), CLOSE)
    assert r["status"] == "gone" and sold(r) == [(D(2), 10.4, 1.0, "gone")]


def test_a_stop_above_the_entry_sells_at_the_first_close_under_it():
    c = [10, 10.1, 10.2]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    assert sold(one(sig(stop=10.5), b, Plan(entry="close", stop=[PlanLine(line="x")], fill="close"))) == [(D(1), 10.1, 1.0, "stop0")]


def test_a_target_not_above_the_entry_is_no_target():
    c = [10, 10.1, 10.2, 10.3]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    r = one(sig(take=9.8), b, Plan(entry="close", take=[PlanLine(line="x")], days=3))
    assert sold(r) == [(D(3), 10.3, 1.0, "days")] and r["no_room"] == 1


def test_relative_lines_are_shares_of_the_entry_price():
    c = [10, 10.0, 9.4, 9.0]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c)
    r = one(sig(stop=0.95), b, Plan(entry="close", stop=[PlanLine(line="x")], fill="close"), relative=True)
    assert sold(r) == [(D(2), 9.4, 1.0, "stop0")]                     # 95% of 10


def test_adjusted_prices_judge_the_lines_and_raw_prices_fill():
    # an ex-dividend day halves the raw price; the line is in adjusted prices (raw × factor) and does not fire
    c = [10, 10.2, 5.2, 5.3]
    b = bars(c, [x + 0.1 for x in c], [x - 0.1 for x in c], c, adj=[1.0, 1.0, 2.0, 2.0],
             up=[11.0, 11.0, 11.22, 5.72], dn=[9.0, 9.0, 4.59, 4.68])
    r = one(sig(stop=9.5), b, Plan(entry="close", stop=[PlanLine(line="x")], days=3, fill="close"))
    assert sold(r) == [(D(3), 5.3, 1.0, "days")]


def test_the_best_and_worst_move_are_over_the_holding_days_against_the_entry():
    c = [10, 10.5, 9.8, 10.2]
    b = bars(c, [10.1, 11.0, 9.9, 10.3], [9.0, 10.4, 9.5, 10.1], c)
    r = one(sig(), b, Plan(entry="close", days=3))
    assert r["mfe"] == pytest.approx(0.10) and r["mae"] == pytest.approx(-0.05)   # the signal day's own low not counted


def test_returns_are_net_of_each_day_s_costs_and_the_benchmark_is_read_at_the_same_times():
    # bought at 10 on 2023-08-01's close; half out at 11 (a target), half at 9 at a close: stamp duty 0.1% then
    from wb.validate.portfolio import COSTS
    from wb.validate.trades import returns
    d0 = date(2023, 8, 1)
    days = [d0 + timedelta(days=i) for i in range(4)]
    b = pl.DataFrame({"listing_id": LID, "date": days, "open": [10.0, 10.5, 10.0, 9.5], "high": [10.1, 11.2, 10.1, 9.6],
                      "low": [9.9, 10.4, 8.9, 9.4], "close": [10.0, 10.6, 9.0, 9.5], "adj": 1.0, "up": 11.0, "down": 8.5,
                      "ul": False, "ll": False, "live": True, "exit0": False})
    s = pl.DataFrame({"sid": [0], "listing_id": [LID], "signal_date": [d0], "next_ok": [True], "stop0": [9.5], "take0": [11.0]})
    p = Plan(entry="close", stop=[PlanLine(line="x")], take=[PlanLine(line="x", share=0.5)], fill="close")
    t = trade(s, b, p, t_plus_1=True)
    bench = pl.DataFrame({"date": days, "open": [2990.0, 3000.0, 3010.0, 3020.0], "close": [3000.0, 3030.0, 3030.0, 3030.0]})
    r = returns(t, bench, COSTS["CN"], "close").row(0, named=True)
    buy, sell = 0.00025 + 0.00001 + 0.001, 0.00025 + 0.00001 + 0.001 + 0.001
    assert r["ret"] == pytest.approx((0.5 * 11 * (1 - sell) + 0.5 * 9 * (1 - sell)) / (10 * (1 + buy)) - 1)
    assert r["bench_ret"] == pytest.approx(0.01) and r["excess"] == pytest.approx(r["ret"] - 0.01)
    assert r["exit_date"] == days[2]



CROSS = Plan(entry="cross", days=60)


def at(price, day=0, stop=None, take=None):
    return sig(day, stop, take).with_columns(pl.lit(price).alias("entry_price"))


def test_a_cross_entry_buys_at_its_price_on_the_signal_bar_and_sells_from_the_next():
    # design 2026-10-11 §7: bought during the day the price reached the line; T+1, so the first sale is the next bar's
    o, c = [10, 10.0, 9.4, 9.3], [10, 9.4, 9.3, 9.2]
    b = bars(o, [10.1, 10.6, 9.5, 9.4], [9.9, 9.3, 9.2, 9.1], c)
    r = one(at(10.5, day=1, stop=9.5), b, Plan(entry="cross", stop=[PlanLine(line="x")], fill="close"))
    assert (r["status"], r["entry_date"], r["entry_price"]) == ("done", D(1), 10.5)
    assert sold(r) == [(D(2), 9.4, 1.0, "stop0")]                                  # its own close under the stop: next open
    r = one(at(10.5, day=1), b, Plan(entry="cross", days=2))
    assert sold(r) == [(D(3), 9.2, 1.0, "days")] and r["held"] == 2               # days counted from the cross day


def test_a_cross_at_or_over_the_limit_up_is_not_bought_nor_chased():
    b = bars([10, 10.5, 11.0], [10.1, 11.0, 11.2], [9.9, 10.4, 10.8], [10, 11.0, 11.0], up=[11.0, 11.0, 12.1])
    assert one(at(11.0, day=1), b, CROSS)["status"] == "limit"                     # the line at the limit up
    r = one(at(10.6, day=1), b, Plan(entry="cross", days=1))
    assert (r["status"], r["entry_price"], sold(r)) == ("done", 10.6, [(D(2), 11.0, 1.0, "days")])
    assert one(at(10.6, day=5), b, CROSS)["status"] == "halt"                      # no bar that day: never bought later


def test_a_cross_entry_s_target_reached_the_same_day_sells_the_next_open():
    b = bars([10, 10.0, 10.9], [10.1, 11.2, 11.0], [9.9, 9.9, 10.8], [10, 10.4, 10.9])
    r = one(at(10.5, day=1, take=11.0), b, Plan(entry="cross", take=[PlanLine(line="x")]))
    assert sold(r) == [(D(2), 10.9, 1.0, "take0")] and r["mfe"] == pytest.approx(11.2 / 10.5 - 1)



def test_a_cross_entry_s_benchmark_is_bought_at_the_index_s_average_of_the_day():
    # daily bars do not say when the line was reached: the index is bought at its (open + high + low + close) ÷ 4
    from wb.validate.portfolio import COSTS
    from wb.validate.trades import returns
    d0 = date(2023, 8, 1)
    days = [d0 + timedelta(days=i) for i in range(3)]
    b = pl.DataFrame({"listing_id": LID, "date": days, "open": [10.0, 10.5, 10.6], "high": [10.6, 10.7, 10.8],
                      "low": [9.9, 10.4, 10.5], "close": [10.4, 10.6, 10.7], "adj": 1.0, "up": 11.5, "down": 9.0,
                      "ul": False, "ll": False, "live": True, "exit0": False})
    t = trade(at(10.2).with_columns(pl.lit(d0).alias("signal_date")), b, Plan(entry="cross", days=2), t_plus_1=True)
    bench = pl.DataFrame({"date": days, "open": [2990.0, 3000.0, 3010.0], "high": [3010.0, 3020.0, 3030.0],
                          "low": [2980.0, 2990.0, 3000.0], "close": [3000.0, 3010.0, 3020.0]})
    r = returns(t, bench, COSTS["CN"], "cross").row(0, named=True)
    assert r["bench_ret"] == pytest.approx(3020.0 / ((2990 + 3010 + 2980 + 3000) / 4) - 1)