"""Explanations are rendered from the match result (spec §5.6)."""
from tests.test_l3_match import BREAKOUT, CLOSE, D, HIGH, LID, VOL, matcher
from wb.engine.l3.explain import render


def test_a_signal_lists_its_stages_and_passed_conditions():
    lines = render(matcher().detail(LID, D(33)))
    assert lines[0] == f"{LID} {D(33)} 信号"
    assert any(line.startswith("  中箱：") for line in lines)           # a stage reads by name: its structure here
    assert "  ✓ 向上突破 · volume：2.333 ≥ 2" in lines                    # an unnamed condition: its stage's name and its id
    assert "    公式：BAND_VOL_RATIO(box) ≥ $spike" in lines               # the formula as written, below
    assert "  ✓ hold：3/3 根满足" in lines and "    公式：C > box.top" in lines


def test_a_fail_names_how_far_it_got_and_what_was_missing():
    m = matcher(volume=VOL[:30] + [2500.0] + VOL[31:])
    lines = render(m.detail(LID, D(33)))
    assert lines[0].endswith("未通过（走到 中箱）")
    assert "  差在：向上突破 · volume，现在 1.667，要 ≥ 2" in lines


def test_a_watch_shows_the_distance_to_the_next_stage():
    lines = render(matcher().detail(LID, D(29)))
    assert lines[0].endswith("潜伏")
    assert "  距上沿：收 11.45，上沿 12，还要涨 4.8%（突破线 12.18，上沿之上 1.5%）" in lines   # one 距上沿, app-wide
    assert "  距离向上突破 · volume：现在 0.3333，要到 2，还差 1.667" in lines


def test_no_pressure_above_reads_as_unlimited_space():
    from wb.engine.l3.explain import num
    assert num(float("inf")) == "∞" and num(0.1032) == "0.1032"


def test_an_unknown_lists_its_reasons():
    from tests.test_l3_match import unknown_matcher
    lines = render(unknown_matcher().detail(LID, D(33)))
    assert lines[0] == f"{LID} {D(33)} 无法判断（rally 就无法判断）"     # a candidate exists: say where it stopped
    assert "  无法判断的原因：窗口要从第一根 K 线之前 5 根开始（历史不够）" in lines
    assert f"  无法判断的原因：{D(31)} 数据有问题（成交额÷成交量不在当天高低价之间）" in lines
    assert "  无法判断的原因：到这天只有 34 根 K 线，可能比公式要的窗口短" in lines


def test_a_miss_on_an_every_bar_condition_counts_bars():
    m = matcher(close=CLOSE[:32] + [11.9] + CLOSE[33:])             # bar 32 closes back under the box top
    lines = render(m.detail(LID, D(33)))
    assert "  差在：hold，3 根里 2 根满足" in lines


def test_a_watch_already_past_the_edge_waits_for_the_break_to_be_confirmed():
    detail = matcher().detail(LID, D(29))
    detail["distances"][0]["value"] = 12.5                       # closed above top × (1 + tol), break not yet confirmed
    lines = render(detail)
    assert "  距上沿：收 12.5，已越过上沿 12 有 4.2%，过了突破线 12.18，待确认" in lines
    detail["distances"][0]["value"] = 12.1                       # over the edge, short of the break line
    assert "  距上沿：收 12.1，已越过上沿 12 有 0.8%，还没到突破线 12.18" in render(detail)


def test_a_box_reads_as_its_scale_and_a_window_with_its_bars_not_as_ids():
    # the structure's id (JP:130A:mid:12) is the program's; you read 中箱
    detail = {"listing_id": "JP:130A", "date": "2026-09-30", "status": "watch", "deepest_stage": None, "closest_miss": None,
              "bindings": [{"stage": "rally", "kind": "window", "id": None, "start": "2026-06-16", "end": "2026-08-10", "at": None, "bars": 40},
                           {"stage": "box", "kind": "structure", "id": "JP:130A:mid:12", "start": "2026-08-11", "end": "2026-09-30",
                            "at": "2026-09-08", "bars": 36}],
              "conditions": [], "distances": [], "unknown_reasons": []}
    lines = render(detail)
    assert "  rally：2026-06-16 至 2026-08-10（40 根）" in lines
    assert "  box：中箱，2026-08-11 至 2026-09-30，2026-09-08 成立" in lines


LABELED = BREAKOUT.replace("  - {id: spike, value: 2.0}", "  - {id: spike, value: 2.0, unit: 倍}\n  - {id: rise, value: 0.15, unit: \"%\"}") \
    .replace("  - id: rally\n    kind: window", "  - id: rally\n    label: 上涨段\n    kind: window") \
    .replace('{id: runup, expr: "RUNUP(H, L) >= 0.15"}', '{id: runup, label: 进箱前涨幅, expr: "RUNUP(H, L) >= $rise"}') \
    .replace('{id: volume, expr: "BAND_VOL_RATIO(box) >= $spike"}', '{id: volume, label: 突破量, expr: "BAND_VOL_RATIO(box) >= $spike"}')


def test_named_stages_and_conditions_read_in_their_units_with_the_formula_below():
    import re
    lines = render(matcher(rule=LABELED).detail(LID, D(33)))
    assert any(line.startswith("  上涨段：") and line.endswith("根）") for line in lines)
    assert "  ✓ 突破量：2.333 倍 ≥ 2 倍" in lines                       # a multiple reads 倍
    assert any(re.fullmatch(r"  ✓ 进箱前涨幅：\d+(\.\d+)?% ≥ 15%", line) for line in lines), lines   # a share reads %
    assert "    公式：RUNUP(H, L) ≥ $rise" in lines


def test_a_condition_that_is_simply_not_met_says_so_not_a_zero():
    detail = {"listing_id": "CN:600519", "date": "2026-09-30", "status": "fail", "deepest_stage": None, "bindings": [],
              "conditions": [], "distances": [], "unknown_reasons": [],
              "closest_miss": {"stage": "today", "id": "when", "kind": "at", "op": None, "value": 0, "threshold": None},
              "names": {"stages": {"today": "信号"}, "conds": {}, "units": {}}}
    assert "  差在：信号（不满足）" in render(detail)


def test_a_rule_without_names_reads_by_its_structures_and_events_and_a_lone_event_is_the_signal():
    from wb.engine.l3.rule import load_rule
    from wb.engine.l3.understand import names
    n = names(load_rule(BREAKOUT))
    assert n["stages"] == {"rally": "rally", "box": "中箱", "breakout": "向上突破", "hold": "hold"}
    lone = load_rule("\n".join(["id: w", "name: 站上均线", "markets: [CN]", "params:", "  - {id: n, value: 20}", "stages:",
                                '  - {id: today, kind: event, when: "C > MA(C, $n)"}', "signal_at: today.at", ""]))
    assert names(lone)["stages"] == {"today": "信号"}
    assert names(load_rule(LABELED))["units"]["breakout.volume"] == "倍"


def test_a_unit_comes_from_the_condition_or_the_parameter_it_compares_with_and_a_gold_reads_as_its_kind():
    from wb.engine.l3.rule import load_rule
    from wb.engine.l3.understand import names
    n = names(load_rule("\n".join([
        "id: u", "name: u", "markets: [CN]", "params:", "  - {id: q, value: 1.0, unit: 倍}", "stages:",
        "  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, variant: true}}",
        "  - {id: d, kind: structure, use: MACD_DIV, with: {kind: bull}}",
        "  - id: up", "    kind: event", "    on: g.break_up", "    at:",
        '      - {id: quiet, expr: "MA(V, 5) <= REF(MA(V, 20), 5) * $q"}',
        '      - {id: vr, expr: "VOL_RATIO(5) >= $q"}',
        '      - {id: rise, expr: "RUNUP(H, L) >= 0.2", unit: "%"}',
        '      - {id: both, expr: "C > O AND VOL_RATIO(5) >= $q"}', "signal_at: up.at", ""])))
    assert (n["stages"]["g"], n["stages"]["d"]) == ("变异下品", "MACD 底背离")   # a 金品 by its kind, a 背离 by its side
    # compared with the parameter: its unit; the parameter inside a computed threshold: none (the values are volumes);
    # a fixed threshold: the condition's own
    assert (n["units"]["up.quiet"], n["units"]["up.vr"], n["units"]["up.rise"], n["units"]["up.both"]) == ("", "倍", "%", "")


def test_an_explanation_reads_its_listing_alone_and_says_the_same(paths):
    # each listing's explanation reads its own rows of the frame (a slice, nothing copied) — read over the whole
    # market it took ~1 s a listing, ~25 min for 1,600 潜伏 of one rule on A shares: the same tree for every listing a
    # scan explains, its signals, watches, unknowns and the ones that got part of the way
    import json

    import polars as pl

    from tests.test_validate_engine import boxes_market
    from wb.engine.l3.run import run_rule
    from wb.workspace.rules import builtin_rules
    boxes_market(paths)
    told = set()
    for rid in ("tpl_calm_breakout", "tpl_shang_pin", "long13_oral"):
        run = run_rule(paths, builtin_rules()[rid][0], "CN", record=False)
        m, day = run.matcher, run.day
        ends = sorted(run.rows["listing_id"].to_list())
        lids = run.rows.filter(pl.col("status") != "fail")["listing_id"].to_list() + ends[:2] + ends[-2:]   # the frame's first and last
        for lid in lids:
            assert json.dumps(m.detail(lid, day), default=str) == json.dumps(m._detail(lid, day), default=str), (rid, lid)
        told |= set(run.rows.filter(pl.col("listing_id").is_in(lids))["status"].to_list())
    assert {"signal", "watch", "fail"} <= told
