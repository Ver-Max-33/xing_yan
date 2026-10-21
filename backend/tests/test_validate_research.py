"""按交易计划的研究 (design 2026-10-09 §6): each signal day is one sample (the mean of that day's trades), the expectancy is
their mean with an interval from resampling days, the rule against its random control day by day, trades by their
ends; a planted edge is found, a shuffled one is not."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.engine.l3.rule import Plan, PlanLine
from wb.validate.research import bootstrap, day_samples, halves_plan, report

D0 = date(2020, 1, 1)
PLAN = Plan(entry="close", stop=[PlanLine(line="x", label="假底")], days=20)


def trades(days, per_day, mean, seed, *, control=False, status="done"):
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(days):
        for j in range(per_day):
            r = float(rng.normal(mean, 0.05))
            rows.append({"listing_id": f"CN:{600000 + j}", "signal_date": D0 + timedelta(days=k), "status": status, "ret": r,
                         "excess": r - 0.001, "held": 5, "mfe": abs(r) + 0.01, "mae": -abs(r) - 0.01,
                         "reason": "stop0" if r < 0 else "days", "no_room": 0, **({"draw": j} if control else {})})
    return pl.DataFrame(rows)


def test_a_day_is_one_sample_however_many_trades_it_had():
    t = pl.concat([trades(1, 100, 0.01, 0), trades(1, 1, 0.01, 1).with_columns(pl.lit(D0 + timedelta(days=1)).alias("signal_date"))])
    s = day_samples(t)
    assert s.height == 2 and s["n"].sort().to_list() == [1, 100]


def test_a_planted_edge_over_the_control_is_found():
    r = report(trades(300, 3, 0.02, 0), trades(300, 20, 0.0, 1, control=True), D0 + timedelta(days=200), PLAN, per_day=20)
    assert r["expectancy"] == pytest.approx(0.02, abs=0.01) and r["ci"][0] > 0.01
    assert r["vs_control"] > 0.01 and r["vs_control_ci"][0] > 0 and r["control_days"] == 300 and r["no_control_days"] == 0


def test_the_same_distribution_for_rule_and_control_is_no_edge():
    held = 0
    for seed in range(20):
        r = report(trades(120, 3, 0.0, seed), trades(120, 20, 0.0, 100 + seed, control=True), D0 + timedelta(days=80), PLAN)
        held += r["vs_control_ci"][0] <= 0 <= r["vs_control_ci"][1]
    assert held >= 17


def test_open_and_unbought_trades_are_counted_apart():
    t = pl.concat([trades(30, 2, 0.01, 0), trades(5, 1, 0.0, 1, status="open"),
                   trades(4, 1, 0.0, 2, status="limit").with_columns(pl.lit(None, pl.Float64).alias("ret"))])
    r = report(t, None, D0 + timedelta(days=20), PLAN)
    assert r["bought"] == 60 + 5 and r["open"] == 5 and r["blocked"] == {"limit": 4, "halt": 0} and r["finished"] == 60
    assert r["vs_control"] is None and r["labels"]["stop0"] == "假底"


def test_a_market_s_signals_with_no_control_at_all_report_without_a_comparison():
    # a way of buying that buys nothing (撞顶涨停 bought at its limit-up close: every one 涨停买不到) draws no control:
    # the report of a market's trades says so, it does not fail (2026-10-11, the 买法对比 on real data)
    t = trades(30, 2, 0.01, 0).with_columns(pl.lit("CN").alias("market"))
    r = report(t, None, D0 + timedelta(days=20), PLAN)
    assert r["vs_control"] is None and r["control_days"] == 0 and r["no_control_days"] == 30 and r["days"] == 30


def test_a_day_whose_control_drew_nobody_has_no_comparison():
    rule = trades(10, 2, 0.01, 0)
    ctrl = trades(10, 20, 0.0, 1, control=True).filter(pl.col("signal_date") != D0)
    r = report(rule, ctrl, D0 + timedelta(days=5), PLAN)
    assert r["control_days"] == 9 and r["no_control_days"] == 1


def test_only_each_day_s_first_bought_draws_are_the_control():
    ctrl = trades(1, 30, 0.0, 1, control=True).with_columns(
        pl.when(pl.col("draw") < 10).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("ret"))
    r = report(trades(1, 2, 0.0, 0), ctrl, D0 + timedelta(days=5), PLAN, per_day=10)
    assert r["control_expectancy"] == pytest.approx(1.0)


def test_the_halves_for_the_checkup_read_expectancy_and_its_standard_error():
    t = trades(100, 2, 0.01, 0)
    h = halves_plan(t, D0 + timedelta(days=50))
    assert h["effective_in"] == 50 and h["effective_out"] == 50
    assert h["mean_in"] == pytest.approx(0.01, abs=0.01) and h["se_in"] == pytest.approx(0.05 / np.sqrt(2) / np.sqrt(50), rel=0.3)


def test_bootstrap_is_seeded():
    v = np.random.default_rng(0).normal(0, 1, 200)
    assert bootstrap(v) == bootstrap(v) and bootstrap(v)[0] < v.mean() < bootstrap(v)[1]



def test_a_cross_plan_s_report_splits_its_crosses_by_how_the_day_closed():
    # design 2026-10-11 §6 「盘中越线不等于收盘还在线上」: the rule's signal that close; over the break line at the close but
    # not the signal (its breakout volume, say); not over it — the 假突破 (final review I1: the last two were one group)
    t = trades(30, 3, 0.01, 0)
    k = pl.int_range(pl.len()) % 3
    t = t.with_columns((k == 0).alias("signal"), (k <= 1).alias("closed_above"), pl.lit(-0.01).alias("gap"))
    c = report(t, None, D0 + timedelta(days=20), Plan(entry="cross"))["cross"]
    groups = {g["key"]: g for g in c["kinds"]}
    assert set(groups) == {"收盘成了规则信号", "收盘站上突破线、没成信号", "收盘没站上（假突破）"}
    assert all(g["finished"] == 30 for g in groups.values())
    assert c["signal"] == pytest.approx(1 / 3) and c["closed_above"] == pytest.approx(2 / 3)