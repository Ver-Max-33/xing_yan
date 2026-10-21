"""盘中评估 (spec §7.2): today's temporary bar — the day's minutes so far, its volume and turnover scaled to the whole
day — goes after the stock's daily bars and the rule runs on it as the scan would if the day closed now, formulas and
structures alike; a price outside the day's limits means an ex-rights day the factor has not caught: not judged."""
from datetime import date, datetime

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.frame import build_frame
from wb.engine.l3.rule import load_rule
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.engine.l2.box import resumed
from wb.engine.l2.run import compute, l2_bars
from wb.engine.l3.run import primitives
from wb.realtime.today import Shared, day_factors, evaluate_today, ex_rights, temp_bars

DAY = date(2026, 10, 1)  # the weekday after the demo data's last day

RISE = """
id: rise
name: 放量大涨
markets: [CN, JP]
stages:
  - {id: today, kind: event, when: "C > REF(C, 1) * 1.05 AND V > MA(V, 5)"}
signal_at: today.at
"""


def minutes(lid, prices, volume=1000.0, start=(9, 0)):
    h, m = start
    return pl.DataFrame([(lid, datetime(DAY.year, DAY.month, DAY.day, h, m + k), p, p * 1.001, p * 0.999, p, volume)
                         for k, p in enumerate(prices)], schema=MINUTES, orient="row")


def last_bar(paths, lid):
    market = lid.split(":")[0]
    return Store(paths, market).read("bars_1d").filter(pl.col("listing_id") == lid).sort("date").tail(1).row(0, named=True)


def test_todays_bar_is_its_minutes_with_the_volume_scaled_to_the_whole_day():
    m = minutes("JP:7203", [100.0, 103.0, 101.0], volume=2000.0)
    bar = temp_bars(m, DAY, total=330, covered=30).row(0, named=True)
    assert (bar["date"], bar["open"], bar["close"]) == (DAY, 100.0, 101.0)
    assert bar["high"] == pytest.approx(103.103) and bar["low"] == pytest.approx(99.9)
    assert bar["volume"] == pytest.approx(6000 * 330 / 30)                            # 按时间折算
    assert bar["amount"] == pytest.approx((100 + 103 + 101) * 2000 * 330 / 30)


def test_on_the_markets_volume_clock_the_day_is_what_has_traded_over_the_share_usually_done_by_now():
    # design 2026-10-10 §6: 30 minutes in, a fifth of the day has usually traded (not an eleventh): the day's volume
    # is the traded ÷ 0.2, where a linear estimate would say × 11
    clock = tuple([0.2 if k == 29 else min(1.0, (k + 1) / 330) for k in range(330)])
    m = minutes("JP:7203", [100.0, 103.0, 101.0], volume=2000.0)
    bar = temp_bars(m, DAY, total=330, covered=30, clock=clock).row(0, named=True)
    assert bar["volume"] == pytest.approx(6000 / 0.2) and bar["amount"] == pytest.approx((100 + 103 + 101) * 2000 / 0.2)


def test_a_source_with_its_turnover_gives_the_bars_amount_as_it_is():
    # TickFlow's minutes carry their amount: the day's turnover is theirs, not price × volume (a minute without one is)
    m = minutes("JP:7203", [100.0, 103.0, 101.0], volume=2000.0).with_columns(
        pl.Series("amount", [210_000.0, 190_000.0, None]))
    bar = temp_bars(m, DAY, total=330, covered=30).row(0, named=True)
    assert bar["amount"] == pytest.approx((210_000 + 190_000 + 101 * 2000) * 330 / 30)


def test_the_bar_goes_after_the_stored_ones_in_the_prices_formulas_read(paths):
    # Toyota split 1:5 in the demo data: today's raw price carries yesterday's factor, as the spec says for JP
    write(paths, 320)
    prev = last_bar(paths, "JP:7203")
    bar = temp_bars(minutes("JP:7203", [prev["close"] * 1.02]), DAY, 330, 1)
    store = Store(paths, "JP")
    f = build_frame(store, "JP", paths.profile, {"close", "raw_close"}, ["JP:7203"], snap=store.latest(), extra=bar).sort("date")
    assert f["date"][-1] == DAY and f["raw_close"][-1] == pytest.approx(prev["close"] * 1.02)
    assert f["close"][-1] / f["raw_close"][-1] == pytest.approx(f["close"][-2] / f["raw_close"][-2])


def test_the_rule_judges_today_as_if_the_day_closed_now(paths):
    write(paths, 320)
    rule = load_rule(RISE)
    up, flat = last_bar(paths, "JP:7203"), last_bar(paths, "JP:6758")
    m = pl.concat([minutes("JP:7203", [up["close"] * 1.08], volume=1e8), minutes("JP:6758", [flat["close"] * 1.01], volume=1e8)])
    got = evaluate_today(paths, rule, "JP", temp_bars(m, DAY, 330, 330))
    assert got["JP:7203"]["status"] == "signal" and got["JP:6758"]["status"] == "fail"
    assert got["JP:7203"]["detail"]["conditions"] and got["JP:7203"]["lines"][0].endswith("信号")   # why, as the scan explains it


def test_a_cross_sectional_rule_is_not_judged_intraday(paths):
    # a rank among the day's listings needs the whole market's day, which the intraday source does not have
    write(paths, 320)
    rule = load_rule("""
id: top
name: 涨幅前列
markets: [CN]
stages:
  - {id: today, kind: event, when: "DENSE_RANK_XS(C / REF(C, 1)) <= 3"}
signal_at: today.at
""")
    flat = last_bar(paths, "CN:600519")
    assert evaluate_today(paths, rule, "CN", temp_bars(minutes("CN:600519", [flat["close"]]), DAY, 240, 1)) == {}


def test_a_move_beyond_the_days_limit_is_an_ex_rights_day_the_factor_has_not_caught(paths):
    write(paths, 320)
    cn, jp = last_bar(paths, "CN:600519"), last_bar(paths, "JP:7203")
    bars = pl.concat([temp_bars(minutes("CN:600519", [cn["close"] * 0.75]), DAY, 240, 1),
                      temp_bars(minutes("CN:000001", [last_bar(paths, "CN:000001")["close"] * 1.09]), DAY, 240, 1)])
    assert ex_rights(paths, "CN", bars) == {"CN:600519"}                               # 10送3: -25% on a 10% board
    assert ex_rights(paths, "JP", temp_bars(minutes("JP:7203", [jp["close"] * 0.5]), DAY, 330, 1)) == {"JP:7203"}


SHANG = """
id: shang
name: 上品
markets: [JP]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: [mid, long]}}
  - {id: g, kind: structure, use: GOLD, with: {kind: 上品, of: big}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
watch_stage: g
"""
BOX = """
id: box
name: 箱体上破
markets: [JP]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: [mid, long]TOL}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
watch_stage: big
"""


def test_rules_sharing_a_polls_structures_get_what_their_own_computation_gives(paths):
    # 盯盘 computes each structure once a poll for every rule reading it with the same settings, each rule taking its
    # own stocks' rows — the same tables, row for row, as the rule computing its own: box touches and events included
    # (they carry no listing), the first poll of the day and one going on from the scans it kept
    write(paths, 320)
    rules = [(load_rule(SHANG), ["JP:6758", "JP:130A"]), (load_rule(BOX.replace("TOL", "")), ["JP:7203", "JP:6758"]),
             (load_rule(BOX.replace("id: box", "id: wide").replace("TOL", ", tol: 0.03")), ["JP:7203"])]
    store = Store(paths, "JP")
    states = {}
    for move in (1.01, 1.04):
        m = pl.concat([minutes(lid, [last_bar(paths, lid)["close"] * move]) for lid in ("JP:7203", "JP:130A", "JP:6758")])
        bars = temp_bars(m, DAY, 330, 30)
        shared = Shared(paths, "JP", bars, rules)
        with resumed(states):
            for rule, ids in rules:
                names, params = primitives(rule), rule.primitive_params()
                own = bars.filter(pl.col("listing_id").is_in(ids))
                want = compute(l2_bars(store, "JP", paths.profile, ids, snap=store.latest(), extra=own), names, params)
                got = shared.tables(names, params, ids)
                assert {n: {t: df.height for t, df in want[n].items()} for n in names} == \
                    {n: {t: df.height for t, df in got[n].items()} for n in names}
                assert all(got[n][t].equals(want[n][t]) for n in names for t in want[n]), (rule.id, move)


# Today's factors on an ex-date (review 10-13): today's bar still forming carried yesterday's 复权 factor, so on a 送转 /
# dividend / split day the K-line gapped (688110 10-12, 10送4.9: history 1.5×, MA5 +41%), the structures and rules read a
# fake fall and a held stock's 失效线 stood the dividend too high in today's prices.
def ex_bar(**kw):
    row = {"listing_id": "CN:600519", "date": DAY, "close": 9.4, "_close": 10.0, "_adj": 2.0, "_share": 1.0,
           "_ratio": None, "_cash": None}
    return pl.DataFrame([{**row, **kw}], schema_overrides={"_ratio": pl.Float64, "_cash": pl.Float64})


def test_a_cash_dividend_day_gets_its_factor_from_the_days_corporate_action():
    r = day_factors(ex_bar(_ratio=1.0, _cash=0.5), "CN").row(0, named=True)
    assert r["pre_close"] == pytest.approx(9.5)                        # the exchange's reference: 10 − 0.5
    assert r["adj_factor"] == pytest.approx(2.0 * 10 / 9.5) and r["share_factor"] == pytest.approx(1.0)


def test_the_quotes_close_before_is_the_exchanges_reference_when_it_moved():
    r = day_factors(ex_bar(pre_close=9.5), "CN").row(0, named=True)   # no action on file: the quote's 昨收 says it
    assert r["adj_factor"] == pytest.approx(2.0 * 10 / 9.5) and r["share_factor"] is None


def test_an_ordinary_day_keeps_yesterdays_factors():
    r = day_factors(ex_bar(pre_close=10.0), "CN").row(0, named=True)
    assert r["adj_factor"] is None and r["share_factor"] is None and r["pre_close"] == 10.0


def test_a_split_is_its_ratio_in_both_factors():
    r = day_factors(ex_bar(listing_id="JP:7203", _close=3000.0, _adj=5.0, _share=5.0, _ratio=2.0, _cash=40.0), "JP").row(0, named=True)
    assert r["adj_factor"] == pytest.approx(10.0) and r["share_factor"] == pytest.approx(10.0)   # JP: splits only


def test_a_forming_bar_on_a_bonus_day_does_not_gap_the_chart():
    from tests.test_chart_series import cn_daily
    from wb.charts.series import build
    d = cn_daily()                                                     # a 1:2 bonus on day 100
    official = build(d.head(101), "CN:600519", "CN", "day", "qfq").frame
    r, y = d.row(100, named=True), d.row(99, named=True)
    temp = pl.DataFrame([{"listing_id": "CN:600519", "date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
                          "close": r["close"], "volume": r["volume"], "amount": r["amount"], "_close": y["close"],
                          "_adj": y["adj_factor"], "_share": y["share_factor"], "_ratio": 2.0, "_cash": 0.0}])
    temp = day_factors(temp, "CN").drop("_close", "_adj", "_share", "_ratio", "_cash")
    live = build(pl.concat([d.head(100), temp], how="diagonal_relaxed"), "CN:600519", "CN", "day", "qfq").frame
    assert live["close"][-1] / live["close"][-2] == pytest.approx(official["close"][-1] / official["close"][-2], rel=1e-6)
    assert live["ma5"][-1] == pytest.approx(official["ma5"][-1], rel=1e-6)
    assert live["volume"][-2] == pytest.approx(official["volume"][-2])  # the days before in today's share units
