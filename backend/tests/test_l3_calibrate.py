"""网格校准 (spec §6.5): a grid over the tuned parameters, scored on your marks, the Pareto front listed."""
from datetime import date

import polars as pl
import pytest

from tests.test_l3_preview import TUNED
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l2.agreement import agreement
from wb.engine.l2.run import compute_market, l2_bars
from wb.engine.l3.calibrate import NEAR, axes, calibrate, group
from wb.engine.l3.preview import preview
from wb.engine.l3.rule import RuleError, load_rule


def test_the_grid_is_the_tuned_parameters_by_their_steps():
    assert axes(load_rule(TUNED)) == {"n": list(range(5, 61))}
    more = TUNED.replace("params:", "params:\n" + "".join(
        f"  - {{id: {x}, value: 1, range: [0, 10], step: 1, tune: true}}\n" for x in "abc")).replace(
        'MA(C, $n)"', 'MA(C, $n) AND V >= $a AND V >= $b AND V >= $c"')
    with pytest.raises(RuleError, match="最多 3 个"):
        axes(load_rule(more))
    two = TUNED.replace("params:", "params:\n  - {id: k, value: 1.0, range: [0.5, 2.0], step: 0.5, tune: true}\n").replace(
        'MA(C, $n)"', 'MA(C, $n) * $k"')
    assert axes(load_rule(two))["k"] == [0.5, 1.0, 1.5, 2.0]                # 56 × 4 = 224 combinations
    big = TUNED.replace("params:", "params:\n  - {id: k, value: 1.0, range: [0.5, 5.0], step: 0.1, tune: true}\n").replace(
        'MA(C, $n)"', 'MA(C, $n) * $k"')
    with pytest.raises(RuleError, match="步长"):
        axes(load_rule(big))                                   # 56 × 46 combinations


def test_marks_go_to_a_tune_or_test_group_once_and_for_all():
    keys = [(f"CN:{600000 + k}", date(2024, 1, 1 + d)) for k in range(10) for d in range(25)]
    groups = [group(lid, d) for lid, d in keys]
    assert groups == [group(lid, d) for lid, d in keys]
    assert 0.6 <= groups.count("tune") / len(groups) <= 0.8


def _marks(paths, rule, lids):
    """Signals under n = 20 marked right when n = 10 also signals within NEAR bars, else wrong."""
    out = []
    for lid in lids:
        a, b = (preview(paths, r, "CN", lid) for r in (rule, rule.with_params({"n": 10})))
        days = a.traded
        near = {days.index(d) for d in b.signals["signal_date"].to_list()}
        for d in a.signals["signal_date"].to_list():
            i = days.index(d)
            ok = any(abs(i - j) <= NEAR for j in near)
            out.append({"listing_id": lid, "signal_date": d, "verdict": "right" if ok else "wrong"})
    return out


def test_every_combination_is_scored_on_your_marks_and_the_front_listed(paths):
    write(paths, 320)
    rule = load_rule(TUNED)
    lids = Store(paths, "CN").read("instruments")["listing_id"][:3].to_list()
    marks = _marks(paths, rule, lids)
    assert {m["verdict"] for m in marks} == {"right", "wrong"}
    r = calibrate(lambda m: paths, rule, marks, years=2)
    assert (r["params"], r["combos"]) == (["n"], 56)
    assert r["current"]["values"] == {"n": 20}
    assert r["current"]["tune"]["right"] == 1.0 and r["current"]["tune"]["wrong"] == 0.0   # it marked its own signals
    top = r["candidates"][0]          # keeps the right, drops the wrong — of such settings the closest to n = 20
    assert top["tune"] == {"right": 1.0, "wrong": 1.0, "boxes": None} and 10 <= top["values"]["n"] < 20
    assert set(top["test"]) == {"right", "wrong", "boxes"} and top["hits"] > 0             # the test group and the sample
    assert r["warning"] is None if len(marks) >= 10 else "过拟合" in r["warning"]
    by = r["samples"]["by_group"]   # how many marks each share is of (67% of 3 is not 67% of 30)
    assert by["tune"]["right"] + by["test"]["right"] == r["samples"]["right"]
    assert by["tune"]["wrong"] + by["test"]["wrong"] == r["samples"]["wrong"]
    assert by["tune"]["right"] * r["current"]["tune"]["right"] == round(by["tune"]["right"] * r["current"]["tune"]["right"])


def test_a_box_label_the_defaults_miss_is_explained_by_the_setting_that_finds_it(paths):
    write(paths, 320)
    rule = load_rule("""
id: tuned_box
name: 调箱体容差
markets: [CN]
params:
  - {id: tol, value: 0.015, range: [0.005, 0.03], step: 0.005, tune: true}
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid, tol: $tol}}
signal_at: box.end
""")
    store = Store(paths, "CN")
    lids = store.read("instruments")["listing_id"].to_list()
    bars = l2_bars(store, "CN", "test", lids)
    closes = bars.select("listing_id", "date", "close")
    boxes = {t: compute_market(bars, ["BOX"], {"BOX": {"tol": t}})["BOX"]["boxes"] for t in (0.015, 0.03)}

    def as_label(b):
        return {"id": "L1", "listing_id": b["listing_id"], "cutoff": b["ended_at"] or b["known_at"], "is_box": True,
                "category": "flat", "start": b["start"], "end": b["end"] or b["known_at"], "top": b["top"],
                "bottom": b["bottom"], "grp": "tune", "redraw_of": None, "verdict": None}

    # your hand-drawn sample: a box tol 0.03 finds that nothing under the default agrees with (§5.10)
    est = boxes[0.03].filter((pl.col("status") == "established") & (pl.col("scale") == "mid"))
    label = next(lb for lb in map(as_label, est.iter_rows(named=True))
                 if not agreement([lb], boxes[0.015], closes)["labels"][0]["consistent"])
    r = calibrate(lambda m: paths, rule, [], labels=[label], years=2)
    assert r["current"]["tune"]["boxes"] == 0.0
    [e] = r["explained"]                                       # the nearest setting under which it agrees
    assert e["label"] == "L1" and e["values"]["tol"] > 0.015


def test_a_mark_on_a_day_without_a_bar_is_simply_not_found(paths):
    write(paths, 320)
    rule = load_rule(TUNED)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    day = preview(paths, rule, "CN", lid).signals["signal_date"][-1]
    marks = [{"listing_id": lid, "signal_date": day, "verdict": "right"},
             {"listing_id": lid, "signal_date": date(1999, 1, 4), "verdict": "right"},     # long before its first bar
             {"listing_id": "CN:688999", "signal_date": day, "verdict": "right"}]          # a stock with no bars
    r = calibrate(lambda m: paths, rule, marks, years=2)
    found = {g: r["current"][g]["right"] for g in ("tune", "test")}
    n = {g: [group(m["listing_id"], m["signal_date"]) for m in marks].count(g) for g in ("tune", "test")}
    assert sum(found[g] * n[g] for g in n if found[g] is not None) == pytest.approx(1)   # only the real one is found


def test_every_built_in_rule_with_tuned_parameters_fits_the_grid():
    from wb.workspace.rules import builtin_rules
    for rid, (rule, _) in builtin_rules().items():
        if any(p.tune for p in rule.params):
            axes(rule)                                           # ≤ 3 tuned, ≤ 500 combinations: the 校准 tab can run it


def test_a_market_without_data_is_left_out_and_named(paths, tmp_path):
    from wb.config import get_paths
    write(paths, 320)
    rule = load_rule(TUNED)                                         # [CN, JP]
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    marks = [{"listing_id": lid, "signal_date": d, "verdict": "right"}
             for d in preview(paths, rule, "CN", lid).signals["signal_date"].to_list()[-3:]]
    empty = get_paths("test", tmp_path / "nothing-yet")
    r = calibrate(lambda m: paths if m == "CN" else empty, rule, marks, years=2)
    assert r["skipped"] == ["JP"] and r["current"]["hits"] >= 0


def test_marks_older_than_the_years_still_count(paths):
    write(paths, 320)
    rule = load_rule(TUNED)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    days = preview(paths, rule, "CN", lid).signals["signal_date"].to_list()
    marks = [{"listing_id": lid, "signal_date": d, "verdict": "right"} for d in days[:4]]     # the oldest signals
    r = calibrate(lambda m: paths, rule, marks, years=1)
    assert all(r["current"][g]["right"] in (1.0, None) for g in ("tune", "test"))           # the rule still finds its own


def test_a_parameter_sets_a_structure_only_by_its_own_name():
    from wb.engine.l3.calibrate import _in_structure
    rule = load_rule("""
id: t
name: t
markets: [CN]
params:
  - {id: tol, value: 1.5, range: [1.0, 2.0], step: 0.5, tune: true}
  - {id: tol_box, value: 0.015, range: [0.01, 0.02], step: 0.005}
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid, tol: $tol_box}}
  - {id: up, kind: event, on: box.break_up, at: [{id: v, expr: "VOL_RATIO(5) >= $tol"}]}
signal_at: up.at
""")
    assert _in_structure(rule, "tol_box") and not _in_structure(rule, "tol")


RANKED = TUNED.replace('"C > MA(C, $n)"', '"C > MA(C, $n) AND RANK_XS(C / REF(C, 1)) >= 0.5"')
RANKED_N = TUNED.replace('"C > MA(C, $n)"', '"RANK_XS(C / MA(C, $n)) >= 0.5"')   # the tuned value inside the rank


def _found_by_preview(paths, rule, lid, marks, points):
    """What each point finds, the rule previewed on the listing (the market read whole) point by point."""
    out = []
    for p in points:
        pv = preview(paths, rule.with_params(p), "CN", lid)
        near = [pv.traded.index(d) for d in pv.signals["signal_date"].to_list()]
        out.append([any(abs(pv.traded.index(m["signal_date"]) - j) <= NEAR for j in near) for m in marks])
    return out


@pytest.mark.parametrize("text, whole", [(RANKED, 1), (RANKED_N, 56)])
def test_a_cross_sectional_rule_is_scored_on_the_whole_market_computing_each_rank_once(paths, monkeypatch, text, whole):
    from wb.engine.l1 import run as l1
    from wb.engine.l3 import calibrate as cal
    write(paths, 320)
    rule, lid = load_rule(text), "CN:600519"
    marks = [{"listing_id": lid, "signal_date": d, "verdict": "right"}
             for d in preview(paths, rule.with_params({"n": 40}), "CN", lid).signals["signal_date"].to_list()[::7]]
    points = [{"n": n} for n in range(5, 61)]
    want = _found_by_preview(paths, rule, lid, marks, points)
    assert any(want[0]) and not all(map(all, want))                                       # the points differ
    heights = []
    real = l1.evaluate
    monkeypatch.setattr(l1, "evaluate", lambda f, *a, **k: heights.append(f.height) or real(f, *a, **k))
    for mod in ("wb.engine.l3.match", "wb.engine.l3.calibrate"):
        if hasattr(__import__(mod, fromlist=["x"]), "evaluate"):
            monkeypatch.setattr(f"{mod}.evaluate", l1.evaluate)
    assert cal._score_marks(paths, rule, "CN", points, marks, years=2) == want
    rows = Store(paths, "CN").read("bars_1d").height
    assert sum(h == rows for h in heights) == whole                                     # the whole market, once per rank


def test_the_candidates_hits_build_the_whole_market_once_per_market(paths, monkeypatch):
    import json

    from wb.engine.l3 import run as l3run
    write(paths, 320)
    rule = load_rule(RANKED)
    marks = _marks(paths, rule, ["CN:600519"])
    built = []
    real = l3run.build_frame
    monkeypatch.setattr(l3run, "build_frame", lambda store, market, profile, columns, listings, **k:
                        built.append((market, listings)) or real(store, market, profile, columns, listings, **k))
    r = calibrate(lambda m: paths, rule, marks, years=2)
    assert len({json.dumps(c["values"]) for c in [r["current"], *r["candidates"]]}) >= 2
    assert sorted(m for m, listings in built if listings is None) == ["CN", "CN", "JP"]  # the marks, then every hit count
