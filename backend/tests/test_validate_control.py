"""随机对照组 (design 2026-10-09 §6.2): on each signal day, stocks drawn at random from the rule's own pool that day — not
that day's signals — each given one of the day's bought signals' 失效线 / 目标位 as a share of its entry price."""
import polars as pl

from tests.test_validate_plantrades import RULE, market  # noqa: F401 — the synthetic A-share store
from wb.engine.l3.match import universe_formula
from wb.engine.l3.rule import load_rule
from wb.validate.control import control_signals
from wb.validate.engine import Variant, market_signals
from wb.validate.plantrades import plan_rule, plan_trades

POOL = RULE.replace("markets: [CN]\n", "markets: [CN]\nuniverse: {exclude: [listed_lt_120d]}\n")


def traded(paths, ds, text):
    rule = load_rule(text)
    sig = market_signals(paths, "CN", [Variant(plan_rule(rule))], ds[30], ds[-1], lines=True)[0]
    return rule, plan_trades(paths, "CN", rule, sig)


def test_the_pool_is_the_rule_s_universe_as_one_condition():
    assert universe_formula(load_rule(POOL), "CN", None) == ("BARSCOUNT(C) >= 120", [])
    text, said = universe_formula(load_rule(POOL.replace("[listed_lt_120d]", "[st, listed_lt_120d]")), "CN", {"close"})
    assert text == "BARSCOUNT(C) >= 120" and said == ["当前数据档没有 ST 信息，股票池没有排除 ST"]
    assert universe_formula(load_rule(RULE), "CN", None) == (None, [])


def test_controls_are_drawn_from_the_pool_that_day_never_the_day_s_signals(market):  # noqa: F811
    paths, ds = market
    rule, t = traded(paths, ds, POOL)
    c = control_signals(paths, "CN", rule, t, per_day=5, seed=1)
    assert c.height > 0
    sigs = t.select("listing_id", "signal_date")
    assert c.join(sigs, on=["listing_id", "signal_date"], how="semi").is_empty()          # never a signal of that day
    assert c["signal_date"].min() >= ds[119]                                              # 120 bars before a stock is in the pool
    assert c.group_by("signal_date").len()["len"].max() <= 15                             # three times the day's control at most
    again = control_signals(paths, "CN", rule, t, per_day=5, seed=1)
    assert again.equals(c) and not control_signals(paths, "CN", rule, t, per_day=5, seed=2).equals(c)


def test_a_control_carries_a_bought_signal_s_lines_as_shares_of_its_entry(market):  # noqa: F811
    paths, ds = market
    rule, t = traded(paths, ds, RULE)
    c = control_signals(paths, "CN", rule, t, per_day=5, seed=0)
    bought = t.filter(pl.col("entry_adj").is_not_null()).with_columns((pl.col("stop0") / pl.col("entry_adj")).alias("r"))
    for d, part in c.group_by("signal_date"):
        want = set(bought.filter(pl.col("signal_date") == d[0])["r"].round(9).to_list())
        assert set(part["stop0"].round(9).to_list()) <= want


def test_a_traded_control_keeps_its_draw_order(market):  # noqa: F811
    paths, ds = market
    rule, t = traded(paths, ds, RULE)
    c = control_signals(paths, "CN", rule, t, per_day=5, seed=0)
    ct = plan_trades(paths, "CN", rule, c, relative=True)
    assert ct.height == c.height and ct.select("listing_id", "signal_date", "draw").sort("signal_date", "draw").equals(
        c.select("listing_id", "signal_date", "draw").sort("signal_date", "draw"))
