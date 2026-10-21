"""候选条件归因 (spec §9.5): each candidate feature on the signal day, its in-sample quintiles and each half's 20-day
excess by group; a filter is suggested only where both halves rise (or fall) group by group the same way — and never
on outcomes shuffled at random (§13 P6)."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.validate.attribution import grouped, suggestions

OOS = date(2024, 1, 1)
SPECS = [{"id": "f", "label": "特征 f", "expr": "F_EXPR"}, {"id": "g", "label": "噪声 g", "expr": "G_EXPR"}]


def rows(days=400, per_day=3, seed=1, planted=True):
    """Signals over `days` (half before OOS): `f` uniform 0–1 that x20 rises with (when planted), `g` noise."""
    rng = np.random.default_rng(seed)
    out, d0 = [], OOS - timedelta(days=days // 2)
    for k in range(days):
        for j in range(per_day):
            f, g = rng.uniform(), rng.uniform()
            x = (0.08 * f - 0.03 if planted else 0.0) + rng.normal(0, 0.02)
            out.append(("CN", f"CN:{600000 + j}", d0 + timedelta(days=k), None, x, f, g))
    return pl.DataFrame(out, schema={"market": pl.Utf8, "listing_id": pl.Utf8, "signal_date": pl.Date, "blocked": pl.Utf8,
                                     "x20": pl.Float64, "f": pl.Float64, "g": pl.Float64}, orient="row")


def test_a_feature_the_outcome_rises_with_is_monotone_in_both_halves():
    r = grouped(rows(), "f", OOS)
    assert r["direction"] == "up" and r["verdict"] == "单调" and len(r["groups"]) == 5
    meds = [[gr[h]["median"] for gr in r["groups"]] for h in ("in", "out")]
    assert all(a < b for m in meds for a, b in zip(m, m[1:], strict=False))
    assert r["cuts"][0] == pytest.approx(0.2, abs=0.05) and r["groups"][0]["hi"] == r["cuts"][0]
    assert grouped(rows(), "g", OOS)["direction"] is None                                   # noise: no order


def test_a_filter_is_suggested_with_what_it_changes():
    r = rows()
    res = {s["id"]: grouped(r, s["id"], OOS) for s in SPECS}
    got = suggestions(r, SPECS, OOS, res)
    assert [s["feature"] for s in got] == ["f"]
    s = got[0]
    assert s["condition"].startswith("F_EXPR > 0.") and s["after"]["signals"] < s["before"]["signals"]
    assert s["after"]["win_out"] > s["before"]["win_out"] and s["after"]["win_in"] > s["before"]["win_in"]


def test_shuffled_outcomes_get_no_suggestion():
    # §13 P6: the forward returns shuffled at random, attribution suggests nothing
    r = rows()
    for seed in range(5):
        shuffled = r.with_columns(pl.Series("x20", np.random.default_rng(seed).permutation(r["x20"].to_numpy())))
        res = {s["id"]: grouped(shuffled, s["id"], OOS) for s in SPECS}
        assert suggestions(shuffled, SPECS, OOS, res) == []


def test_a_feature_with_one_value_mostly_cuts_into_empty_groups_without_a_conclusion():
    # 换手率 0 on most days: the quintile cuts coincide, groups go empty — numbers only, nothing suggested
    r = rows().with_columns(pl.when(pl.col("f") < 0.9).then(0.0).otherwise(pl.col("f")).alias("f"))
    res = grouped(r, "f", OOS)
    assert res["verdict"] == "样本太少" and any(g["in"]["effective"] == 0 for g in res["groups"])
    assert suggestions(r, SPECS, OOS, {"f": res, "g": grouped(r, "g", OOS)}) == []



def test_no_pressure_above_is_more_space_than_any_cut():
    # pressure.space is +inf when no box is above (上方没有压力): the most room, in the top fifth, and the cuts
    # stay numbers a condition can be written with
    r = rows()
    r = r.with_columns(pl.when(pl.col("f") > 0.7).then(float("inf")).otherwise(pl.col("f")).alias("f"))
    res = grouped(r, "f", OOS)
    assert all(np.isfinite(res["cuts"])) and res["direction"] == "up"
    top = r.filter(pl.col("f") > res["cuts"][-1])
    assert top.filter(pl.col("f").is_infinite()).height == r.filter(pl.col("f").is_infinite()).height
    assert suggestions(r, SPECS, OOS, {"f": res})[0]["condition"].startswith("F_EXPR > 0.")

def test_too_few_samples_give_numbers_but_no_conclusion():
    r = grouped(rows(days=60), "f", OOS)
    assert r["verdict"] == "样本太少" and r["direction"] is None and r["groups"]


def test_the_candidates_are_the_presets_and_the_box_s_shape_when_the_rule_has_one(paths):
    from wb.data.store import Store
    from wb.engine.l1.frame import available_columns
    from wb.engine.l3.rule import load_rule
    from wb.validate.attribution import FEATURES, candidates, feature_rule
    from wb.workspace.rules import builtin_rules
    boxed = builtin_rules()["tpl_shang_pin"][0]
    stage = next(s.id for s in boxed.stages if s.use == "BOX")
    assert [f["id"] for f in candidates(boxed)][-4:] == ["box_width", "box_length", "box_bar", "box_bar_rel"]
    assert [f["expr"] for f in candidates(boxed)][-4:-2] == [f"{stage}.width", f"COUNT(RAW_C > 0, of={stage})"]
    plain = load_rule(RULE_JP)
    assert candidates(plain) == FEATURES
    # a market whose data has no valuation: 总市值 and 换手 are left out, said why; the rest rank the rule's signals
    ranked, ok, out = feature_rule(plain, "JP", available_columns(Store(paths, "JP")), FEATURES)
    assert {f["id"] for f in out} == {"mv", "turnover20"} and all(f["why"] for f in out)
    assert [r.expr for r in ranked.ranking] == [f["expr"] for f in ok] and len(ok) == len(FEATURES) - 2


RULE_JP = """
id: plain
name: 站上均线
markets: [JP]
stages:
  - {id: today, kind: event, when: "C > MA(C, 20)"}
signal_at: today.at
"""


def test_two_markets_are_grouped_each_on_its_own_and_get_no_suggestion():
    # 元 and 円 (and two markets' own levels) cut at one set of quintiles would make the groups the markets: a market's
    # better outcomes would read as a feature's. Each market is grouped on its own; a filter only for one market's check-up
    from wb.validate.attribution import attribute
    cn = rows(seed=3)
    jp = rows(seed=4).with_columns(pl.lit("JP").alias("market"), (pl.col("f") * 1000).alias("f"), (pl.col("x20") + 0.01).alias("x20"))
    a = attribute(pl.concat([cn, jp]), SPECS, OOS, "today")
    assert {(f["id"], f["market"]) for f in a["features"]} == {("f", "CN"), ("f", "JP"), ("g", "CN"), ("g", "JP")}
    assert next(f for f in a["features"] if f["id"] == "f" and f["market"] == "JP")["cuts"][0] > 100   # its own quintiles
    assert a["suggestions"] == [] and a["markets"] == ["CN", "JP"]
    assert [s["feature"] for s in attribute(cn, SPECS, OOS, "today")["suggestions"]] == ["f"]           # one market: suggested


def test_a_feature_unknown_on_every_signal_of_a_market_is_said_not_counted():
    from wb.validate.attribution import attribute
    r = rows().with_columns(pl.lit(None, pl.Float64).alias("g"))
    a = attribute(r, SPECS, OOS, "today")
    assert [f["id"] for f in a["features"]] == ["f"] and [(s["id"], s["market"]) for s in a["skipped"]] == [("g", "CN")]
    assert "算不出" in a["skipped"][0]["why"]


def test_a_box_s_length_is_read_as_its_bars_so_far_on_a_breakout(paths):
    # a box's own length is known only once it ended (after the break): the candidate counts its bars up to the signal
    from tests.test_validate_engine import ALL, boxes_market
    from wb.data.store import Store
    from wb.engine.l1.frame import available_columns
    from wb.validate.attribution import candidates, feature_rule
    from wb.validate.engine import Variant, market_signals
    from wb.workspace.rules import builtin_rules
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_pin"][0]
    spec = next(f for f in candidates(rule) if f["id"] == "box_length")
    ranked, ok, _ = feature_rule(rule, "CN", available_columns(Store(paths, "CN")), [spec])
    sig = market_signals(paths, "CN", [Variant(ranked)], *ALL, lines=True)[0]
    assert ok and sig.height and sig["rank0"].null_count() == 0 and sig["rank0"].min() >= 5


def test_a_box_s_candles_are_measured_on_its_bars_and_against_the_market(paths):
    # short, even candles mean controlled trading: the candles' mean length in the box (high − low over the close before),
    # and that over the index's mean daily move in the same bars (a calm market makes every box's candles short)
    from tests.test_validate_engine import ALL, boxes_market
    from wb.data.store import Store
    from wb.engine.l1.frame import available_columns
    from wb.validate.attribution import candidates, feature_rule
    from wb.validate.engine import Variant, market_signals
    from wb.workspace.rules import builtin_rules
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_pin"][0]
    specs = [f for f in candidates(rule) if f["id"] in ("box_bar", "box_bar_rel")]
    ranked, ok, out = feature_rule(rule, "CN", available_columns(Store(paths, "CN")), specs)
    assert len(ok) == 2 and not out
    sig = market_signals(paths, "CN", [Variant(ranked)], *ALL, lines=True)[0]
    assert sig.height and sig["rank0"].null_count() == 0 and 0 < sig["rank0"].min() and sig["rank0"].max() < 0.2
    assert sig["rank1"].null_count() == 0 and sig["rank1"].min() > 0


def test_a_gold_rule_is_offered_its_small_box_s_candles(paths):
    # 天品 / 上品 / 下品 rules bind a 金品 (GOLD), whose bars are its small box's — the consolidation the signal breaks
    from tests.test_validate_engine import ALL, boxes_market
    from wb.data.store import Store
    from wb.engine.l1.frame import available_columns
    from wb.validate.attribution import candidates, feature_rule
    from wb.validate.engine import Variant, market_signals
    from wb.workspace.rules import builtin_rules
    boxes_market(paths)
    rule = builtin_rules()["tpl_tian_pin"][0]
    specs = {f["id"]: f for f in candidates(rule)}
    assert "box_bar" in specs and "of=g)" in specs["box_bar"]["expr"] and "box_width" not in specs
    ranked, ok, out = feature_rule(rule, "CN", available_columns(Store(paths, "CN")), [specs["box_bar"], specs["box_bar_rel"]])
    assert len(ok) == 2 and not out


def test_by_the_plan_groups_are_ordered_by_their_expectancy_and_a_filter_must_raise_it_in_both_halves():
    # design 2026-10-09 §8: a check-up by the trade plan reads the plan's return (`ret`), each group's mean per signal day
    from wb.validate.attribution import PLAN
    r = rows().rename({"x20": "ret"})
    res = {s["id"]: grouped(r, s["id"], OOS, PLAN) for s in SPECS}
    assert res["f"]["direction"] == "up" and res["g"]["direction"] is None
    got = suggestions(r, SPECS, OOS, res, PLAN)
    assert [s["feature"] for s in got] == ["f"]
    assert got[0]["after"]["mean_in"] > got[0]["before"]["mean_in"] and got[0]["after"]["mean_out"] > got[0]["before"]["mean_out"]
