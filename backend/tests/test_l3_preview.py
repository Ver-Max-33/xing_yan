"""Single-stock debugging and the sample gallery (spec §6.3)."""
import polars as pl
import pytest

from tests.test_l3_run import ABOVE_MA
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l3.preview import gallery, preview, sample
from wb.engine.l3.rule import RuleError, load_rule
from wb.engine.l3.understand import names

TUNED = ABOVE_MA.replace("signal_at: today.at", """params:
  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}
signal_at: today.at""").replace("MA(C, 20)", "MA(C, $n)")


def test_a_preview_is_the_rule_on_one_listing_with_every_structure(paths):
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    p = preview(paths, load_rule(ABOVE_MA), "CN", lid)
    assert p.signals.height > 0 and set(p.signals["listing_id"]) == {lid}
    assert {"BOX", "PIVOTS", "SWING", "SLANT_BOX", "MACD_DIV", "BOX_DERIVED"} <= set(p.tables)   # the chart draws them all
    day = p.signals["signal_date"][0]
    assert p.matcher.detail(lid, day)["status"] == "signal"


def test_slider_values_replace_the_rule_parameters(paths):
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    rule = load_rule(TUNED)
    a = preview(paths, rule, "CN", lid).signals.height
    b = preview(paths, rule.with_params({"n": 5}), "CN", lid).signals.height
    assert a != b
    with pytest.raises(RuleError, match="n"):
        rule.with_params({"n": 500})                                    # outside its range
    with pytest.raises(RuleError, match="m"):
        rule.with_params({"m": 5})                                      # no such parameter


def test_a_sample_is_fixed_by_its_seed_and_the_gallery_lists_hits(paths):
    write(paths, 320)
    s1, s2 = sample(paths, "CN", 1, seed=7), sample(paths, "CN", 1, seed=7)
    assert s1 == s2 and len(s1) == 1
    assert len(sample(paths, "CN", 300)) == 2                          # the dev market has two listings
    hits = gallery(paths, load_rule(ABOVE_MA), "CN", sample(paths, "CN", 300), years=1)
    assert hits.height > 0 and hits.columns[:2] == ["listing_id", "signal_date"]


def test_a_missed_hit_names_the_closest_miss_and_what_relaxing_it_would_do(paths):
    from wb.engine.l3.preview import missed
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    rule = load_rule(TUNED.replace('when: "C > MA(C, $n)"', 'when: "C > 0", at: [{id: up, expr: "C / REF(C, 1) - 1 >= $gain"}]')
                     .replace("  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}",
                              "  - {id: gain, value: 0.5, range: [0.0, 0.6], step: 0.01, tune: true}"))
    bars = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == lid).sort("date")
    start, end = bars["date"][-30], bars["date"][-1]
    out = missed(paths, rule, "CN", lid, start, end, sample(paths, "CN", 300), years=1)
    assert out["status"] == "missed" and out["miss"]["id"] == "up" and out["param"] == "gain"
    assert out["names"]["stages"] == names(rule)["stages"]   # the page says the stage and the miss by name
    assert out["relaxed"] < 0.5 and out["hits_before"] == 0 and out["hits_after"] > 0
    assert out["day"] is not None and out["lines"][0].endswith("未通过（信号就没通过）")


def test_the_gallery_shows_a_run_of_consecutive_hit_days_once():
    import tempfile

    from wb.config import get_paths
    paths = get_paths("test", tempfile.mkdtemp())
    write(paths, 320)
    hits = gallery(paths, load_rule(ABOVE_MA), "CN", sample(paths, "CN", 300), years=1)
    assert "days" in hits.columns and hits["days"].min() >= 1 and hits["days"].max() > 1
    bars = Store(paths, "CN").read("bars_1d").sort("listing_id", "date").with_columns(
        pl.int_range(pl.len()).over("listing_id").alias("i"))
    i = hits.join(bars.select("listing_id", pl.col("date").alias("signal_date"), "i"), on=["listing_id", "signal_date"]).sort(
        "listing_id", "signal_date")
    gaps = i.with_columns(pl.col("i").diff().over("listing_id").alias("gap"))["gap"].drop_nulls()
    assert gaps.min() > 1                                                  # never two hits on neighbouring bars


def test_two_versions_are_compared_on_the_same_sample(paths):
    from wb.engine.l3.preview import compare_versions
    write(paths, 320)
    s = sample(paths, "CN", 300)
    a, b = load_rule(TUNED), load_rule(TUNED).with_params({"n": 5})
    out = compare_versions(paths, a, b, "CN", s, years=1)
    ga, gb = gallery(paths, a, "CN", s, years=1), gallery(paths, b, "CN", s, years=1)
    key = lambda g: set(zip(g["listing_id"], g["signal_date"], strict=True))  # noqa: E731
    assert {(h["listing_id"], h["signal_date"]) for h in out["added"]} == key(gb) - key(ga)
    assert {(h["listing_id"], h["signal_date"]) for h in out["lost"]} == key(ga) - key(gb)
    assert out["kept"] == len(key(ga) & key(gb))


def test_a_parameter_is_whole_only_when_its_value_step_and_range_are():
    rule = load_rule(TUNED.replace("  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}",
                                   "  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}\n"
                                   "  - {id: gain, value: 0, range: [0, 0.6], step: 0.01, tune: true}")
                     .replace('MA(C, $n)"', 'MA(C, $n) AND C / REF(C, 1) - 1 >= $gain"'))
    assert rule.with_params({"gain": 0.05}).param("gain") == 0.05             # written 0 but stepped by 0.01
    assert rule.with_params({"n": 5.0}).param("n") == 5 and isinstance(rule.with_params({"n": 5.0}).param("n"), int)
    with pytest.raises(RuleError):
        rule.with_params({"n": 5.5})


def test_a_missed_hit_relaxes_a_parameter_written_on_the_left(paths):
    from wb.engine.l3.preview import missed
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    rule = load_rule(TUNED.replace('when: "C > MA(C, $n)"', 'when: "C > 0", at: [{id: up, expr: "$gain <= C / REF(C, 1) - 1"}]')
                     .replace("  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}",
                              "  - {id: gain, value: 0.5, range: [0.0, 0.6], step: 0.01, tune: true}"))
    bars = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == lid).sort("date")
    out = missed(paths, rule, "CN", lid, bars["date"][-30], bars["date"][-1], sample(paths, "CN", 300), years=1)
    assert out["param"] == "gain" and out["relaxed"] < 0.5 and out["hits_after"] > 0


def test_the_sample_and_a_market_wide_run_are_of_stocks(paths):
    # spec §7.1 股票池: a fund (an ETF on the JP list) is in neither the tuning sample nor a market-wide signal run
    from wb.validate.engine import Variant, market_signals
    write(paths, 320)
    st = Store(paths, "JP")
    inst = st.read("instruments")
    st.publish({"instruments": inst.filter(pl.col("listing_id") == "JP:130A").with_columns(pl.lit("ETF・ETN").alias("board"))},
               as_of=st.latest().as_of)
    assert sample(paths, "JP", 300) == ["JP:6758", "JP:7203"]
    rule = load_rule(ABOVE_MA.replace("markets: [CN]", "markets: [JP]"))
    days = st.read("calendar").filter(pl.col("is_open"))["date"]
    sig = market_signals(paths, "JP", [Variant(rule)], days.min(), days.max())[0]
    assert sig.height > 0 and "JP:130A" not in set(sig["listing_id"])
