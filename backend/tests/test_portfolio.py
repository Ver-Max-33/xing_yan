"""组合回测 (spec §9.2): a long-only book bought at the open after each signal — never at the signal day's close — in
lots, equal weight up to a number of positions, with each market's rules (CN: T+1 and the stk_limit prices; JP: a
UL / LL open), costs and slippage kept apart, dividends and splits booked from the factors, and a cash ledger that
balances every day."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.validate.portfolio import COSTS, Costs, Settings, simulate

D0 = date(2026, 3, 2)   # a Monday
DAYS = [D0 + timedelta(days=k) for k in range(40) if (D0 + timedelta(days=k)).weekday() < 5]
BARS = {"listing_id": pl.Utf8, "date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
        "adj_factor": pl.Float64, "share_factor": pl.Float64, "up_limit": pl.Float64, "down_limit": pl.Float64,
        "ul": pl.Boolean, "ll": pl.Boolean}


def bars(lid, rows, adj=1.0, share=1.0, limits=True):
    """rows: (day index, open, high, low, close[, adj, share]) — CN limits ±10% of the previous close when `limits`."""
    out, prev = [], None
    for r in rows:
        k, o, h, lo, c = r[:5]
        a, s = (r[5], r[6]) if len(r) > 5 else (adj, share)
        up = round(prev * 1.1, 2) if limits and prev else None
        dn = round(prev * 0.9, 2) if limits and prev else None
        out.append((lid, DAYS[k], o, h, lo, c, a, s, up, dn, False, False))
        prev = c
    return pl.DataFrame(out, schema=BARS, orient="row")


def flat(lid, n=30, px=10.0, start=0):
    return bars(lid, [(k, px, px * 1.01, px * 0.99, px) for k in range(start, n)])


def signals(rows):
    """rows: (listing_id, signal day index, box bottom (adjusted) or None, rank or None)"""
    return pl.DataFrame([(lid, DAYS[k], stop, rank) for lid, k, stop, rank in rows],
                        schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "stop": pl.Float64, "rank": pl.Int64}, orient="row")


NO_COST = Costs()


def run(sig, b, **kw):
    s = Settings(**{"cash": 100_000.0, "costs": NO_COST, "max_positions": 2, "hold": None, "box_stop": False, **kw})
    return simulate(sig, b, DAYS[:30], s)


def balanced(out, start=100_000.0):
    led, nav = out["ledger"], out["nav"]
    assert start + led["amount"].sum() == pytest.approx(nav["cash"][-1], abs=1e-6)        # every cash move is in the ledger
    assert (nav["nav"] - nav["cash"] - nav["holdings"]).abs().max() < 1e-6                  # NAV = cash + holdings, each day


def test_bought_at_the_next_open_in_lots_never_at_the_signal_close():
    b = bars("CN:600000", [(k, 10.0 + k, 10.5 + k, 9.5 + k, 10.2 + k) for k in range(30)], limits=False)
    out = run(signals([("CN:600000", 3, None, None)]), b, max_positions=1)
    t = out["trades"].row(0, named=True)
    assert t["entry_date"] == DAYS[4] and t["entry_price"] == 14.0                       # day 4's open, not day 3's close
    assert t["shares"] == 7100 and t["shares"] % 100 == 0                                  # 100,000 / 14 in lots of 100
    assert out["nav"]["date"][0] == DAYS[0] and out["nav"].height == 30                    # flat days are on the timeline
    balanced(out)


def test_costs_and_slippage_are_booked_apart():
    b = flat("CN:600000")
    c = Costs(commission=0.00025, min_commission=5.0, stamp=0.0005, transfer=0.00001, slippage=0.001)
    out = run(signals([("CN:600000", 0, None, None)]), b, costs=c, hold=3, max_positions=1)
    led = out["ledger"]
    kinds = led.group_by("kind").agg(pl.col("amount").sum()).sort("kind")
    t = out["trades"].row(0, named=True)
    value_in, value_out = t["shares"] * 10.0, t["shares"] * 10.0
    assert dict(kinds.rows())["slippage"] == pytest.approx(-(value_in + value_out) * 0.001)
    assert dict(kinds.rows())["fee"] == pytest.approx(-(max(value_in * 0.00025, 5) + value_in * 0.00001
                                                         + max(value_out * 0.00025, 5) + value_out * 0.00001 + value_out * 0.0005))
    assert t["exit_date"] == DAYS[3] and t["reason"] == "hold"                             # the entry day is day 1: day 3's close
    balanced(out)


def test_a_rate_that_changed_on_a_date_is_charged_by_the_day_of_each_trade():
    b = pl.concat([flat("CN:600000"), flat("CN:600001")])
    c = Costs(stamp=0.0005, history=((DAYS[8], "stamp", 0.001),))                      # until DAYS[8]: 0.1%
    out = run(signals([("CN:600000", 0, None, None), ("CN:600001", 9, None, None)]), b, costs=c, hold=3)
    sold = out["trades"].sort("exit_date").select("exit_date", "shares").rows()
    fees = out["ledger"].filter(pl.col("kind") == "fee").sort("date").select("date", "amount").rows()
    assert sold[0][0] < DAYS[8] <= sold[1][0]
    assert fees == [(sold[0][0], pytest.approx(-sold[0][1] * 10.0 * 0.001)), (sold[1][0], pytest.approx(-sold[1][1] * 10.0 * 0.0005))]
    balanced(out)


def test_cn_rates_follow_the_days_they_changed():
    cn = COSTS["CN"]
    assert (cn.on(date(2023, 8, 25)).stamp, cn.on(date(2023, 8, 28)).stamp) == (0.001, 0.0005)          # 印花税减半: 2023-08-28
    assert (cn.on(date(2022, 4, 28)).transfer, cn.on(date(2022, 4, 29)).transfer) == (0.00002, 0.00001)  # 过户费减半: 2022-04-29
    assert cn.on(date(2013, 1, 4)).stamp == 0.001 and cn.on(date(2026, 9, 30)) == cn
    assert cn.scaled(2).on(date(2023, 8, 25)).stamp == 0.002                                               # 成本上调 raises the old rates too


def test_a_close_below_the_box_line_sells_at_the_next_open_and_a_limit_down_open_waits():
    # the box's line (bottom × (1 − tol), adjusted) is judged on the close like BOX's break_down — a wick that closes back
    # inside is no break (broken intraday, back by the close). Day 1 closes below 9.6: sold at day 2's open — which is
    # the limit down (from 9.5): no sale; day 3's open: sold
    b = bars("CN:600000", [(0, 10, 10, 10, 10), (1, 10, 10.2, 9.0, 9.5), (2, 8.55, 8.6, 8.55, 8.55), (3, 8.6, 8.9, 8.5, 8.8)]
             + [(k, 8.8, 8.9, 8.7, 8.8) for k in range(4, 30)])
    out = run(signals([("CN:600000", 0, 9.6, None)]), b, box_stop=True, t_plus_1=True, max_positions=1)
    t = out["trades"].row(0, named=True)
    assert (t["exit_date"], t["exit_price"], t["reason"]) == (DAYS[3], 8.6, "box")
    assert out["counts"]["blocked_sell"] == {"limit": 1}
    wick = bars("CN:600002", [(0, 10, 10, 10, 10), (1, 10, 10.2, 9.0, 9.9)] + [(k, 9.9, 10, 9.8, 9.9) for k in range(2, 30)])
    assert run(signals([("CN:600002", 0, 9.6, None)]), wick, box_stop=True, t_plus_1=True)["trades"]["exit_date"].to_list() == [None]
    # a next-day open at the limit up cannot be bought
    up = bars("CN:600001", [(0, 10, 10, 10, 10), (1, 11, 11, 11, 11)] + [(k, 11, 11.2, 10.9, 11) for k in range(2, 30)])
    out = run(signals([("CN:600001", 0, None, None)]), up, t_plus_1=True)
    assert out["trades"].is_empty() and out["counts"]["blocked_buy"] == {"limit": 1}


def test_cn_never_sells_on_the_day_it_bought():
    # bought at 10 on day 1, whose low touches the 5% trailing stop (9.5): JP sells there, CN (T+1) cannot and holds
    rows = [(0, 10, 10, 10, 10), (1, 10, 10.2, 9.0, 9.8)] + [(k, 9.9, 10.0, 9.6, 9.7) for k in range(2, 30)]
    jp = run(signals([("JP:7203", 0, None, None)]), bars("JP:7203", rows, limits=False), trail=0.05)["trades"].row(0, named=True)
    assert (jp["exit_date"], jp["exit_price"], jp["reason"]) == (DAYS[1], 9.5, "trail")
    cn = run(signals([("CN:600000", 0, None, None)]), bars("CN:600000", rows), trail=0.05, t_plus_1=True)["trades"].row(0, named=True)
    assert cn["exit_date"] is None


def test_jp_an_open_at_the_limit_up_is_not_bought_and_one_at_the_limit_down_not_sold():
    rows = [("JP:7203", DAYS[0], 1000.0, 1000.0, 1000.0, 1000.0, 1.0, 1.0, None, None, False, False),
            ("JP:7203", DAYS[1], 1300.0, 1300.0, 1250.0, 1280.0, 1.0, 1.0, None, None, True, False),    # UL, open at the high
            ("JP:7204", DAYS[0], 1000.0, 1000.0, 1000.0, 1000.0, 1.0, 1.0, None, None, False, False),
            ("JP:7204", DAYS[1], 1000.0, 1010.0, 990.0, 1000.0, 1.0, 1.0, None, None, False, False),
            ("JP:7204", DAYS[2], 950.0, 960.0, 850.0, 860.0, 1.0, 1.0, None, None, False, False),       # closes below the line
            ("JP:7204", DAYS[3], 760.0, 790.0, 760.0, 780.0, 1.0, 1.0, None, None, False, True),        # LL, open at the low
            ("JP:7204", DAYS[4], 770.0, 790.0, 760.0, 780.0, 1.0, 1.0, None, None, False, False)]
    rows += [("JP:7204", DAYS[k], 780.0, 790.0, 770.0, 780.0, 1.0, 1.0, None, None, False, False) for k in range(5, 30)]
    b = pl.DataFrame(rows, schema=BARS, orient="row")
    out = run(signals([("JP:7203", 0, None, None), ("JP:7204", 0, 900.0, None)]), b, box_stop=True, cash=1_000_000.0)
    assert out["counts"]["blocked_buy"] == {"limit": 1}
    t = out["trades"].row(0, named=True)
    assert t["listing_id"] == "JP:7204" and (t["exit_date"], t["exit_price"], t["reason"]) == (DAYS[4], 770.0, "box")
    assert out["counts"]["blocked_sell"] == {"limit": 1}


def test_a_gap_through_the_stop_fills_at_the_open_and_stop_with_target_on_one_bar_takes_the_stop():
    b = bars("JP:7203", [(0, 100, 100, 100, 100), (1, 100, 101, 99, 100), (2, 90, 92, 88, 91)]
             + [(k, 91, 92, 90, 91) for k in range(3, 30)], limits=False)
    t = run(signals([("JP:7203", 0, None, None)]), b, trail=0.05)["trades"].row(0, named=True)
    assert (t["exit_date"], t["exit_price"], t["reason"]) == (DAYS[2], 90.0, "trail")     # the open, not the stop price
    wide = bars("JP:7204", [(0, 100, 100, 100, 100), (1, 100, 101, 99, 100), (2, 100, 112, 94, 100)]
                + [(k, 100, 101, 99, 100) for k in range(3, 30)], limits=False)
    out = run(signals([("JP:7204", 0, None, None)]), wide, trail=0.05, target=0.10)
    t = out["trades"].row(0, named=True)
    assert (t["exit_price"], t["reason"]) == (95.0, "trail") and out["counts"]["same_bar"] == 1


def test_a_trailing_stop_follows_the_highest_close():
    b = bars("JP:7203", [(0, 100, 100, 100, 100), (1, 100, 101, 99, 100), (2, 101, 121, 100, 120), (3, 119, 119, 107, 110)]
             + [(k, 110, 111, 109, 110) for k in range(4, 30)], limits=False)
    t = run(signals([("JP:7203", 0, None, None)]), b, trail=0.10)["trades"].row(0, named=True)
    assert (t["exit_date"], t["exit_price"], t["reason"]) == (DAYS[3], 108.0, "trail")     # 10% below the 120 close


def test_dividends_and_splits_are_booked_from_the_factors_not_from_adjusted_prices():
    # a 2:1 split with a ¥10 dividend on day 5 (per old share): prev close 200, ex price (200 − 10) / 2 = 95
    rows = [(k, 200.0, 201.0, 199.0, 200.0, 1.0, 1.0) for k in range(5)]
    adj = 1.0 * 200 * 2 / (200 - 10)                        # hfq continuous: 200 × 1.0 = 95 × adj
    rows += [(k, 95.0, 96.0, 94.0, 95.0, adj, 2.0) for k in range(5, 30)]
    out = run(signals([("JP:7203", 0, None, None)]), bars("JP:7203", rows, limits=False), max_positions=1)
    t = out["trades"].row(0, named=True)
    assert t["shares"] == 500                               # 100,000 / 200 in lots of 100
    led = out["ledger"]
    div = led.filter(pl.col("kind") == "dividend")
    assert div["amount"].to_list() == [pytest.approx(500 * 10.0)]
    assert led.filter(pl.col("kind") == "split")["shares"].to_list() == [500]           # 500 more shares
    nav = out["nav"]
    assert nav.filter(pl.col("date") == DAYS[5])["nav"][0] == pytest.approx(nav.filter(pl.col("date") == DAYS[4])["nav"][0])  # no jump
    balanced(out)


def test_equal_weight_up_to_the_positions_allowed_in_the_rules_order():
    sig = signals([("JP:7203", 0, None, 3), ("JP:7204", 0, None, 1), ("JP:7205", 0, None, 2)])
    b = pl.concat([flat("JP:7203"), flat("JP:7204"), flat("JP:7205")])
    out = run(sig, b, max_positions=2)
    assert sorted(out["trades"]["listing_id"].to_list()) == ["JP:7204", "JP:7205"]
    assert out["counts"]["skipped"] == {"capacity": 1}
    assert out["trades"]["shares"].to_list() == [5000, 5000]                              # half the book each


def test_a_halted_day_holds_the_exit_to_the_next_open_and_open_positions_are_kept_at_the_end():
    rows = [(k, 10.0, 10.1, 9.9, 10.0) for k in range(3)] + [(k, 11.0, 11.1, 10.9, 11.0) for k in range(4, 30)]  # no day 3
    out = run(signals([("JP:7203", 0, None, None), ("JP:7204", 0, None, None)]),
              pl.concat([bars("JP:7203", rows, limits=False), flat("JP:7204")]), hold=3)
    t = {r["listing_id"]: r for r in out["trades"].iter_rows(named=True)}
    assert (t["JP:7203"]["exit_date"], t["JP:7203"]["exit_price"], t["JP:7203"]["reason"]) == (DAYS[4], 11.0, "hold")
    still = run(signals([("JP:7204", 0, None, None)]), flat("JP:7204"))["trades"].row(0, named=True)
    assert still["exit_date"] is None and still["reason"] is None                        # held at the end, marked to market


def test_a_factor_missing_before_its_first_value_is_no_corporate_action():
    # the factors can arrive later than the prices (an optional call): the first one known is no dividend and no split
    rows = [(k, 10.0, 10.1, 9.9, 10.0, None, None) for k in range(4)] + [(k, 10.0, 10.1, 9.9, 10.0, 3.0, 1.0) for k in range(4, 30)]
    out = run(signals([("CN:600000", 0, None, None)]), bars("CN:600000", rows, limits=False), max_positions=1)
    assert out["ledger"].filter(pl.col("kind").is_in(["dividend", "split", "unexplained"])).height == 0
    assert out["counts"]["corporate"] == 0 and out["nav"]["nav"].n_unique() == 1
    balanced(out)


def test_cn_cash_comes_from_the_dividend_records_and_what_they_do_not_explain_is_no_cash():
    # 10派1 on day 5 (close 20, ex price 19): the record pays 1.0 a share. A 10配3 at 5 the same day (close 10, the
    # exchange's reference 8.846) moves the factor as if 1.15 were paid: nothing is, the holder who does not subscribe
    # just has the cheaper shares — no cash, a row saying so, counted
    paid = [(k, 20.0, 20.2, 19.8, 20.0, 1.0, 1.0) for k in range(5)] + [(k, 19.0, 19.2, 18.8, 19.0, 20 / 19, 1.0) for k in range(5, 30)]
    ref = (10 + 0.3 * 5) / 1.3
    rights = [(k, 10.0, 10.1, 9.9, 10.0, 1.0, 1.0) for k in range(5)] + [(k, ref, ref, ref, ref, 10 / ref, 1.0) for k in range(5, 30)]
    b = pl.concat([bars("CN:600000", paid, limits=False), bars("CN:600001", rights, limits=False)]).with_columns(
        pl.when((pl.col("listing_id") == "CN:600000") & (pl.col("date") == DAYS[5])).then(1.0).alias("cash_div"))
    out = run(signals([("CN:600000", 0, None, 0), ("CN:600001", 0, None, 1)]), b)
    led = out["ledger"]
    div = led.filter(pl.col("kind") == "dividend")
    assert div["listing_id"].to_list() == ["CN:600000"] and div["price"][0] == pytest.approx(1.0)
    odd = led.filter(pl.col("kind") == "unexplained")
    assert odd["listing_id"].to_list() == ["CN:600001"] and odd["amount"][0] == 0.0 and odd["price"][0] == pytest.approx(10 - ref)
    assert out["counts"]["unexplained"] == 1
    balanced(out)


def test_n_of_its_days_without_a_new_high_sells_at_the_next_open():
    # 有新高再看高，直到没有高, read on the daily bars for research: a close above the highest since entry
    # restarts the count; this many of its own trading days without one, and it is sold at the next open
    rows = [(k, 10.0 + k, 10.5 + k, 9.8 + k, 10.2 + k) for k in range(6)] + [(k, 15.0, 15.1, 14.9, 15.0) for k in range(6, 30)]
    out = run(signals([("CN:600000", 0, None, None)]), bars("CN:600000", rows, limits=False), max_positions=1, stall=3)
    t = out["trades"].row(0, named=True)
    assert t["reason"] == "stall" and t["exit_date"] == DAYS[9] and t["exit_price"] == 15.0
    balanced(out)


def test_a_weak_market_allows_fewer_new_places_and_keeps_what_is_held():
    # 随大势（simplified for research): the day after the benchmark closed below its 200-day line new places
    # stop at `weak_positions`; what is held is not sold for it
    b = pl.concat([flat("CN:600000"), flat("CN:600001"), flat("CN:600002")])
    sig = signals([("CN:600000", 2, None, 0), ("CN:600001", 4, None, 0), ("CN:600002", 4, None, 1)])
    s = Settings(cash=100_000.0, costs=NO_COST, max_positions=3, hold=None, box_stop=False, trail=0.5, weak_positions=1)
    out = simulate(sig, b, DAYS[:30], s, weak={DAYS[5]})
    assert out["trades"]["listing_id"].to_list() == ["CN:600000"] and out["counts"]["skipped"]["weak"] == 2
    assert simulate(sig, b, DAYS[:30], s)["trades"].height == 3                         # no weak days: three places
    balanced(out)


def test_the_research_exits_where_they_meet_the_others():
    # the plan's review focus, pinned: no new places at all in a weak market with 0; a box line and the stall on one
    # close — the box line names it; a halted day is no day without a new high
    b = pl.concat([flat("CN:600000"), flat("CN:600001")])
    s = Settings(cash=100_000.0, costs=NO_COST, max_positions=2, hold=None, box_stop=False, trail=0.5, weak_positions=0)
    out = simulate(signals([("CN:600000", 2, None, None)]), b, DAYS[:30], s, weak={DAYS[3]})
    assert out["trades"].height == 0 and out["counts"]["skipped"]["weak"] == 1
    rows = [(k, 10.0, 10.1, 9.9, 10.0) for k in range(5)] + [(k, 9.0, 9.1, 8.9, 9.0) for k in range(5, 30)]
    out = run(signals([("CN:600000", 0, 9.5, None)]), bars("CN:600000", rows, limits=False), max_positions=1, box_stop=True, stall=5)
    assert out["trades"]["reason"].to_list() == ["box"]                        # day 5's close: under the line and the 5th quiet day
    halted = bars("CN:600000", [(k, 10.0 + k, 10.5 + k, 9.8 + k, 10.2 + k) for k in range(5)]
                  + [(k, 15.0, 15.1, 14.9, 15.0) for k in range(5, 7)] + [(k, 15.0, 15.1, 14.9, 15.0) for k in range(12, 30)], limits=False)
    t = run(signals([("CN:600000", 0, None, None)]), halted, max_positions=1, stall=3)["trades"].row(0, named=True)
    assert t["reason"] == "stall" and t["exit_date"] == DAYS[14]               # halted 7–11: quiet on 6, 12, 13 → sold at 14's open


def test_the_rule_s_own_lines_end_a_position_its_line_on_the_close_its_target_when_touched():
    # 规则自己的判断 in the book (spec §9.1, §9.2): the signal's 失效线 (stop, adjusted) on the close, sold at the next open,
    # reason fail; its 目标位 (target_px) touched during a day, sold there, reason target — the box line and the fixed
    # target are not used
    rise = bars("CN:600000", [(k, 10.0 + 0.2 * k, 10.1 + 0.2 * k, 9.9 + 0.2 * k, 10.0 + 0.2 * k) for k in range(30)], limits=False)
    fall = bars("CN:600001", [(k, 10.0 - 0.1 * k, 10.05 - 0.1 * k, 9.95 - 0.1 * k, 10.0 - 0.1 * k) for k in range(30)], limits=False)
    sig = pl.DataFrame([("CN:600000", DAYS[1], 9.0, 12.0, 0), ("CN:600001", DAYS[1], 9.5, 99.0, 1)],
                       schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "stop": pl.Float64, "target_px": pl.Float64, "rank": pl.Int64},
                       orient="row")
    out = run(sig, pl.concat([rise, fall]), own=True, target=0.5)
    t = {r["listing_id"]: r for r in out["trades"].iter_rows(named=True)}
    up, down = t["CN:600000"], t["CN:600001"]
    assert up["reason"] == "target" and up["exit_price"] == pytest.approx(12.0)          # 10.1 + 0.2k reaches 12.0 on day 10
    assert down["reason"] == "fail" and down["exit_date"] == DAYS[7]                    # closes under 9.5 on day 6, out at 7's open
    balanced(out)
