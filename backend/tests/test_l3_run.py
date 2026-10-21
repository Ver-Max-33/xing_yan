"""Running rules over a snapshot (spec §5.5 modes, §5.9 run records, §7.1 stored results)."""
import json
import threading

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate
from wb.engine.l3.rule import RuleError, load_rule
from wb.engine.l3.run import primitives, run_rows, run_rule, runs
from wb.workspace.rules import Rules

ABOVE_MA = """
id: above_ma
name: 站上 20 日线
markets: [CN, JP]
stages:
  - {id: today, kind: event, when: "C > MA(C, 20)"}
signal_at: today.at
"""


def test_a_date_run_judges_every_listing_and_is_recorded(paths):
    write(paths, 320)
    run = run_rule(paths, load_rule(ABOVE_MA), "CN")
    assert run.mode == "date" and run.rows.height == 2 and set(run.rows["status"]) <= {"signal", "fail", "unknown"}
    assert sum(run.counts.values()) == 2 and run.rows["name"].null_count() == 0
    [rec] = runs(paths)
    assert rec["rule_id"] == "above_ma" and rec["snapshot_id"] == run.snapshot_id and rec["counts"] == run.counts
    assert_frame_equal(run_rows(paths, run.id), run.rows)
    # the same answer as the formula on its own (L1 and L3 agree)
    st = Store(paths, "CN")
    comp = Compiler("CN", available=available_columns(st))
    comp.add("x", "C > MA(C, 20)")
    f = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program())
    hits = set(f.filter((pl.col("date") == run.day) & pl.col("x"))["listing_id"])
    assert hits == set(run.rows.filter(pl.col("status") == "signal")["listing_id"])
    detail = json.loads(run.rows.filter(pl.col("status") == "signal")["detail"][0]) if hits else None
    assert detail is None or detail["status"] == "signal"


def test_a_structure_rule_runs_on_the_market_and_on_chosen_listings(paths):
    write(paths, 320)
    rule = Rules(paths).get("box_breakout_after_rally")[0]
    # the demo listings trade less than the rule's liquidity floor: drop it so they stay in the pool
    rule = rule.model_copy(update={"universe": rule.universe.model_copy(update={"min_avg_amount_20d": None})})
    assert primitives(rule) == ["BOX", "BOX_DERIVED", "SLANT_BOX"]   # its plan's 失效线 is the box's 假底 (BOX_DERIVED)
    one = run_rule(paths, rule, "CN", listings=["CN:000001"])
    assert one.rows["listing_id"].to_list() == ["CN:000001"]
    assert not list((paths.profile_dir / "derived").glob("*/l2"))  # chosen listings are computed, not cached
    market = run_rule(paths, rule, "CN")
    assert market.rows.height == 2 and list((paths.profile_dir / "derived").glob("*/l2/BOX-*"))
    assert one.rows.drop("detail").equals(market.rows.filter(pl.col("listing_id") == "CN:000001").drop("detail"))


def test_a_range_run_lists_signals(paths):
    write(paths, 320)
    first = Store(paths, "JP").read("bars_1d")["date"].min()
    run = run_rule(paths, load_rule(ABOVE_MA), "JP", start=first)
    assert run.mode == "range" and run.rows.height > 0
    assert run.rows["signal_date"].is_sorted() and run.counts == {"signal": run.rows.height}


def test_a_preview_leaves_no_record(paths):
    write(paths, 320)
    run_rule(paths, load_rule(ABOVE_MA), "CN", record=False)
    assert runs(paths) == [] and not (paths.profile_dir / "runs").exists()


def test_a_rule_reading_pressure_gets_the_boxes():
    rule = load_rule("""
id: s
name: 上方空间
markets: [CN]
stages:
  - {id: day, kind: event, when: "C > 0", at: [{id: space, expr: "pressure.space >= 0.1"}]}
signal_at: day.at
""")
    assert "BOX" in primitives(rule)



def test_a_rule_reading_a_box_s_false_bottom_gets_the_derived_structures():
    # a plan's 失效线 box.false_bottom (design 2026-10-09 §3) is BOX_DERIVED's 假底: the run computes it
    rule = load_rule("""
id: f
name: 假底止损
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
plan: {stop: [{line: "box.false_bottom"}]}
""")
    assert "BOX_DERIVED" in primitives(rule)

def test_a_run_records_the_same_content_hash_as_the_saved_version(paths):
    write(paths, 320)
    rules = Rules(paths)
    rid, version, _ = rules.save(ABOVE_MA)
    rule, _, version = rules.get(rid)
    run = run_rule(paths, rule, "CN", version=version)
    assert runs(paths)[0]["rule_hash"] == rules.versions(rid)[-1]["hash"]
    assert runs(paths)[0]["id"] == run.id


def test_a_window_parameter_moves_the_signal_as_the_number_would(paths):
    from tests.test_l3_rule import HOLD
    write(paths, 320)
    first = Store(paths, "JP").read("bars_1d")["date"].min()
    rule = load_rule(HOLD)
    written = load_rule(HOLD.replace("$hold]", "3]").replace("params:\n  - {id: hold, value: 3, range: [1, 10], step: 1}\n", ""))
    a = run_rule(paths, rule, "JP", start=first, record=False).rows
    assert a.height > 0 and a.equals(run_rule(paths, written, "JP", start=first, record=False).rows)
    b = run_rule(paths, rule.with_params({"hold": 5}), "JP", start=first, record=False).rows
    assert not a["signal_date"].equals(b["signal_date"])                       # the window is two bars longer


def test_a_date_run_records_how_far_each_listing_got(paths):
    write(paths, 320)
    run = run_rule(paths, load_rule(ABOVE_MA), "CN")
    depth = dict(zip(run.rows["listing_id"], run.rows["depth"], strict=True))
    for lid, status in zip(run.rows["listing_id"], run.rows["status"], strict=True):
        assert depth[lid] == (0 if status == "signal" else -1)                       # one stage: passed, or not reached
    assert run_rows(paths, run.id)["depth"].to_list() == run.rows["depth"].to_list()


RANKED = ABOVE_MA.replace("id: above_ma", "id: top_half").replace('"C > MA(C, 20)"', '"RANK_XS(C) >= 0.5"')


def test_a_cross_sectional_value_on_a_sample_is_still_the_whole_markets(paths):
    # a rank among the sample alone would be a different rank: the formulas read the market, the run keeps the sample
    write(paths, 320)
    whole = run_rule(paths, load_rule(RANKED), "CN", record=False).rows
    status = dict(zip(whole["listing_id"], whole["status"], strict=True))
    assert sorted(status.values()) == ["fail", "signal"]                       # of two listings, the higher one
    for lid, want in status.items():
        alone = run_rule(paths, load_rule(RANKED), "CN", listings=[lid], record=False).rows
        assert alone["listing_id"].to_list() == [lid] and alone["status"][0] == want
    from wb.engine.l3.preview import preview
    top = next(lid for lid, s in status.items() if s == "signal")
    assert preview(paths, load_rule(RANKED), "CN", top).matcher.detail(top, whole["signal_date"].drop_nulls()[0])["status"] == "signal"


def test_a_cross_sectional_sample_takes_the_markets_turn(paths, monkeypatch):
    # a sample of such a rule reads the whole market: as heavy as a scan, so it waits for one that is running
    import wb.engine.l3.run as run
    write(paths, 320)
    monkeypatch.setattr(run, "WAIT", 0.2)
    held, done = threading.Event(), threading.Event()

    def scan():
        with run.RUN_LOCK:
            held.set()
            done.wait(10)
    t = threading.Thread(target=scan)
    t.start()
    held.wait(10)
    try:
        with pytest.raises(RuleError, match="全市场"):
            run_rule(paths, load_rule(RANKED), "CN", listings=["CN:600519"], record=False)
        assert run_rule(paths, load_rule(ABOVE_MA), "CN", listings=["CN:600519"], record=False).rows.height == 1  # reads itself
    finally:
        done.set()
        t.join()
    with run.RUN_LOCK:  # the job holding the turn (a calibration) runs its own samples
        assert run_rule(paths, load_rule(RANKED), "CN", listings=["CN:600519"], record=False).rows.height == 1

def test_a_rule_can_keep_to_boards_industries_tags_or_a_watch_group(paths):
    from wb.workspace.store import Workspace
    write(paths, 320)
    pool = lambda u: run_rule(paths, load_rule(ABOVE_MA.replace("signal_at:", f"universe: {u}\nsignal_at:")), "CN",  # noqa: E731
                              record=False).rows["listing_id"].to_list()
    assert sorted(pool("{boards: [主板]}")) == ["CN:000001", "CN:600519"] and pool("{boards: [创业板, 科创板]}") == []
    ws = Workspace(paths)
    ws.set_tags("CN:600519", ["白酒"])
    ws.add_item(ws.add_group("重点"), "CN:000001")
    assert pool("{tags: [白酒]}") == ["CN:600519"] and pool("{watchlist: 重点}") == ["CN:000001"]
    assert pool("{tags: [白酒], watchlist: 重点}") == []                      # every option narrows the pool
    assert pool("{industries: [银行]}") == ["CN:000001"]                      # the demo's 申万一级
    assert pool("{industries: [白酒]}") == []                                 # Tushare's own label: never matched
