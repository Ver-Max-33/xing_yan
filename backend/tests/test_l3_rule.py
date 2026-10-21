"""Rules as data (spec §5.2): one Pydantic model for hand-written YAML and AI-written JSON."""
import pytest

from wb.engine.l3.rule import RuleError, json_schema, load_rule, rule_hash

EXAMPLE = """
id: box_breakout_after_rally
version: 3
name: 上涨后箱体缩量、放量突破
markets: [CN, JP]
universe:
  exclude: [st, suspended, listed_lt_120d]
  min_avg_amount_20d: {CN: 50_000_000, JP: 300_000_000}
params:
  - {id: box_max_width, value: 0.15, range: [0.08, 0.30], step: 0.01, tune: true}
  - {id: box_tol,       value: 0.015, range: [0.005, 0.03], step: 0.005, tune: true}
  - {id: vol_spike,     value: 2.0,  range: [1.5, 3.0],  step: 0.25, tune: true}
stages:
  - id: rally
    kind: window
    before: {stage: box, bars: [1, 60]}
    during:
      - {id: runup, expr: "RUNUP(H, L) >= 0.20"}
  - id: box
    kind: structure
    use: BOX
    with:
      scale: mid
      tol: $box_tol
      min_touch: {top: 2, bottom: 2}
      inside: 0.8
      spike: {return_within: 3}
      max_width: $box_max_width
    during:
      - {id: shrink, expr: "MEAN(V, last=10) / MEAN(V, of=rally) < 0.7"}
  - id: breakout
    kind: event
    on: box.break_up
    at:
      - {id: volume, expr: "BAND_VOL_RATIO(box) >= $vol_spike"}
      - {id: macd,   expr: "BARSLAST(CROSS(MACD.DIF, MACD.DEA)) <= 3 AND MACD.DIF > 0"}
  - id: hold
    kind: window
    after: {stage: breakout, bars: [1, 3]}
    all: "C > box.top"
signal_at: hold.end
watch_stage: box
ranking:
  - {expr: "box.width", dir: asc}
  - {expr: "BAND_VOL_RATIO(box)@breakout", dir: desc}
"""


def test_the_spec_example_loads():
    r = load_rule(EXAMPLE)
    assert r.id == "box_breakout_after_rally" and [s.id for s in r.stages] == ["rally", "box", "breakout", "hold"]
    box = r.stage("box")
    # the method's words map onto the primitive's parameters; scale is a filter
    assert box.scale == "mid"
    assert r.primitive_params() == {"BOX": {"tol": 0.015, "mid.min_top": 2, "mid.min_bottom": 2, "inside": 0.8,
                                               "spike_return": 3, "mid.max_width": 0.15}}
    assert r.param("vol_spike") == 2.0 and r.universe.listed_days == 120
    assert r.signal == ("hold", "end") and r.watch_stage == "box"


def test_the_same_rule_written_as_json_has_the_same_hash():
    import json

    from wb.engine.l3.rule import load_text

    a = load_rule(EXAMPLE)
    b = load_rule(json.dumps(load_text(EXAMPLE)))
    assert rule_hash(a) == rule_hash(b)
    c = load_rule(EXAMPLE.replace("value: 2.0", "value: 2.5"))
    assert rule_hash(c) != rule_hash(a)  # a parameter change is a new version (spec §5.9)


@pytest.mark.parametrize("bad, words", [
    (EXAMPLE.replace("stage: box, bars: [1, 60]", "stage: boxx, bars: [1, 60]"), "boxx"),
    (EXAMPLE.replace("on: box.break_up", "on: box.fly_away"), "fly_away"),
    (EXAMPLE.replace("use: BOX", "use: TRIANGLE"), "TRIANGLE"),
    (EXAMPLE.replace("tol: $box_tol", "tol: $no_such_param"), "no_such_param"),
    (EXAMPLE.replace("inside: 0.8", "insde: 0.8"), "insde"),
    (EXAMPLE.replace('"RUNUP(H, L) >= 0.20"', '"RUNUP(H, L >= "'), "runup"),
    (EXAMPLE.replace("signal_at: hold.end", "signal_at: nowhere.end"), "nowhere"),
    (EXAMPLE.replace("watch_stage: box", "watch_stage: hold"), "watch_stage"),
    (EXAMPLE.replace('      - {id: shrink, expr: "MEAN(V, last=10) / MEAN(V, of=rally) < 0.7"}',
                     '      - {id: shrink, expr: "V < 1000"}'), "汇总"),
    (EXAMPLE.replace('"BAND_VOL_RATIO(box) >= $vol_spike"', '"pressure.spaces >= 0.1"'), "spaces"),
    (EXAMPLE.replace('"MEAN(V, last=10) / MEAN(V, of=rally) < 0.7"', '"MEAN(C, last=10) / pressure.bottom < 0.9"'), "按某一天"),
    (EXAMPLE.replace("  - id: rally\n", "  - id: pressure\n").replace("stage: rally", "stage: pressure")
     .replace("of=rally", "of=pressure"), "保留"),
    (EXAMPLE.replace('"BAND_VOL_RATIO(box) >= $vol_spike"', '"REL_PERF(20, of=rally) > 0"'), "汇总函数"),
    # attributes that change after the box is confirmed would pick boxes by their future
    (EXAMPLE.replace("      scale: mid\n", "      scale: mid\n      end_reason: 上破离箱\n"), "还会变"),
    (EXAMPLE.replace("      scale: mid\n", "      scale: mid\n      touches_top: 3\n"), "还会变"),
    (EXAMPLE.replace("      scale: mid\n", "      scale: mid\n      status: proto\n"), "雏形"),
    # windows, gaps and relations that would silently do nothing
    (EXAMPLE.replace("before: {stage: box, bars: [1, 60]}", "before: {stage: box, bars: [60, 1]}"), "bars"),
    (EXAMPLE.replace("after: {stage: breakout, bars: [1, 3]}", "after: {stage: breakout, bars: [0, 3]}"), "bars"),
    (EXAMPLE.replace("    on: box.break_up\n", "    on: box.break_up\n    within: {stage: box, bars: 0}\n"), "within"),
    (EXAMPLE.replace("    use: BOX\n", "    use: BOX\n    before: {stage: rally, bars: [1, 5]}\n"), "before"),
    (EXAMPLE.replace("    use: BOX\n", "    use: BOX\n    after: {stage: rally, bars: [1, 5]}\n"), "gap"),
    (EXAMPLE.replace("    use: BOX\n", "    use: BOX\n    after: {stage: rally, gap: [5, 1]}\n"), "gap"),
    # a cycle between stages is found when the rule is checked, not when it runs
    (EXAMPLE.replace("    use: BOX\n", "    use: BOX\n    after: {stage: hold, gap: [1, 5]}\n"), "环"),
])
def test_mistakes_are_reported_in_chinese_with_the_place(bad, words):
    with pytest.raises(RuleError) as e:
        load_rule(bad)
    assert words in str(e.value)


def test_on_and_off_stay_words_in_yaml():
    # YAML 1.1 turns the key `on:` into True; rules read it as the word
    from wb.engine.l3.rule import load_text

    assert load_text("on: box.break_up\nx: yes") == {"on": "box.break_up", "x": "yes"}


def test_json_schema_has_fixed_keys_for_user_named_things():
    s = json_schema()
    assert s["properties"]["params"]["type"] == "array" and s["properties"]["stages"]["type"] == "array"


def test_the_pressure_look_back_is_a_rule_setting():
    assert load_rule(EXAMPLE).pressure.lookback == 500
    assert load_rule(EXAMPLE + "pressure: {lookback: 250}\n").pressure.lookback == 250
    with pytest.raises(RuleError):
        load_rule(EXAMPLE + "pressure: {lookback: 0}\n")
    assert load_rule(EXAMPLE).pressure.scales == ["mid", "long"]
    with pytest.raises(RuleError):
        load_rule(EXAMPLE + "pressure: {scales: [huge]}\n")


def test_a_per_scale_setting_needs_one_scale():
    # with a list of scales, `max_width` would name no scale and be dropped without a word (§5.5)
    text = """
id: t
name: t
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: [mid, long], max_width: 0.2}}
signal_at: box.at
"""
    with pytest.raises(RuleError, match="mid.max_width"):
        load_rule(text)


def test_a_parameter_nothing_uses_is_refused():
    # §5.5: nothing written may silently do nothing — a slider or a clarification card on it would change nothing
    text = """id: x
name: x
markets: [CN]
params:
  - {id: n, value: 20, range: [5, 60], step: 1}
  - {id: confirm, value: 3, range: [1, 5], step: 1}
stages:
  - {id: today, kind: event, when: "C > MA(C, $n)"}
signal_at: today.at
"""
    with pytest.raises(RuleError, match="confirm.*没有用到"):
        load_rule(text)
    load_rule(text.replace("  - {id: confirm, value: 3, range: [1, 5], step: 1}\n", ""))


HOLD = """id: hold_after
name: 站上均线后守住
markets: [CN, JP]
params:
  - {id: hold, value: 3, range: [1, 10], step: 1}
stages:
  - {id: today, kind: event, when: "C > MA(C, 20)"}
  - id: keep
    kind: window
    after: {stage: today, bars: [1, $hold]}
    all: "C > 0"
signal_at: keep.end
"""


def test_a_window_length_can_be_a_whole_number_parameter():
    # §6.6: a number the description leaves open is a parameter — "突破后 3 天" too
    r = load_rule(HOLD)
    assert r.span(r.stage("keep").after.bars) == (1, 3)
    five = r.with_params({"hold": 5})
    assert five.span(five.stage("keep").after.bars) == (1, 5)
    with pytest.raises(RuleError, match="hold.*整数"):
        load_rule(HOLD.replace("{id: hold, value: 3, range: [1, 10], step: 1}", "{id: hold, value: 0.5, range: [0.1, 1], step: 0.1}"))
    with pytest.raises(RuleError, match="没有叫 nope 的参数"):
        load_rule(HOLD.replace("$hold]", "$nope]"))
    with pytest.raises(RuleError, match="1 ≤ a ≤ b"):
        load_rule(HOLD.replace("bars: [1, $hold]", "bars: [$hold, 2]"))


def test_all_written_like_a_condition_list_is_told_it_is_one_formula():
    text = HOLD.replace('    all: "C > 0"', '    all:\n      - {id: up, expr: "C > 0"}')
    with pytest.raises(RuleError, match=r"stages\[1\]\.all：要写成一条公式文字.*AND"):
        load_rule(text)


def test_the_pool_options_load_and_a_name_cannot_break_the_formula():
    r = load_rule(HOLD.replace("signal_at:", "universe: {boards: [创业板], industries: [电子], tags: [锂电], watchlist: 重点}\nsignal_at:"))
    assert (r.universe.boards, r.universe.industries, r.universe.tags, r.universe.watchlist) == (["创业板"], ["电子"], ["锂电"], "重点")
    with pytest.raises(RuleError, match="universe.tags"):
        load_rule(HOLD.replace("signal_at:", 'universe: {tags: [\'a"b\']}\nsignal_at:'))
    with pytest.raises(RuleError, match="universe.index：CN:IDX:000300 是 A 股的指数，markets 只能写 CN"):
        load_rule(HOLD.replace("signal_at:", "universe: {index: CN:IDX:000300}\nsignal_at:"))      # HOLD is [CN, JP]
