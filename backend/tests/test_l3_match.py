"""The L3 matcher (spec §5.5, §5.6) on hand-made bars and structures, so every semantic is pinned:
stage geometry, conditions, completion time, known-at access, priority, dedupe, watch, diagnostics."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import load_rule

D0 = date(2025, 1, 1)
D = lambda i: D0 + timedelta(days=i)  # noqa: E731
LID = "CN:600000"


def frame(close, volume=None, high=None, lid=LID):
    n = len(close)
    high = high or [c * 1.01 for c in close]
    return pl.DataFrame({"listing_id": lid, "date": [D(i) for i in range(n)], "open": close, "high": high,
                         "low": [c * 0.99 for c in close], "close": close,
                         "volume": volume or [1000.0] * n, "amount": [1e9] * n})


def boxes(*rows, lid=LID):
    """(box_id, scale, start, known, end, ended, reason, top, bottom) with bar numbers."""
    cols = ["box_id", "scale", "start", "known_at", "end", "ended_at", "end_reason", "top", "bottom"]
    df = pl.DataFrame([dict(zip(cols, r, strict=True)) for r in rows], schema={
        "box_id": pl.Utf8, "scale": pl.Utf8, "start": pl.Int64, "known_at": pl.Int64, "end": pl.Int64,
        "ended_at": pl.Int64, "end_reason": pl.Utf8, "top": pl.Float64, "bottom": pl.Float64})
    return df.with_columns([pl.col(c).map_elements(D, return_dtype=pl.Date) for c in ("start", "known_at", "end", "ended_at")]
                           ).with_columns(pl.lit(lid).alias("listing_id"), pl.lit("established").alias("status"),
                                          ((pl.col("top") - pl.col("bottom")) / pl.col("bottom")).alias("width"))


def events(*rows):
    return pl.DataFrame([{"box_id": b, "date": D(i), "event": e, "value": None} for b, i, e in rows],
                        schema={"box_id": pl.Utf8, "date": pl.Date, "event": pl.Utf8, "value": pl.Float64})


def tables(b, e):
    return {"BOX": {"boxes": b, "touches": pl.DataFrame(), "events": e}}


BREAKOUT = """
id: t
name: 箱体放量突破后站稳
markets: [CN]
params:
  - {id: spike, value: 2.0}
stages:
  - id: rally
    kind: window
    before: {stage: box, bars: [1, 10]}
    during:
      - {id: runup, expr: "RUNUP(H, L) >= 0.15"}
  - id: box
    kind: structure
    use: BOX
    with: {scale: mid}
    during:
      - {id: shrink, expr: "MEAN(V, last=5) / MEAN(V, of=rally) < 0.7"}
  - id: breakout
    kind: event
    on: box.break_up
    at:
      - {id: volume, expr: "BAND_VOL_RATIO(box) >= $spike"}
  - id: hold
    kind: window
    after: {stage: breakout, bars: [1, 3]}
    all: "C > box.top"
signal_at: hold.end
watch_stage: box
ranking:
  - {expr: "box.width", dir: asc}
"""

# bars 0–9 rally 10 → 11.8 (+18%, volume 2000); 10–29 box 11–12 (volume 1000, 500 in the last five,
# a 1500 near the top on bar 20); 30 breakout to 12.6 on 3500; 31–33 hold above 12
CLOSE = [10 + 0.2 * i for i in range(10)] + [11.5, 11.9, 11.1, 11.6, 11.95, 11.2, 11.5, 11.8, 11.3, 11.6,
                                             11.9, 11.4, 11.5, 11.7, 11.2, 11.6, 11.5, 11.55, 11.5, 11.45] + [12.6, 12.7, 12.5, 12.8]
VOL = [2000.0] * 10 + [1000.0] * 10 + [1500.0] + [1000.0] * 4 + [500.0] * 5 + [3500.0, 2000.0, 1800.0, 1700.0]
HIGH = [c * 1.01 for c in CLOSE[:20]] + [12.0] + [c * 1.01 for c in CLOSE[21:]]
BOX = ("b1", "mid", 10, 22, 30, 35, "上破离箱", 12.0, 11.0)


def matcher(rule=BREAKOUT, close=CLOSE, volume=VOL, high=HIGH, box_rows=(BOX,), ev=(("b1", 30, "break_up"),)):
    return Matcher(load_rule(rule), "CN", frame(close, volume, high), tables(boxes(*box_rows), events(*ev)))


def test_the_signal_completes_when_the_last_stage_ends():
    m = matcher()
    assert m.at(D(32)).row(0, named=True)["status"] != "signal"     # the hold window is not over yet
    r = m.at(D(33)).row(0, named=True)
    assert r["status"] == "signal" and r["signal_date"] == D(33)
    detail = m.detail(LID, D(33))
    bind = {b["stage"]: b for b in detail["bindings"]}
    assert (bind["rally"]["start"], bind["rally"]["end"]) == (D(0), D(9))   # 1..10 bars before the box start
    assert (bind["box"]["start"], bind["box"]["end"]) == (D(10), D(29))     # cut the bar before its breakout
    assert bind["breakout"]["at"] == D(30) and (bind["hold"]["start"], bind["hold"]["end"]) == (D(31), D(33))
    cond = {c["id"]: c for c in detail["conditions"]}
    assert cond["runup"]["value"] == pytest.approx(11.8 * 1.01 / (10 * 0.99) - 1) and cond["runup"]["status"] == "pass"
    assert cond["shrink"]["value"] == pytest.approx(500 / 2000)
    assert cond["volume"]["value"] == pytest.approx(3500 / 1500) and cond["volume"]["threshold"] == 2.0
    assert cond["hold"]["status"] == "pass"


def test_a_failed_condition_is_named_with_its_shortfall():
    m = matcher(volume=VOL[:30] + [2500.0] + VOL[31:])       # breakout volume only 2500 / 1500 = 1.67 < 2
    r = m.at(D(33)).row(0, named=True)
    assert r["status"] == "fail"
    d = m.detail(LID, D(33))
    assert d["deepest_stage"] == "box"                         # rally and box passed, the breakout did not
    miss = d["closest_miss"]
    assert miss["id"] == "volume" and miss["value"] == pytest.approx(2500 / 1500) and miss["threshold"] == 2.0


def test_unknown_is_not_fail():
    # the rally window reaches before the first bar: its run-up cannot be measured
    m = matcher(box_rows=(("b1", "mid", 5, 22, 30, 35, "上破离箱", 12.0, 11.0),))
    assert m.at(D(33)).row(0, named=True)["status"] == "unknown"


def test_only_what_is_known_by_t_is_used():
    # the box is confirmed only on bar 40: at bar 33 it does not exist yet, so nothing can match
    m = matcher(box_rows=(("b1", "mid", 10, 40, 30, 45, "上破离箱", 12.0, 11.0),))
    assert m.at(D(33)).row(0, named=True)["status"] == "fail"
    # and the same answer comes from bars that stop at t (spec §5.8 rule 3)
    assert matcher().at(D(33)).equals(Matcher(load_rule(BREAKOUT), "CN", frame(CLOSE[:34], VOL[:34], HIGH[:34]),
                                              tables(boxes(BOX), events(("b1", 30, "break_up")))).at(D(33)))


def test_watch_while_the_box_is_active_with_distances():
    # on bar 28 the box is active, rally and shrink... shrink needs the last 5 bars: bars 24–28
    m = matcher()
    r = m.at(D(29)).row(0, named=True)
    assert r["status"] == "watch"
    d = m.detail(LID, D(29))
    dist = {x["id"]: x for x in d["distances"]}
    # the breakout needs a close above the top band (12.0 × 1.015 by default tol)
    assert dist["break_up"]["value"] == pytest.approx(11.45) and dist["break_up"]["threshold"] == pytest.approx(12.0 * 1.015)
    assert dist["volume"]["value"] == pytest.approx(500 / 1500) and dist["volume"]["threshold"] == 2.0


SIMPLE = """
id: r
name: 箱体突破
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
"""


def test_the_funnel_counts_a_structure_standing_on_the_day_whatever_the_watch_list_waits_on():
    # 选股 funnel (spec §7.1): a box standing on the day got the listing to 箱体, with a 潜伏 list or without; it was
    # counted only through the 潜伏 list's stage, so a rule without one (or waiting on a later stage) showed none there
    m = matcher(SIMPLE)
    assert m.depths(D(5)).row(0, named=True)["depth"] == -1    # no box known yet
    assert m.depths(D(25)).row(0, named=True)["depth"] == 0    # the box stands, no breakout yet
    assert m.depths(D(30)).row(0, named=True)["depth"] == 1    # its breakout


def test_range_mode_gives_one_signal_per_structure():
    # a second break of the same box (after a return) must not signal again
    ev = (("b1", 30, "break_up"), ("b1", 32, "return"), ("b1", 33, "break_up"))
    s = matcher(SIMPLE, ev=ev).signals(D(0), D(33))
    assert s["signal_date"].to_list() == [D(30)] and s["box"].to_list() == ["b1"]


def test_nearest_structure_wins():
    # two boxes break on the same bar and both pass: the more recent one is bound
    near = ("b2", "mid", 15, 25, 30, 35, "上破离箱", 12.0, 11.2)
    m = matcher(SIMPLE, box_rows=(BOX, near), ev=(("b1", 30, "break_up"), ("b2", 30, "break_up")))
    assert m.at(D(30)).row(0, named=True)["box"] == "b2"


def test_a_screen_is_a_condition_on_the_day():
    rule = """
id: s
name: 站上 5 日线
markets: [CN]
stages:
  - {id: today, kind: event, when: "C > MA(C, 5)"}
signal_at: today.at
"""
    close = [10.0, 10.0, 10.0, 10.0, 10.0, 11.0, 9.0, 12.0]
    m = Matcher(load_rule(rule), "CN", frame(close), {})
    assert [m.at(D(i)).row(0, named=True)["status"] for i in (4, 5, 6, 7)] == ["fail", "signal", "fail", "signal"]
    assert m.signals(D(0), D(7))["signal_date"].to_list() == [D(5), D(7)]
    assert m.at(D(2)).row(0, named=True)["status"] == "unknown"  # MA(C, 5) not known yet


def test_universe_leaves_out_young_and_illiquid_listings():
    rule = """
id: u
name: x
markets: [CN]
universe: {exclude: [listed_lt_5d], min_avg_amount_20d: {CN: 2000000000}}
stages:
  - {id: today, kind: event, when: "C > 0"}
signal_at: today.at
"""
    m = Matcher(load_rule(rule), "CN", frame([10.0] * 30), {})
    assert m.at(D(29)).is_empty()  # amount 1e9 < 2e9: outside the pool, not a fail
    rule2 = rule.replace("2000000000", "500000000")
    m2 = Matcher(load_rule(rule2), "CN", frame([10.0] * 30), {})
    assert m2.at(D(3)).is_empty() and m2.at(D(29)).row(0, named=True)["status"] == "signal"


PREFIX = """
id: p
name: 前缀一致
markets: [CN]
stages:
  - id: box
    kind: structure
    use: BOX
    with: {scale: short}
    at:
      - {id: touches, expr: "box.touches_top >= 2"}
    during:
      - {id: quiet, expr: "MEAN(V, last=3) <= MEAN(V) * 2"}
  - id: breakout
    kind: event
    on: box.break_up
  - id: hold
    kind: window
    after: {stage: breakout, bars: [1, 2]}
    all: "C > box.bottom"
signal_at: hold.end
watch_stage: box
"""


@pytest.mark.parametrize("seed", range(6))
def test_matching_on_t_reads_nothing_after_t(seed):
    # real structures from the full history and from the history cut at t must give the same answer
    # on t (spec §5.8 rule 3): running counts such as touches_top and a box's end are as of t
    from tests.test_l2_contract import with_dif
    from tests.test_l2_prefix import series
    from wb.engine.l2.spec import compute

    rule = load_rule(PREFIX)
    df = with_dif(series(seed, 300)).with_columns(pl.lit(LID).alias("listing_id"), pl.lit(1e9).alias("amount"))
    a = Matcher(rule, "CN", df, compute(df))
    signal_days = a.signals(df["date"][0], df["date"][-1])["signal_date"].to_list()
    days = [df["date"][t] for t in (120, 180, 240, 299)] + signal_days
    for day in days:
        cut = df.filter(pl.col("date") <= day)
        b = Matcher(rule, "CN", cut, compute(cut))
        assert a.at(day).equals(b.at(day)), (seed, day)
        assert a.detail(LID, day) == b.detail(LID, day), (seed, day)
    # range mode and the day-by-day mode agree on when signals happen
    assert all(a.at(day)["status"][0] == "signal" for day in signal_days)


@pytest.mark.parametrize("seed", range(6))
def test_the_watch_list_over_a_range_equals_at_day_on_real_structures(seed, monkeypatch):
    # real boxes (versions, touches recounted as of each day, ends) from the L2 engine, two stocks unrolled one at a
    # time (PIECE), ranked by an aggregation over the box: the range mode's 潜伏 rows are at(day)'s, day after day
    from tests.test_l2_contract import with_dif
    from tests.test_l2_prefix import series
    from wb.engine.l2.spec import compute
    from wb.engine.l3 import match

    monkeypatch.setattr(match, "PIECE", 1)
    one = lambda k, lid: with_dif(series(k, 300)).with_columns(pl.lit(lid).alias("listing_id"), pl.lit(1e9).alias("amount"))  # noqa: E731
    a, b = one(seed, LID), one(seed + 10, "CN:600001")
    ta, tb = compute(a), compute(b)
    t = {"BOX": {k: pl.concat([ta["BOX"][k], tb["BOX"][k]], how="diagonal_relaxed") for k in ("boxes", "touches", "events")}}
    m = Matcher(load_rule(PREFIX + 'ranking:\n  - {expr: "MEAN(V, of=box)", dir: desc}\n'), "CN", pl.concat([a, b]), t)
    w = m.watches(a["date"][0], a["date"][-1])
    assert w["listing_id"].n_unique() == 2
    for day in a["date"][60::3]:
        want = m.at(day).filter(pl.col("status") == "watch").select("listing_id", "box", "rank0").sort("listing_id")
        got = w.filter(pl.col("date") == day).select("listing_id", "box", "rank0").sort("listing_id")
        assert got.select("listing_id", "box").equals(want.select("listing_id", "box")), (seed, day)
        assert got["rank0"].to_list() == pytest.approx(want["rank0"].to_list()), (seed, day)

def test_a_range_with_nothing_in_it_is_empty():
    # bindings exist, but none finishes inside the range: no rows, not an error
    s = matcher().signals(D(0), D(5))
    assert s.is_empty() and s.columns[:2] == ["listing_id", "signal_date"]


def test_results_are_sorted_by_status_then_rank():
    rule = SIMPLE + """watch_stage: box
ranking:
  - {expr: "box.width", dir: asc}
  - {expr: "C@breakout", dir: desc}
"""
    two = pl.concat([frame(CLOSE, VOL, HIGH), frame(CLOSE, VOL, HIGH, lid="CN:600001"), frame(CLOSE, VOL, HIGH, lid="CN:600002")])
    b = pl.concat([boxes(BOX), boxes(("w1", "mid", 10, 22, 30, 35, "上破离箱", 12.0, 11.5), lid="CN:600001"),
                   boxes(("a1", "mid", 10, 22, None, None, None, 12.0, 10.0), lid="CN:600002")])
    e = events(("b1", 30, "break_up"), ("w1", 30, "break_up"))
    r = Matcher(load_rule(rule), "CN", two, tables(b, e)).at(D(30))
    # both breakouts signal, the narrower box first; the active box is a watch, ranked by width only
    assert r["listing_id"].to_list() == ["CN:600001", "CN:600000", "CN:600002"]
    assert r["status"].to_list() == ["signal", "signal", "watch"]
    assert r["rank1"].to_list()[2] is None and r["rank0"].to_list()[2] == pytest.approx(0.2)


def test_a_rule_names_the_columns_it_reads_and_its_markets():
    rule = load_rule(BREAKOUT)
    assert {"close", "high", "low", "volume"} <= Matcher.columns(rule, "CN")
    from wb.engine.l3.rule import RuleError

    with pytest.raises(RuleError, match="不适用于 JP"):
        Matcher.columns(rule, "JP")


def test_a_gold_stage_binds_the_small_box_bars():
    rule = """
id: g
name: 天品
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 天品}}
signal_at: g.at
"""
    small = ("s1", "short", 32, 36, None, None, None, 12.6, 12.2)
    gold = pl.DataFrame({"listing_id": LID, "kind": ["天品", "上品"], "variant": False, "big_box_id": "b1",
                         "small_box_id": ["s1", "x"], "known_at": [D(36), D(25)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 12.6, "small_bottom": 12.2, "inner_distance": None, "above_top": 12.2 / 12 - 1})
    t = {"BOX": {"boxes": boxes(BOX, small), "touches": pl.DataFrame(), "events": events()},
         "BOX_DERIVED": {"gold": gold}}
    close = CLOSE + [12.4, 12.5, 12.4, 12.45]
    m = Matcher(load_rule(rule), "CN", frame(close), t)
    assert m.at(D(36)).row(0, named=True)["status"] == "signal"
    bind = m.detail(LID, D(36))["bindings"][0]
    assert (bind["start"], bind["at"]) == (D(32), D(36))
    # its id is its big and small box — what 今日's chart draws (an id of dates matched no structure)
    assert bind["id"] == "b1>s1"
    assert m.signals(D(0), D(36))["g"].to_list() == ["b1>s1"]


def test_a_formula_that_does_not_compile_names_its_place():
    from wb.engine.l3.rule import RuleError

    bad = BREAKOUT.replace('"BAND_VOL_RATIO(box) >= $spike"', '"C > 5"').replace("params:\n  - {id: spike, value: 2.0}\n", "")
    with pytest.raises(RuleError, match=r"breakout\.volume（C > 5）：.*RAW_C"):
        Matcher.columns(load_rule(bad), "CN")


def test_without_st_data_the_pool_says_so():
    rule = load_rule("""
id: s
name: x
markets: [CN]
universe: {exclude: [st]}
stages:
  - {id: today, kind: event, when: "C > 0"}
signal_at: today.at
""")
    m = Matcher(rule, "CN", frame([10.0] * 5), {}, available={"open", "high", "low", "close", "volume", "amount"})
    assert m.warnings == ["当前数据档没有 ST 信息，股票池没有排除 ST"]
    assert m.at(D(4)).row(0, named=True)["status"] == "signal"


def test_an_event_found_on_confirmation_is_known_then_not_on_its_own_day():
    # a spike inside the box's establishing window happened on bar 15 but is known only on bar 22,
    # when the box is confirmed: a rule on it fires on 22 and reads bar 15's values
    rule = """
id: sp
name: 箱体成立前有过冲高针
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - id: spike
    kind: event
    on: box.spike_up
    at:
      - {id: traded, expr: "V > 0"}
signal_at: spike.at
"""
    ev = events(("b1", 15, "spike_up")).with_columns(pl.lit(D(22)).alias("known_at"))
    m = Matcher(load_rule(rule), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(BOX), ev))
    assert m.at(D(15)).row(0, named=True)["status"] == "fail"
    assert m.at(D(22)).row(0, named=True)["status"] == "signal"
    bind = {b["stage"]: b for b in m.detail(LID, D(22))["bindings"]}
    assert bind["spike"]["at"] == D(15)


def test_a_stage_that_needs_an_active_box_to_end_is_not_complete():
    rule = """
id: a
name: 箱体结束后三天站上沿
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - id: after
    kind: window
    after: {stage: box, bars: [1, 3]}
    all: "C > box.top"
signal_at: after.end
"""
    active = ("b1", "mid", 10, 22, None, None, None, 12.0, 11.0)
    m = Matcher(load_rule(rule), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(active), events()))
    assert [m.at(D(d)).row(0, named=True)["status"] for d in (22, 25, 33)] == ["fail"] * 3


def test_the_watch_list_uses_only_stages_known_by_t():
    rule = """
id: w
name: 前一个箱体冲高针后的新箱体
markets: [CN]
stages:
  - {id: a, kind: structure, use: BOX}
  - {id: sp, kind: event, on: a.spike_up}
  - {id: box, kind: structure, use: BOX}
  - {id: br, kind: event, on: box.break_up}
signal_at: br.at
watch_stage: box
"""
    first, active = ("b1", "mid", 0, 8, 9, 9, "上破离箱", 12.0, 11.0), ("b2", "mid", 12, 20, None, None, None, 12.0, 11.0)
    m = Matcher(load_rule(rule), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(first, active), events(("b1", 30, "spike_up"))))
    assert m.at(D(25)).row(0, named=True)["status"] == "fail"      # the spike on bar 30 is not there yet
    assert m.at(D(31)).row(0, named=True)["status"] == "watch"


def test_a_signal_at_a_structures_end_comes_when_it_is_known_to_have_ended():
    rule = """
id: e
name: 箱体结束
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
signal_at: box.end
"""
    m = matcher(rule, box_rows=(("b1", "mid", 10, 22, 30, 32, "上破离箱", 12.0, 11.0),))
    assert [m.at(D(d)).row(0, named=True)["status"] for d in (30, 31, 32)] == ["fail", "fail", "signal"]
    assert m.signals(D(0), D(33))["signal_date"].to_list() == [D(32)]


PRESSURE_RULE = """
id: p
name: 突破时上方有空间
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - id: breakout
    kind: event
    on: box.break_up
    at:
      - {id: space, expr: "pressure.space >= 0.10"}
signal_at: breakout.at
"""


def test_pressure_counts_mid_and_long_boxes_unless_the_rule_says_otherwise():
    small = ("b0", "short", 0, 5, 8, 9, "下破离箱", 13.2, 12.9)    # a few-day box just above (a small box is fine)
    old = ("b9", "mid", 0, 5, 8, 9, "下破离箱", 14.5, 13.9)
    m = matcher(PRESSURE_RULE, box_rows=(small, old, BOX))
    assert m.at(D(30)).row(0, named=True)["status"] == "signal"
    m = matcher(PRESSURE_RULE + "pressure: {scales: [short, mid, long]}\n", box_rows=(small, old, BOX))
    cond = {c["id"]: c for c in m.detail(LID, D(30))["conditions"]}
    assert cond["space"]["value"] == pytest.approx(12.9 / 12.6 - 1) and cond["space"]["status"] == "fail"


def test_pressure_space_is_read_on_the_stage_bar():
    rule = """
id: p
name: 突破时上方有空间
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - id: breakout
    kind: event
    on: box.break_up
    at:
      - {id: space, expr: "pressure.space >= 0.10"}
signal_at: breakout.at
"""
    old = ("b0", "mid", 0, 5, 8, 9, "下破离箱", 14.5, 13.9)                 # an ended box above the breakout
    m = matcher(rule, box_rows=(old, BOX))
    assert m.at(D(30)).row(0, named=True)["status"] == "signal"
    cond = {c["id"]: c for c in m.detail(LID, D(30))["conditions"]}
    assert cond["space"]["value"] == pytest.approx(13.9 / 12.6 - 1)



def test_pressure_read_for_a_box_is_above_its_top():
    # 上方空间 of a box (design 2026-10-11 §6: 按线价算, pressure.bottom / 箱顶 − 1) — before the break the close is inside
    # the box, and an older box just above it but under the box's top is no pressure on a breakout: read for a box stage
    # (its 潜伏 condition, a 潜伏 row's plan lines), the pressure is the nearest box above the box's top, the line a cross
    # buys at; for an event, above its close
    rule = """
id: room
name: 潜伏看上方空间
markets: [CN]
stages:
  - id: box
    kind: structure
    use: BOX
    with: {scale: mid}
    at: [{id: room, expr: "pressure.space >= 0.05"}]
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
ranking:
  - {expr: "pressure.bottom"}
"""
    inner = ("i0", "mid", 0, 5, 8, 9, "下破离箱", 11.9, 11.6)     # over the closes of the box, under its top (12.0)
    above = ("o9", "mid", 0, 5, 8, 9, "下破离箱", 14.5, 13.9)
    m = matcher(rule, box_rows=(inner, above, BOX))
    day = m.at(D(25)).row(0, named=True)
    assert day["status"] == "watch" and day["rank0"] == pytest.approx(13.9)
    space = {c["id"]: c for c in m.detail(LID, D(25))["conditions"]}["room"]
    assert space["value"] == pytest.approx(13.9 / 12.0 - 1)
    assert m.at(D(30)).row(0, named=True)["rank0"] == pytest.approx(13.9)   # the breakout: above its close (12.6)


def test_a_formula_function_of_a_structure_or_pressure_attribute_is_refused_by_the_check():
    # MIN(pressure.space, 1) passed the rule check and failed the run with a Python repr (2026-10-11): a formula
    # function reads a column bar by bar; a box's or the pressure's attribute is one number at the stage's bar
    from wb.engine.l3.match import compile_errors

    for expr in ("MIN(pressure.space, 1) >= 0.1", "ABS(box.top) > 1", "IF(pressure.space > 1, 1, pressure.space) > 0.1"):
        rule = load_rule(PRESSURE_RULE.replace("pressure.space >= 0.10", expr))
        errs = compile_errors(rule)
        assert len(errs) == 1 and "逐根" in errs[0] and "Call(" not in errs[0], (expr, errs)
    assert compile_errors(load_rule(PRESSURE_RULE.replace(">= 0.10", "* 2 >= MEAN(C / REF(C, 1) - 1, of=box)"))) == []


def test_a_box_s_false_bottom_is_a_price_known_on_its_break():
    # 交易计划的失效线 (design 2026-10-09 §3: box.false_bottom): the 假底 of the box, known on the bar it broke up, read
    # like any of the box's values; before it is known there is none
    from wb.engine.l2.derived import FB_SCHEMA
    rule = """
id: fb
name: 突破读假底
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
ranking:
  - {expr: "box.false_bottom"}
"""
    fb = pl.DataFrame([{"listing_id": LID, "box_id": "b1", "source": "C 点", "gold_box_id": None, "date": D(24), "price": 11.2,
                        "previous_low": 11.0, "known_at": D(30), "end": None, "ended_at": None, "end_reason": None}], schema=FB_SCHEMA)
    t = tables(boxes(BOX), events(("b1", 30, "break_up")))
    t["BOX_DERIVED"] = {"false_bottoms": fb}
    m = Matcher(load_rule(rule), "CN", frame(CLOSE, VOL, HIGH), t)
    sig = m.signals(D(0), D(40), ranks=True)
    assert sig["signal_date"].to_list() == [D(30)] and sig["rank0"].to_list() == [pytest.approx(11.2)]
    watch = m.at(D(29))
    assert watch.row(0, named=True)["status"] == "watch" and watch["rank0"].to_list() == [None]   # not known before the break



def test_a_watch_row_s_false_bottom_as_if_its_box_broke_the_next_day():
    # design 2026-10-11 §6: a cross buys before the break: its 失效线 is the 假底 the box would have if it broke that
    # day, read the evening before from that evening's data — asked for (the range mode for cross entries); at(day)
    # and the plain range mode keep none before the break
    rule = """
id: fb
name: 潜伏读假底
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
ranking:
  - {expr: "box.false_bottom"}
"""
    low = dict(zip(range(21, 30), [11.5, 11.45, 11.6, 11.5, 11.4, 11.35, 11.5, 11.45, 11.5], strict=True))
    high = [12.2 if i in (23, 26) else h for i, h in enumerate(HIGH)]   # the line (12.18) reached on 23, 26 and the break (30)
    f = frame(CLOSE, VOL, high).with_columns(pl.Series("low", [low.get(i, c * 0.99) for i, c in enumerate(CLOSE)]))
    t = tables(boxes(BOX), events(("b1", 30, "break_up")))
    t["BOX"]["touches"] = pl.DataFrame([{"box_id": "b1", "date": D(d), "side": s, "price": p, "known_at": D(22)}
                                        for d, s, p in ((12, "bottom", 10.99), (20, "top", 12.0))])
    from wb.engine.l2.derived import FB_SCHEMA
    t["BOX_DERIVED"] = {"false_bottoms": pl.DataFrame(schema=FB_SCHEMA)}   # the break's 假底 is a later day's
    m = Matcher(load_rule(rule), "CN", f, t)
    plain, asif = m.watches(D(0), D(40)), m.watches(D(0), D(40), as_if_break=True)
    assert plain["rank0"].null_count() == plain.height
    got = dict(asif.select("date", "rank0").iter_rows())
    assert got[D(22)] == pytest.approx(11.45) and got[D(25)] == pytest.approx(11.4)   # the lowest low since the last top touch,
    assert got[D(29)] == pytest.approx(11.35)                                          # up to that evening
    assert got[D(27)] is None   # a day whose next day does not reach the line buys nothing: its 假底 is not worked out

SWITCHED = """
id: sw
name: 开关关掉的条件
markets: [CN]
params:
  - {id: sw, value: 0}
  - {id: dry, value: 0.25}
stages:
  - id: box
    kind: structure
    use: BOX
    with: {scale: mid}
    during:
      - {id: xingmo, expr: "$sw == 0 OR MIN(MA(V, 5), last=10) / MAX(MA(V, 5)) <= $dry"}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
"""


def test_a_condition_its_switch_turns_off_is_not_evaluated(monkeypatch):
    # 形末量尽 / 阳量群 are off by default ($xingmo == 0 OR …): a whole range's aggregation over every bar of every box was
    # computed and thrown away; folded, it passes without reading a bar — on, it is evaluated as before
    calls = []
    real = Matcher._bars

    def counted(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(Matcher, "_bars", counted)
    m = Matcher(load_rule(SWITCHED), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(BOX), events(("b1", 30, "break_up"))))
    assert m.signals(D(0), D(40))["signal_date"].to_list() == [D(30)] and calls == []
    assert {c["id"]: c["status"] for c in m.detail(LID, D(30))["conditions"]}["xingmo"] == "pass"
    on = load_rule(SWITCHED.replace("{id: sw, value: 0}", "{id: sw, value: 1}"))
    m.variant(on).signals(D(0), D(40))   # a check-up's variant turning it on shares the evaluated frame: judged on
    assert calls                        # with its own switch, not the one the frame was compiled with


WATCHED = """
id: wt
name: 箱体潜伏
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
ranking:
  - {expr: "box.width", dir: asc}
"""


def test_the_watch_list_over_a_range_is_at_day_s_watch_list_day_by_day():
    # design 2026-10-11 §9: ten years of 潜伏 lists cannot be at(day) 2,400 times (1.1 s a day for 800 stocks); the range
    # mode unrolls each standing binding over its days — and must say what at(day) says, day by day
    b2 = ("b2", "mid", 12, 24, 31, 36, "上破离箱", 12.1, 11.1)            # a second box standing over the same days
    m = Matcher(load_rule(WATCHED), "CN", frame(CLOSE, VOL, HIGH),
                tables(boxes(BOX, b2), events(("b1", 30, "break_up"), ("b2", 31, "break_up"))))
    w = m.watches(D(0), D(40))
    assert w.height > 0 and set(w.columns) >= {"listing_id", "date", "box", "edge", "line", "per_bar", "rank0"}
    for i in range(41):
        want = m.at(D(i)).filter(pl.col("status") == "watch").select("listing_id", "box", "rank0").sort("listing_id")
        got = w.filter(pl.col("date") == D(i)).select("listing_id", "box", "rank0").sort("listing_id")
        assert got.equals(want), (D(i), got, want)
    assert w.filter(pl.col("date") == D(30)).is_empty()                     # the day the rule signals is not a 潜伏 day
    kept = m.watches(D(0), D(40), all_days=True)                              # unless asked to keep them (one replay for
    assert kept.filter(pl.col("date") == D(30))["box"].to_list() == ["b2"]   # check-up variants: each removes its own)
    assert kept.join(w, on=["listing_id", "date"], how="anti")["date"].to_list() == [D(30), D(31)]
    top = {"b1": 12.0, "b2": 12.1}
    assert all(r["edge"] == top[r["box"]] and r["line"] == pytest.approx(top[r["box"]] * 1.015) and r["per_bar"] == 0
               and r["line_of"] == r["box"] for r in w.iter_rows(named=True))   # the break line 盯盘 watches (live.box_top)
    assert w.filter(pl.col("date") < D(5)).is_empty()                         # before any box is known
    for r in w.iter_rows(named=True):                                         # the line the explanation gives 盯盘
        x = next(d for d in m.detail(LID, r["date"])["distances"] if d["id"] == "break_up")
        assert (r["edge"], r["line"]) == pytest.approx((x["edge"], x["threshold"]))


def test_the_top_a_cross_buys_at_is_the_top_live_alerts_at_the_next_session():
    # design 2026-10-11 §6: 「线是潜伏等着突破的那个箱的箱顶（和盯盘盯的同一条线）」 — 盯盘 reads the explanation's break line for
    # the next session (live.box_top) and alerts at it ÷ (1 + tol), the top itself (final review C1: the cross had
    # bought at the break line, 1.5% over); a slanted small box's top a bar further along
    import json

    from wb.engine.l3.match import box_tol
    from wb.jobs.live import box_top

    m = Matcher(load_rule(WATCHED), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(BOX), events(("b1", 30, "break_up"))))
    gold_rule = """
id: g
name: 变异下品
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, variant: true}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
watch_stage: g
"""
    slant = boxes(("sl1", "slant", 23, 26, 28, 31, "上破离箱", 11.7, 11.4)).with_columns(
        pl.lit(D(23)).alias("top_a"), pl.lit(11.6).alias("top_a_price"), pl.lit(D(25)).alias("top_b"), pl.lit(11.7).alias("top_b_price"))
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": True, "big_box_id": "b1",
                         "small_box_id": ["sl1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
    t = {"BOX": {"boxes": boxes(BOX), "touches": pl.DataFrame(), "events": events(("b1", 30, "break_up"))},
         "SLANT_BOX": {"slant": slant, "events": events(("sl1", 28, "break_up"))}, "BOX_DERIVED": {"gold": gold}}
    g = Matcher(load_rule(gold_rule), "CN", frame(CLOSE), t)
    from wb.engine.l3.rule import Plan
    from wb.validate.cross import cross_signals

    cal = [D(i) for i in range(len(CLOSE) + 1)]
    for mm in (m, g):
        w = mm.watches(D(0), D(40))
        assert w.height > 0
        for r in w.iter_rows(named=True):
            alert = box_top(json.dumps(mm.detail(LID, r["date"]), default=str)) / (1 + box_tol(mm.rule))
            assert r["edge"] + r["rise"] == pytest.approx(alert), (mm.rule.id, r["date"])
            # the next day's high at that top is a cross bought there; a high a hair under it is none
            one = w.filter(pl.col("date") == r["date"])
            for high, n in ((alert, 1), (alert * 0.999, 0)):
                b = pl.DataFrame({"listing_id": LID, "date": [r["date"], r["date"] + timedelta(days=1)], "open": [alert * 0.98] * 2,
                                  "high": [alert * 0.985, high], "close": [alert * 0.98] * 2, "adj": 1.0})
                x = cross_signals(one, b, Plan(entry="cross"), cal)
                assert x.height == n and (n == 0 or x["entry_price"][0] == pytest.approx(alert)), (mm.rule.id, r["date"], high)

def test_a_formula_event_judged_on_the_day_in_the_watch_stages_is_judged_on_each_replayed_day():
    # a formula event before the watch stage that nothing hangs on is judged on the day itself (at(day)); the range mode
    # places it on each unrolled day — and must not ask when it was known before placing it (final review I3)
    rule = WATCHED.replace("stages:\n", 'stages:\n  - {id: up, kind: event, when: "C > REF(C, 1)"}\n')
    m = Matcher(load_rule(rule), "CN", frame(CLOSE, VOL, HIGH), tables(boxes(BOX), events(("b1", 30, "break_up"))))
    w = m.watches(D(0), D(40))
    assert w.height > 0
    for i in range(41):
        want = m.at(D(i)).filter(pl.col("status") == "watch").select("listing_id", "box").sort("listing_id")
        assert w.filter(pl.col("date") == D(i)).select("listing_id", "box").sort("listing_id").equals(want), D(i)

def test_a_slanted_small_box_s_line_on_the_watch_list_rises_a_bar_a_day_as_its_explanation_says():
    # 变异金品's small box is slanted: the line 盯盘 watches is its top line extended to the day (live.box_top reads the
    # explanation's threshold + per_bar for the next session); the range mode reads the same line on each day
    rule = """
id: g
name: 变异下品
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, variant: true}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
watch_stage: g
"""
    slant = boxes(("sl1", "slant", 23, 26, 28, 31, "上破离箱", 11.7, 11.4)).with_columns(
        pl.lit(D(23)).alias("top_a"), pl.lit(11.6).alias("top_a_price"), pl.lit(D(25)).alias("top_b"), pl.lit(11.7).alias("top_b_price"))
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": True, "big_box_id": "b1",
                         "small_box_id": ["sl1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
    t = {"BOX": {"boxes": boxes(BOX), "touches": pl.DataFrame(), "events": events(("b1", 30, "break_up"))},
         "SLANT_BOX": {"slant": slant, "events": events(("sl1", 28, "break_up"))}, "BOX_DERIVED": {"gold": gold}}
    m = Matcher(load_rule(rule), "CN", frame(CLOSE), t)
    w = m.watches(D(0), D(len(CLOSE) - 1))
    assert w["date"].to_list() == [D(26), D(27), D(29), D(30)]           # known on 26, ended on 31, the break on 28 a signal
    assert set(w["line_of"]) == {"sl1"}                                    # the line is the small box's
    for r in w.iter_rows(named=True):
        x = next(d for d in m.detail(LID, r["date"])["distances"] if d["id"] == "break_up")
        assert (r["edge"], r["line"], r["per_bar"]) == pytest.approx((x["edge"], x["threshold"], x["per_bar"]))
    rows = w.rows(named=True)
    assert rows[0]["edge"] == pytest.approx(11.6 + 0.05 * 3) and rows[0]["per_bar"] == pytest.approx(0.05 * 1.015)
    assert rows[1]["line"] - rows[0]["line"] == pytest.approx(rows[0]["per_bar"])   # a day further along the line

def unknown_matcher():
    # the rally window starts 5 bars before the first bar; bar 31 has a data problem
    q = pl.Series("quality", [["vwap_range"] if i == 31 else None for i in range(len(CLOSE))], dtype=pl.List(pl.Utf8))
    return Matcher(load_rule(BREAKOUT), "CN", frame(CLOSE, VOL, HIGH).with_columns(q),
                   tables(boxes(("b1", "mid", 5, 22, 30, 35, "上破离箱", 12.0, 11.0)), events(("b1", 30, "break_up"))))


def test_an_unknown_says_why():
    d = unknown_matcher().detail(LID, D(33))
    assert d["status"] == "unknown"
    assert {"kind": "history", "bars": 5} in d["unknown_reasons"]
    assert {"kind": "quality", "date": D(31), "issues": ["vwap_range"]} in d["unknown_reasons"]
    assert {"kind": "young", "bars": 34} in d["unknown_reasons"]          # formula windows may not fit yet
    assert matcher().detail(LID, D(33))["unknown_reasons"] == []


def test_an_empty_pool_gives_empty_results_with_every_column():
    m = Matcher(load_rule(BREAKOUT), "CN", frame(CLOSE, VOL, HIGH).clear(), tables(boxes(BOX), events(("b1", 30, "break_up"))))
    assert m.at(D(33)).is_empty() and m.at(D(33)).columns == matcher().at(D(33)).columns
    assert m.signals(D(0), D(33)).is_empty() and m.signals(D(0), D(33)).columns == matcher().signals(D(0), D(33)).columns


def test_rel_perf_over_a_stage_compares_the_same_bars_with_the_index():
    rule = """
id: r
name: 箱体期间强于大盘
markets: [CN]
stages:
  - id: box
    kind: structure
    use: BOX
    during:
      - {id: strong, expr: "REL_PERF(of=box) > 0"}
signal_at: box.at
"""
    idx = [1000.0 + 10 * i for i in range(len(CLOSE))]           # the index rises steadily
    f = frame(CLOSE, VOL, HIGH).with_columns(pl.Series("idx_close", idx))
    m = Matcher(load_rule(rule), "CN", f, tables(boxes(BOX), events()))
    cond = {c["id"]: c for c in m.detail(LID, D(22))["conditions"]}
    # on its confirmation bar the box is seen up to that bar: bars 10..22
    want = (CLOSE[22] / CLOSE[10]) / (idx[22] / idx[10]) - 1
    assert cond["strong"]["value"] == pytest.approx(want) and cond["strong"]["status"] == "fail"
    assert "idx_close" in Matcher.columns(load_rule(rule), "CN")


WHEN_THEN = """
id: w
name: 站上 5 日线后三天不破
markets: [CN]
stages:
  - {id: up, kind: event, when: "C > MA(C, 5) AND REF(C, 1) <= REF(MA(C, 5), 1)"}
  - id: hold
    kind: window
    after: {stage: up, bars: [1, 3]}
    all: "C > REF(C, 1)"
signal_at: hold.end
"""


def test_a_formula_event_followed_by_another_stage_signals_on_the_day_in_date_mode():
    close = [10.0, 10.0, 10.0, 10.0, 10.0, 11.0, 11.1, 11.2, 11.3, 11.0, 10.0, 10.0, 10.0, 10.0, 12.0, 12.1, 12.2, 12.3]
    m = Matcher(load_rule(WHEN_THEN), "CN", frame(close), {})
    days = m.signals(D(0), D(17))["signal_date"].to_list()
    assert days == [D(8), D(17)]
    assert [m.at(D(i)).row(0, named=True)["status"] == "signal" for i in range(18)] == [D(i) in days for i in range(18)]


def test_a_box_after_a_formula_leg_is_watched_then_signals_by_day():
    rule = """
id: g
name: 长颈鹿
markets: [CN]
stages:
  - {id: leg, kind: event, when: "C / REF(C, 1) - 1 >= 0.09"}
  - {id: box, kind: structure, use: BOX, after: {stage: leg, gap: [1, 60]}}
  - {id: br, kind: event, on: box.break_up}
signal_at: br.at
watch_stage: box
"""
    close = [10.0, 10.0, 11.0] + CLOSE[3:]                          # a 10% leg on bar 2
    m = Matcher(load_rule(rule), "CN", frame(close, VOL, HIGH), tables(boxes(BOX), events(("b1", 30, "break_up"))))
    assert m.at(D(25)).row(0, named=True)["status"] == "watch"
    assert m.at(D(30)).row(0, named=True)["status"] == "signal"
    assert m.signals(D(0), D(33))["signal_date"].to_list() == [D(30)]


def test_the_explanation_of_a_watch_does_not_need_a_signal():
    rule = """
id: r
name: 放量突破
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - {id: breakout, kind: event, on: box.break_up, at: [{id: volume, expr: "BAND_VOL_RATIO(box) >= 2"}]}
signal_at: breakout.at
watch_stage: box
"""
    m = matcher(rule, volume=VOL[:30] + [2500.0] + VOL[31:])        # the breakout bar fails (2500 / 1500 < 2)
    assert m.at(D(30)).row(0, named=True)["status"] == "watch"
    assert m.detail(LID, D(30))["status"] == "watch"


def test_two_structures_pair_only_through_their_gap():
    rule = """
id: two
name: 中箱结束后不久的小箱
markets: [CN]
stages:
  - {id: a, kind: structure, use: BOX, with: {scale: mid}}
  - {id: b, kind: structure, use: BOX, with: {scale: short}, after: {stage: a, gap: [1, 5]}}
signal_at: b.at
"""
    rows = (("m1", "mid", 0, 6, 8, 9, "下破离箱", 12.0, 11.0),          # its last bar inside is 7
            ("m2", "mid", 12, 18, 20, 22, "上破离箱", 13.0, 12.0),      # its last bar inside is 19
            ("s1", "short", 10, 14, 16, 17, "上破离箱", 11.5, 11.2),    # starts 3 bars after m1: a pair
            ("s2", "short", 26, 30, None, None, None, 12.5, 12.2))      # starts 7 bars after m2: too late
    m = matcher(rule, box_rows=rows, ev=())
    assert m.signals(D(0), D(33)).select("signal_date", "a", "b").rows() == [(D(14), "m1", "s1")]
    assert m.at(D(14)).select("status", "a", "b").row(0) == ("signal", "m1", "s1")


def test_a_watch_on_a_rule_that_signals_when_its_box_ends():
    rule = """
id: e
name: 箱体结束
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
signal_at: box.end
watch_stage: box
"""
    m = matcher(rule, box_rows=(("b1", "mid", 10, 22, None, None, None, 12.0, 11.0),))
    assert m.at(D(25)).row(0, named=True)["status"] == "watch"


def test_a_watch_measures_the_next_formula_stage_too():
    rule = """
id: n
name: 箱内等放量
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX}
  - id: go
    kind: event
    when: "C > 0"
    at:
      - {id: vol, expr: "V / MA(V, 5) >= 2"}
signal_at: go.at
watch_stage: box
"""
    m = matcher(rule, box_rows=(("b1", "mid", 10, 22, None, None, None, 12.0, 11.0),))
    d = m.detail(LID, D(29))
    assert d["status"] == "watch"
    assert {"id": "vol", "stage": "go", "value": pytest.approx(1.0), "threshold": 2.0} in d["distances"]   # 500 against a mean of 500


def test_a_ranking_aggregation_reads_the_first_bound_stage_in_rule_order():
    rule = BREAKOUT.replace('ranking:\n  - {expr: "box.width", dir: asc}', 'ranking:\n  - {expr: "MEAN(V)", dir: desc}')
    r = matcher(rule).at(D(29)).row(0, named=True)
    assert r["status"] == "watch" and r["rank0"] == 2000.0          # the rally (bars 0-9), not the box


def test_a_gold_breaks_out_when_its_small_box_does():
    # 下品拐点买: the buy is the small box's own breakout, not the big box's
    rule = """
id: g
name: 下品 · 小箱突破
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, variant: false}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
"""
    small = ("s1", "short", 23, 26, 28, 31, "上破离箱", 11.7, 11.4)
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": False, "big_box_id": "b1",
                         "small_box_id": ["s1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
    t = {"BOX": {"boxes": boxes(BOX, small), "touches": pl.DataFrame(),
                 "events": events(("s1", 28, "break_up"), ("b1", 30, "break_up"))},
         "BOX_DERIVED": {"gold": gold}}
    m = Matcher(load_rule(rule), "CN", frame(CLOSE), t)
    assert m.signals(D(0), D(len(CLOSE) - 1))["signal_date"].to_list() == [D(28)]


def test_a_gold_of_a_box_stage_is_that_box_s_gold():
    # 下品 · 大箱突破买: the big box's breakout, with a 下品 inside that same box
    rule = """
id: g
name: 下品 · 大箱突破
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: mid}}
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, of: big}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
"""
    small = ("s1", "short", 23, 26, None, None, None, 11.7, 11.4)

    def signals(big_box_id):
        gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": False, "big_box_id": [big_box_id],
                             "small_box_id": ["s1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                             "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
        t = {"BOX": {"boxes": boxes(BOX, small), "touches": pl.DataFrame(), "events": events(("b1", 30, "break_up"))},
             "BOX_DERIVED": {"gold": gold}}
        return Matcher(load_rule(rule), "CN", frame(CLOSE), t).signals(D(0), D(len(CLOSE) - 1))["signal_date"].to_list()

    assert signals("b1") == [D(30)]
    assert signals("another big box") == []                      # a 下品 of some other box is not this box's


def test_a_box_stage_picks_boxes_by_their_level():
    # 大箱 or 大箱里的中箱: a 中箱 standing alone is left out (its breakouts did worse than no box at all, 2016–2020)
    rule = """
id: z
name: 箱体突破
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {level: [大箱, 大箱里的中箱]}}
  - {id: go, kind: event, on: box.break_up}
signal_at: go.at
"""
    def signals(level):
        b = boxes(("b1", "mid", 10, 22, None, None, None, 12.0, 11.0)).with_columns(pl.lit(level).alias("level"))
        return Matcher(load_rule(rule), "CN", frame(CLOSE), tables(b, events(("b1", 30, "break_up")))).signals(D(0), D(33))["signal_date"].to_list()
    assert signals("大箱里的中箱") == [D(30)] and signals("大箱") == [D(30)]
    assert signals("中箱") == []


def test_with_of_names_a_box_stage_and_only_for_gold_or_nest():
    from wb.engine.l3.rule import RuleError
    base = """
id: g
name: t
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: mid}}
  - %s
  - {id: e, kind: event, on: big.break_up}
signal_at: e.at
"""
    with pytest.raises(RuleError, match="BOX"):                    # a slanted box is never a gold's big box
        load_rule(base.replace("  - %s\n", "  - {id: sl, kind: structure, use: SLANT_BOX}\n  - {id: g, kind: structure, use: GOLD, with: {of: sl}}\n"))
    for stage, msg in (("{id: g, kind: structure, use: GOLD, with: {of: nope}}", "nope"),
                       ("{id: g, kind: structure, use: SWING, with: {of: big}}", "金品"),
                       ("{id: g, kind: structure, use: GOLD, with: {of: e}}", "箱体")):
        with pytest.raises(RuleError, match=msg):
            load_rule(base % stage)


def test_a_variant_gold_breaks_out_when_its_slanted_small_box_does():
    rule = """
id: g
name: 变异下品
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品, variant: true}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
"""
    slant = boxes(("sl1", "slant", 23, 26, 28, 31, "上破离箱", 11.7, 11.4))
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": True, "big_box_id": "b1",
                         "small_box_id": ["sl1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
    t = {"BOX": {"boxes": boxes(BOX), "touches": pl.DataFrame(), "events": events(("b1", 30, "break_up"))},
         "SLANT_BOX": {"slant": slant, "events": events(("sl1", 28, "break_up"))}, "BOX_DERIVED": {"gold": gold}}
    m = Matcher(load_rule(rule), "CN", frame(CLOSE), t)
    assert m.signals(D(0), D(len(CLOSE) - 1))["signal_date"].to_list() == [D(28)]


def test_a_gold_event_with_no_events_at_all_simply_does_not_match():
    rule = """
id: g
name: 下品 · 小箱突破
markets: [CN]
stages:
  - {id: g, kind: structure, use: GOLD, with: {kind: 下品}}
  - {id: turn, kind: event, on: g.break_up}
signal_at: turn.at
"""
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品"], "variant": False, "big_box_id": "b1",
                         "small_box_id": ["s1"], "known_at": [D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": 11.7, "small_bottom": 11.4, "inner_distance": 12.0 / 11.7 - 1, "above_top": None})
    small = ("s1", "short", 23, 26, 28, 31, "上破离箱", 11.7, 11.4)
    for t in ({"BOX": {"boxes": boxes(BOX, small), "touches": pl.DataFrame(), "events": events()}, "BOX_DERIVED": {"gold": gold}},
              {"BOX_DERIVED": {"gold": gold}}):                                    # empty events, and no box tables at all
        m = Matcher(load_rule(rule), "CN", frame(CLOSE), t)
        assert m.signals(D(0), D(len(CLOSE) - 1)).is_empty()


def test_a_binding_says_how_many_bars_it_spans():
    bind = {b["stage"]: b for b in matcher().detail(LID, D(33))["bindings"]}   # the 选股 table's stage summaries
    assert (bind["rally"]["bars"], bind["box"]["bars"], bind["breakout"]["bars"], bind["hold"]["bars"]) == (10, 20, 1, 3)


def test_a_gold_a_later_small_box_replaced_is_no_longer_the_big_box_s():
    # 金品是大箱末端的小箱 — at the big box's breakout, a 下品 that a later small box in it replaced is not its 金品
    rule = """
id: g
name: 金品 · 大箱突破
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: mid}}
  - {id: g, kind: structure, use: GOLD, with: {kind: %s, of: big}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
"""
    early, late = ("s1", "short", 12, 15, 18, 19, "下破离箱", 11.6, 11.3), ("s2", "short", 22, 26, None, None, None, 11.98, 11.7)
    gold = pl.DataFrame({"listing_id": LID, "kind": ["下品", "上品"], "variant": False, "big_box_id": "b1",
                         "small_box_id": ["s1", "s2"], "known_at": [D(15), D(26)], "big_top": 12.0, "big_bottom": 11.0,
                         "small_top": [11.6, 11.98], "small_bottom": [11.3, 11.7], "inner_distance": [12.0 / 11.6 - 1, None],
                         "above_top": None, "superseded_at": [D(26), None]})
    t = {"BOX": {"boxes": boxes(BOX, early, late), "touches": pl.DataFrame(), "events": events(("b1", 30, "break_up"))},
         "BOX_DERIVED": {"gold": gold}}

    def run(kind):
        return Matcher(load_rule(rule % kind), "CN", frame(CLOSE), t).signals(D(0), D(len(CLOSE) - 1))["signal_date"].to_list()
    assert run("下品") == [] and run("上品") == [D(30)]


def test_an_event_on_the_bar_a_box_is_redrawn_is_still_the_box_s():
    # the box machine records a bar's 撞顶涨停 (or a 回箱 before an outer box is found) on the version standing when the
    # bar opened, and may redraw the box on that same bar: the version is superseded on the event's bar and the new
    # version does not carry the event. The event is the box's, so a rule on it signals that day. (CN 2013–2026:
    # 2,321 of 15,140 撞顶涨停 sat on a version superseded the same bar, 12% of the mid / long ones; 5,897 回箱.)
    rule = """
id: z
name: 撞顶涨停
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: hit, kind: event, on: box.limit_hit_top}
signal_at: hit.at
"""
    v1, v2 = ("b1", "mid", 10, 22, None, None, None, 12.0, 11.0), ("b2", "mid", 10, 28, None, None, None, 12.1, 11.0)
    b = boxes(v1, v2).with_columns(pl.Series("superseded_at", [D(28), None], dtype=pl.Date), pl.Series("lineage", ["b1", "b1"]))
    m = Matcher(load_rule(rule), "CN", frame(CLOSE), tables(b, events(("b1", 28, "limit_hit_top"))))
    assert m.signals(D(0), D(33))["signal_date"].to_list() == [D(28)]
    assert m.at(D(28)).row(0, named=True)["status"] == "signal"
    assert m.at(D(29)).row(0, named=True)["status"] != "signal"          # the day after, the old version is gone


def test_a_condition_on_a_stage_counts_a_rounding_tie_as_equal():
    # the rally runs from a 9.90 low to an 11.385 high: +15% exactly, though 11.385 / 9.9 − 1 is 0.1499999999999999 in
    # floating point; "RUNUP(H, L) >= 0.15" holds, as the same comparison does in a formula (EQ)
    close = [10 + 0.1 * i for i in range(10)] + CLOSE[10:]
    high = [c * 1.01 for c in close[:9]] + [11.385] + HIGH[10:]
    assert max(high[:10]) / (close[0] * 0.99) - 1 < 0.15
    d = matcher(close=close, high=high).detail(LID, D(33))
    assert {c["id"]: c for c in d["conditions"]}["runup"]["status"] == "pass"


def test_the_pool_can_leave_out_shares_that_never_did_the_reform():
    # the formula leaves out names starting with S (未股改): S佳通 (600182) still trades so in 2026
    rule = load_rule("""
id: s
name: x
markets: [CN]
universe: {exclude: [s_share]}
stages:
  - {id: today, kind: event, when: "C > 0"}
signal_at: today.at
""")
    df = pl.concat([frame([10.0] * 5).with_columns(pl.lit(True).alias("s_share")),
                    frame([10.0] * 5, lid="CN:600001").with_columns(pl.lit(False).alias("s_share"))])
    r = Matcher(rule, "CN", df, {}).at(D(4))
    assert r.filter(pl.col("status") == "signal")["listing_id"].to_list() == ["CN:600001"]


def test_a_switch_parameter_turns_a_stage_condition_on_and_off():
    # `$sw == 0 OR …` on a stage's bars: off, the rule finds what it finds without the condition; on, what it finds with the
    # condition alone — a condition of numbers and parameters is read where it stands, not as a column of the bars (a
    # stage's aggregation has no bar to read one on)
    from tests.test_l2_contract import with_dif
    from tests.test_l2_prefix import series
    from wb.engine.l2.spec import compute

    base = PREFIX.replace('      - {id: quiet, expr: "MEAN(V, last=3) <= MEAN(V) * 2"}\n', "{quiet}")
    switched = base.replace("{quiet}", '      - {id: quiet, expr: "$sw == 0 OR MEAN(V, last=3) <= MEAN(V) * 0.8"}\n') \
        .replace("markets: [CN]\n", "markets: [CN]\nparams:\n  - {id: sw, value: false}\n")
    plain = base.replace("{quiet}", '      - {id: quiet, expr: "MEAN(V, last=3) <= MEAN(V) * 0.8"}\n')
    none = base.replace("    during:\n{quiet}", "")
    df = with_dif(series(3, 300)).with_columns(pl.lit(LID).alias("listing_id"), pl.lit(1e9).alias("amount"))
    tables = compute(df)

    def found(text, **params):
        rule = load_rule(text)
        return Matcher(rule.with_params(params) if params else rule, "CN", df, tables).signals(df["date"][0], df["date"][-1])["signal_date"].to_list()
    off, on = found(switched), found(switched, sw=True)
    assert off == found(none) and on == found(plain) and set(on) < set(off)


BACK = """
id: back
name: 下破回箱
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: down, kind: event, on: box.break_down}
  - {id: back, kind: event, on: box.return, within: {stage: down, bars: 10}}
signal_at: back.at
"""


def test_a_return_is_explained_by_the_break_it_came_back_from():
    # 10-12, CN:000810: broke down 09-28, back 09-29, broke down again 10-08, back 10-12 — the 10-12 signal named the
    # 09-28 break (both within the 10 bars) although the stock had already come back from it
    close = [11.5] * 20 + [10.5, 11.5, 11.5, 11.5, 10.5, 10.6, 10.7, 11.5]
    m = Matcher(load_rule(BACK), "CN", frame(close), tables(boxes(("b1", "mid", 0, 10, 27, None, None, 12.0, 11.0)), events(
        ("b1", 20, "break_down"), ("b1", 21, "return"), ("b1", 24, "break_down"), ("b1", 27, "return"))))
    assert m.at(D(27)).row(0, named=True)["status"] == "signal"
    bind = {b["stage"]: b for b in m.detail(LID, D(27))["bindings"]}
    assert bind["down"]["at"] == D(24) and bind["back"]["at"] == D(27)


SHANG = """
id: s
name: 上品
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: mid}}
  - {id: g, kind: structure, use: GOLD, with: {kind: 上品, of: big}}
  - {id: breakout, kind: event, on: big.break_up}
signal_at: breakout.at
"""


def test_a_breakout_before_the_small_box_began_is_not_its_breakout():
    # 10-12: 10 of 42 上品 signals paired a big box's old breakout (weeks before, then back in the box) with a small box
    # drawn under the top after it — signalled the day the small box was known, the close under the break line
    big = ("b1", "mid", 10, 22, 39, None, None, 12.0, 11.0)
    small = ("s1", "short", 34, 37, None, None, None, 12.0, 11.7)
    gold = pl.DataFrame({"listing_id": LID, "kind": ["上品"], "variant": False, "big_box_id": "b1", "small_box_id": ["s1"],
                         "known_at": [D(37)], "big_top": 12.0, "big_bottom": 11.0, "small_top": 12.0, "small_bottom": 11.7,
                         "inner_distance": None, "above_top": None})
    t = {"BOX": {"boxes": boxes(big, small), "touches": pl.DataFrame(),
                 "events": events(("b1", 30, "break_up"), ("b1", 31, "return"), ("b1", 39, "break_up"))},
         "BOX_DERIVED": {"gold": gold}}
    close = CLOSE + [11.8, 11.85, 11.9, 11.8, 11.9, 12.4]
    m = Matcher(load_rule(SHANG), "CN", frame(close), t)
    assert m.at(D(37)).row(0, named=True)["status"] != "signal"      # the small box known: no breakout that day
    assert m.at(D(39)).row(0, named=True)["status"] == "signal"      # the breakout after it is
    assert {b["stage"]: b["at"] for b in m.detail(LID, D(39))["bindings"]}["breakout"] == D(39)


def test_universe_leaves_out_a_stock_just_back_from_a_long_suspension():
    """You chose (10-13, review C): A 股 over 10 years, 453 stocks back from over 120 days' suspension gave 441 signals on
    the day they came back (a box from years before 'broken' by the gap) — `resumed_lt_<N>d` keeps the first N bars after
    a suspension of 30 days or more out of the pool (a holiday is never that long)."""
    rule = """
id: r
name: x
markets: [CN]
universe: {exclude: [resumed_lt_3d]}
stages:
  - {id: today, kind: event, when: "C > 0"}
signal_at: today.at
"""
    f = frame([10.0] * 12)
    f = f.with_columns(pl.Series("date", [D(i) for i in range(6)] + [D(i + 60) for i in range(6, 12)]))   # 2 months off
    m = Matcher(load_rule(rule), "CN", f, {})
    assert m.at(D(5)).row(0, named=True)["status"] == "signal"
    assert [m.at(D(i + 60)).is_empty() for i in range(6, 12)] == [True, True, True, False, False, False]   # 3 bars out


def test_the_bars_since_a_long_suspension():
    from tests.test_l1_compiler import run
    f = frame([10.0] * 8).with_columns(pl.Series("date", [D(i) for i in range(4)] + [D(i + 45) for i in range(4, 8)]))
    assert run("BARSRESUMED", f) == [0, 1, 2, 3, 0, 1, 2, 3]


BOXUP = """
id: bu
name: 箱体上破
markets: [CN]
stages:
  - {id: big, kind: structure, use: BOX, with: {scale: mid}}
  - {id: up, kind: event, on: big.break_up}
signal_at: up.at
"""


def test_a_second_breakout_of_the_same_box_is_not_its_first():
    """You chose (10-13): the scan signals a box broken out of, back in it and broken out of again each time, a backtest
    counts the first only (上品: 28.7% of 60 days' day signals) — `first` says which a day's signal is, the scan marks
    the other 再次突破."""
    t = tables(boxes(("b1", "mid", 10, 22, None, None, None, 12.0, 11.0)),
               events(("b1", 30, "break_up"), ("b1", 31, "return"), ("b1", 34, "break_up")))
    m = Matcher(load_rule(BOXUP), "CN", frame(CLOSE + [11.9, 11.95]), t)
    assert m.at(D(30)).row(0, named=True)["status"] == "signal" and m.first(LID, D(30))
    assert m.at(D(34)).row(0, named=True)["status"] == "signal" and not m.first(LID, D(34))
