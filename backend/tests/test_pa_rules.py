"""价格行为分析: where the workbench's rules differ from PA_Agent's own, each found by the audit of 12 real analyses
(10-21) that were all 不买 — the vendored code's changes are in backend/pa/a_share.patch."""
import pytest
from pa_agent.ai.decision_continuity import always_in_is_weak, apply_continuity_guard, render_continuity_prompt_block
from pa_agent.ai.decision_stance import build_decision_stance_guidance
from pa_agent.ai.prompt_assembler import _A_SHARE_RULES_ZH
from types import SimpleNamespace

from pa_agent.util.trade_metrics import (validate_limit_order_k1_freshness, validate_order_trade_metrics,
                                         widen_stop_for_t_plus_1)


def plan(**kw) -> dict:
    """三全食品's own plan (10-21): a limit buy at 11.71, its stop one tick under K3's low, TP1 at K1's high."""
    d = {"order_type": "限价单", "order_direction": "做多", "entry_price": 11.71, "stop_loss_price": 11.54,
         "take_profit_price": 11.99, "take_profit_price_2": 12.26, "estimated_win_rate": 48}
    return {**d, **kw}


def test_the_trader_equation_reads_the_ais_own_prices_not_the_stop_the_program_widened():
    # the AI's 48% is for its own stop: 0.48 × 0.28 > 0.52 × 0.17 passes. PA widens the stop to 1:1 (11.43) and re-ran
    # the equation there with the same 48% — a rate for another stop — so the plan was refused
    d = plan()
    assert validate_order_trade_metrics(d) == []
    assert d["stop_loss_price"] == pytest.approx(11.43)          # still widened to 1:1, as PA places the order
    assert d["ai_stop_loss_price"] == 11.54                      # the AI's own stop, kept beside it


def test_a_plan_failing_its_own_equation_is_still_refused():
    errors = validate_order_trade_metrics(plan(estimated_win_rate=30))   # 0.30 × 0.28 < 0.70 × 0.17
    assert any("trader equation fails at 30% win rate (risk=0.17, reward=0.28" in e for e in errors)


def stage2(*, boundary: str = "是", side: str = "lower", quality: str = "medium", bar: str | None = "K1") -> dict:
    """东箭科技's case (10-21): K2 undercut the range floor and closed back in, K1 a bull bar above K2's high."""
    return {"decision": {"order_type": "突破单", "order_direction": "做多", "entry_price": 8.68, "stop_loss_price": 8.31,
                         "take_profit_price": 9.05, "take_profit_price_2": 9.33, "estimated_win_rate": 55},
            "decision_trace": [{"node_id": "6.3", "answer": boundary, "branch": side, "reason": "收在区间下三分之一"}],
            "bar_analysis": {"signal_bar": {"bar": bar, "quality": quality, "reason": "假跌破后收回"}}}


def ctx(**kw) -> dict:
    """Stage 1 said: a neutral trading range, the program's §2.4 a weak AIS."""
    return {"direction": "neutral", "always_in_branch": "AIS", "always_in_weak": True, "cycle_position": "trading_range",
            "has_previous_plan": False, **kw}


def kept(s2: dict, c: dict) -> bool:
    return apply_continuity_guard(s2, c)["decision"]["order_type"] != "不下单"


def test_a_long_at_a_range_bottom_on_a_signal_bar_passes_a_weak_ais():
    # 8 days drifting under a flat EMA20 make a weak AIS at nearly every range bottom: 96% of 1,043 stocks there on 10-20
    assert kept(stage2(), ctx())
    assert kept(stage2(), ctx(cycle_position="trending_tr"))


@pytest.mark.parametrize("s2, c", [
    (stage2(), ctx(always_in_weak=False)),                    # a strong AIS: the structure is confirmed down
    (stage2(), ctx(cycle_position="normal_channel")),          # a channel, not a range
    (stage2(boundary="否", side="middle"), ctx()),             # not at the range bottom
    (stage2(boundary="否", side="lower"), ctx()),              # near it, but 6.3 did not say at the boundary
    (stage2(quality="weak"), ctx()),                           # no signal bar of quality
    (stage2(bar=None, quality="invalid"), ctx()),              # a planned limit: no signal bar at all
])
def test_otherwise_an_ais_still_forbids_the_long(s2, c):
    assert not kept(s2, c)


def test_without_always_in_a_long_needs_a_signal_bar_of_quality():
    # §9.0P: 「§2.4 答案为「否」→ 仅 §9.0=是 且有合格信号棒时可下单」; the program refused every order there
    assert kept(stage2(boundary="否", side="middle"), ctx(always_in_branch=None))
    assert not kept(stage2(quality="weak"), ctx(always_in_branch=None))


def test_the_weak_label_is_read_from_the_programs_own_2_4():
    s1 = {"gate_trace": [{"node_id": "2.4", "answer": "是", "branch": "AIS",
                          "reason": "加权收盘低于EMA占比88.9%≥65%。判定为Always In Short（AIS）（结构弱/回撤深，弱AIS）。"}]}
    assert always_in_is_weak(s1)
    s1["gate_trace"][0]["reason"] = "判定为Always In Short（AIS）（结构确认，强AIS）。"
    assert not always_in_is_weak(s1)
    s1["gate_trace"][0]["reason"] = "判定为Always In Long（AIL）（结构弱/回撤深，弱AIL）。"   # the AI overrode it to AIS:
    s1["gate_trace"][0]["overridden_by_ai"] = True                                       # the reason is still the program's
    assert not always_in_is_weak(s1)
    s1["gate_trace"][0]["reason"] = "判定为Always In Short（AIS）（结构弱/回撤深，弱AIS）。"
    assert not always_in_is_weak(s1)                                                     # the program's call, not in force


def test_the_a_share_rules_say_spot_only_the_range_bottom_exception_and_the_daily_stop_filter():
    r = _A_SHARE_RULES_ZH   # first in both stages' system prompt, ahead of anything it overrides
    assert "只做现货" in r and "不融资融券" in r and "不加杠杆" in r          # 10-21: spot only, no leverage
    assert "「弱AIS」" in r and "branch=lower" in r and "strong 或 medium" in r   # the exception, as the program checks it
    assert "2 倍 ATR14" in r and "区间宽度 10%" in r and "8跳动" in r            # the daily stop filter, and what it replaces
    assert "至少要离买入价 1 个 ATR14" in r                                       # T+1 (10-21): the stop's floor
    assert "以程序算的「收盘位置分位」为准" in r                                     # §6.3 as PA computes it, not the AI's own


def test_the_continuity_rules_point_an_ais_to_that_exception():
    block = render_continuity_prompt_block({"has_previous_plan": False, "direction": "neutral", "always_in_branch": "AIS",
                                            "cooldown_bars": 3})
    assert "唯一例外见「A 股规则」第 1 条" in block


def test_the_stance_guidance_no_longer_sends_an_ais_to_the_short_side():
    g = build_decision_stance_guidance("balanced")    # the stance PA runs at here (wb/pa/settings.py)
    assert "AIS→只做空侧" not in g and "唯一例外见「A 股规则」第 1 条" in g


def frame(*, atr: float, low: float, close: float = 9.20) -> SimpleNamespace:
    """K1 alone, as PA reads it: its ATR14 first in the indicators, its prices to the cent."""
    k1 = SimpleNamespace(seq=1, closed=True, open=9.10, high=9.21, low=low, close=close)
    return SimpleNamespace(bars=(k1,), indicators=SimpleNamespace(atr14=(atr,), ema20=(9.27,)))


def test_t_plus_1_moves_a_stop_closer_than_one_atr_out_to_it_and_says_so_when_the_target_no_longer_pays():
    # 迈克生物's plan (10-21 re-run): buy 9.18, stop 9.00 — 0.75 of K1's ATR 0.24, one cent under K1's low. A share
    # bought today cannot be sold today: such a stop is crossed on the day the buy fills in 19% of fills (all A-shares
    # 2024–2026), then sold at the next open. Out to one ATR: 8.94; TP1 9.36 pays 0.18 on 0.24 — 0.75:1
    d = plan(entry_price=9.18, stop_loss_price=9.00, take_profit_price=9.36, take_profit_price_2=9.43, estimated_win_rate=55)
    errors = validate_order_trade_metrics(d, kline_frame=frame(atr=0.24, low=9.01))
    assert d["stop_loss_price"] == 8.94 and d["ai_stop_loss_price"] == 9.00
    assert any(e.startswith("decision prices: T+1") and "0.75 : 1" in e for e in errors)


def test_t_plus_1_leaves_a_stop_already_one_atr_away():
    for sl in (11.40, 11.47):                          # 0.31 away, and exactly one ATR (0.24)
        d = plan(entry_price=11.71, stop_loss_price=sl)
        assert widen_stop_for_t_plus_1(d, kline_frame=frame(atr=0.24, low=11.76, close=11.82)) is False
        assert d["stop_loss_price"] == sl and "ai_stop_loss_price" not in d


def test_t_plus_1_does_not_turn_a_stop_at_or_over_the_entry_into_a_buy():
    # final review 2: a long whose stop is not under its entry is no plan; moved to one ATR under, it became one
    for sl in (9.30, 9.18):
        d = plan(entry_price=9.18, stop_loss_price=sl, take_profit_price=9.50, take_profit_price_2=9.60, estimated_win_rate=55)
        assert validate_order_trade_metrics(d, kline_frame=frame(atr=0.24, low=9.21, close=9.25))
        assert d["stop_loss_price"] == sl and "ai_stop_loss_price" not in d


def test_k1_below_the_ais_own_stop_ends_the_plan_even_when_the_program_widened_it():
    # PA's §9.0P: 「K1 已触及 stop 仍判方案无效」 — the stop the AI set at the structure. The program's wider one (1:1, T+1)
    # is no structure: K1's low 8.99 already broke the AI's 9.00, yet against 8.86 it passed (10-21)
    d = plan(entry_price=9.18, stop_loss_price=9.00, take_profit_price=9.50, take_profit_price_2=9.70, estimated_win_rate=55)
    ba = {"entry_bar": {"freshness": "pending", "strength": "not_triggered", "bar": None}}
    errors = validate_order_trade_metrics(d, kline_frame=frame(atr=0.24, low=8.99, close=9.25), bar_analysis=ba)
    assert d["stop_loss_price"] == 8.86 and d["ai_stop_loss_price"] == 9.00
    assert any("already at/below stop 9;" in e for e in errors)


def test_an_exact_1_to_1_plan_is_not_refused_by_float_error():
    # final review 3: in floats 10.19 − 9.80 = 0.3899…97 and 9.80 − 9.41 = 0.3900…06, so 1:1 read 0.9999999999999954 and
    # failed 「1.00 : 1 below minimum 1.00:1」 — 21% of exact 1:1 plans on the cent grid; the T+1 move then took the blame
    d = plan(entry_price=9.80, stop_loss_price=9.41, take_profit_price=10.19, take_profit_price_2=10.58, estimated_win_rate=55)
    assert validate_order_trade_metrics(d) == []
    d = plan(entry_price=9.80, stop_loss_price=9.60, take_profit_price=10.19, take_profit_price_2=10.58, estimated_win_rate=55)
    assert validate_order_trade_metrics(d, kline_frame=frame(atr=0.39, low=9.85, close=9.90)) == []
    assert d["stop_loss_price"] == 9.41                # one ATR under the entry for T+1: exactly 1:1


def test_k1_has_touched_the_stop_only_when_its_low_reached_it():
    # the check counted a low one tick above the stop as touched: 9.01 against 9.00 threw the plan out
    d = plan(entry_price=9.18, stop_loss_price=9.00, take_profit_price=9.36)
    ba = {"entry_bar": {"freshness": "pending", "strength": "not_triggered", "bar": None}}
    assert not [e for e in validate_limit_order_k1_freshness(d, frame(atr=0.24, low=9.01), bar_analysis=ba) if "stop" in e]
    assert [e for e in validate_limit_order_k1_freshness(d, frame(atr=0.24, low=9.00), bar_analysis=ba) if "stop" in e]


def test_a_planned_limit_one_tick_past_the_close_is_not_refused_by_float_error():
    # review of 1fc7dec (16): 6.61 − 0.01 is 6.6000000000000005 in floats, so a planned buy limit exactly one tick over
    # K1's close 6.60 read 「close below entry」 and was refused — about one close in ten from ¥1 to ¥30; two ticks
    # over still is. The same for a sell limit one tick under (2.01 + 0.01 against a close of 2.02)
    ba = {"entry_bar": {"freshness": "pending", "strength": "not_triggered", "bar": None}}
    k1 = frame(atr=0.2, low=6.55, close=6.60)
    assert not [e for e in validate_limit_order_k1_freshness(plan(entry_price=6.61, stop_loss_price=6.40), k1, bar_analysis=ba)
                if "below entry" in e]
    assert [e for e in validate_limit_order_k1_freshness(plan(entry_price=6.62, stop_loss_price=6.40), k1, bar_analysis=ba)
            if "below entry" in e]
    short = plan(order_direction="做空", entry_price=2.01, stop_loss_price=2.20)
    assert not [e for e in validate_limit_order_k1_freshness(short, frame(atr=0.1, low=1.95, close=2.02), bar_analysis=ba)
                if "above entry" in e]


def test_6_3_is_the_zone_pa_computes_not_the_ais_own_call():
    # PA works out where the close sits in its 40-bar range, in thirds (compute_simple_market_features), and shows it
    # to the AI, which then judged §6.3 itself: 300978 at 0.29 was 「下边界」 in one run and 「中部」 in the next (10-21)
    from pa_agent.ai.decision_nodes import DecisionNodeEngine
    from pa_agent.ai.market_features import compute_simple_market_features

    from tests.pa_fixtures import synthetic_frame
    f = synthetic_frame(trend=-0.002)                                 # falling: the close near its range's low
    assert compute_simple_market_features(f).zone == "lower_third"
    out = {"decision": {"order_type": "不下单"}, "decision_trace": [
        {"node_id": "6.1", "answer": "是", "reason": "边界清楚"}, {"node_id": "6.3", "answer": "否", "branch": "middle", "reason": "AI：在中部"}]}
    DecisionNodeEngine.apply_stage2(out, f, {})
    n = next(x for x in out["decision_trace"] if x["node_id"] == "6.3")
    assert (n["answer"], n["branch"]) == ("是", "lower") and "收盘位置分位" in n["reason"]


def test_a_path_that_never_went_through_6_gets_no_6_3():
    from pa_agent.ai.decision_nodes import DecisionNodeEngine

    from tests.pa_fixtures import synthetic_frame
    out = {"decision": {"order_type": "不下单"}, "decision_trace": [{"node_id": "4.1", "answer": "是", "reason": "通道"}]}
    DecisionNodeEngine.apply_stage2(out, synthetic_frame(trend=-0.002), {})
    assert not [x for x in out["decision_trace"] if x["node_id"] == "6.3"]


def middle_third_frame():
    """A synthetic frame whose close PA puts in the middle third of its 40-bar range."""
    from pa_agent.ai.market_features import compute_simple_market_features

    from tests.pa_fixtures import synthetic_frame
    return next(x for t in (0.0, 0.0005, -0.0005, 0.001) if compute_simple_market_features(x := synthetic_frame(trend=t)).zone == "middle_third")


def buy_under(f) -> dict:
    """A limit buy 1% under K1's close, its stop 5% under, TP1 4% and TP2 8% over."""
    c = f.bars[0].close
    return plan(entry_price=round(c * 0.99, 2), stop_loss_price=round(c * 0.95, 2), take_profit_price=round(c * 1.04, 2),
                take_profit_price_2=round(c * 1.08, 2))


def test_an_order_where_pa_computes_the_middle_third_is_not_placed_and_says_why():
    # PA's own §6.3: 「否，在中间1/3：不交易」 — with the zone computed, so is this rule
    from pa_agent.ai.decision_nodes import DecisionNodeEngine

    from wb.pa.plans import program_changes
    f = middle_third_frame()
    ai = buy_under(f)
    out = {"decision": dict(ai), "decision_trace": [{"node_id": "6.3", "answer": "是", "branch": "lower", "reason": "AI：在下沿"}]}
    DecisionNodeEngine.apply_stage2(out, f, {})
    assert out["decision"]["order_type"] == "不下单" and out["terminal"]["node_id"] == "6.3"
    why = program_changes(ai, out["decision"], out)[0]["reason"]   # in the stock's numbers, plainly (10-21)
    assert why.startswith("区间中部不交易：近 40 根 K 线在") and f"最新收盘 {f.bars[0].close:.2f} 落在中间那一段" in why


def test_6_3_only_where_pas_tree_reaches_it():
    # final review 4: PA reaches §6.3 through 6.1 = 是. 「没有清楚的上下边界，按宽通道走」 is a channel, whose own lines are not
    # the 40-bar envelope's thirds: no §6.3 there and no middle-third veto. A range path the AI cut short still gets it
    from pa_agent.ai.decision_nodes import DecisionNodeEngine
    f = middle_third_frame()
    channel = {"decision": buy_under(f), "decision_trace": [
        {"node_id": "6.1", "answer": "否", "reason": "没有清楚的上下边界，按宽通道走"}, {"node_id": "4.1", "answer": "是", "reason": "宽通道"}]}
    DecisionNodeEngine.apply_stage2(channel, f, {})
    assert not [x for x in channel["decision_trace"] if x["node_id"] == "6.3"]
    assert (channel.get("terminal") or {}).get("node_id") != "6.3" and channel["decision"]["order_type"] == "限价单"
    cut = {"decision": buy_under(f), "decision_trace": [{"node_id": "6.1", "answer": "是", "reason": "边界清楚"}]}
    DecisionNodeEngine.apply_stage2(cut, f, {})
    assert cut["decision"]["order_type"] == "不下单" and cut["terminal"]["node_id"] == "6.3"


def test_the_ais_6_3_written_with_a_section_sign_is_replaced_too():
    # final review 8: PA's merge took the AI's 「§6.3」 for another node than the program's 「6.3」, so both stayed and the
    # AI's could open rule 1's exception against the program's value
    from pa_agent.ai.decision_nodes import DecisionNodeEngine

    from tests.pa_fixtures import synthetic_frame
    out = {"decision": {"order_type": "不下单"}, "decision_trace": [
        {"node_id": "§6.1", "answer": "是", "reason": "边界清楚"},
        {"node_id": "§6.3", "answer": "是", "branch": "upper", "reason": "AI：在上沿"}]}
    DecisionNodeEngine.apply_stage2(out, synthetic_frame(trend=-0.002), {})
    assert [(x["answer"], x["branch"]) for x in out["decision_trace"]
            if str(x["node_id"]).lstrip("§") == "6.3"] == [("是", "lower")]


def test_a_buy_priced_in_the_middle_third_of_a_range_is_not_placed_and_says_where():
    # 002791 (10-21): the close at 0.77 of its 40-bar range (upper edge), the AI's limit buy 15.39 at 0.54. PA says it in
    # its own words — 「中间位置不挂单」 (§11.3), 「在区间中部入场」 forbidden (§14), §9.0P 否 in the middle — but measured
    # only the close (10-21)
    from pa_agent.ai.decision_nodes import DecisionNodeEngine
    from pa_agent.ai.market_features import compute_simple_market_features

    from tests.pa_fixtures import synthetic_frame
    from wb.pa.plans import program_changes
    f = synthetic_frame(trend=0.002)                                   # rising: the close in the upper third
    m = compute_simple_market_features(f)
    assert m.zone == "upper_third"
    mid = round(m.range_low + (m.range_high - m.range_low) / 2, 2)
    ai = plan(entry_price=mid, stop_loss_price=round(mid * 0.97, 2), take_profit_price=round(mid * 1.03, 2),
              take_profit_price_2=round(mid * 1.06, 2))
    out = {"decision": dict(ai), "decision_trace": [{"node_id": "6.1", "answer": "是", "reason": "边界清楚"}]}
    DecisionNodeEngine.apply_stage2(out, f, {})
    assert out["decision"]["order_type"] == "不下单" and out["terminal"]["node_id"] == "11.3"
    why = program_changes(ai, out["decision"], out)[0]["reason"]
    assert why.startswith("买入价在区间中部，不挂单：近 40 根 K 线在") and f"买入价 {mid:.2f} 落在中间那一段" in why


def test_a_short_priced_mid_range_is_refused_as_a_short_not_as_a_buy():
    # review of 1fc7dec (4): the entry check read every order as a buy — a 做空 limit in the middle third ended 不下单 with
    # 「买入价在区间中部…只在靠近底部的地方买」, so the A-share guard's 「A 股不能做空」 never showed
    from pa_agent.ai.decision_nodes import DecisionNodeEngine
    from pa_agent.ai.market_features import compute_simple_market_features

    from tests.pa_fixtures import synthetic_frame
    from wb.pa.plans import a_share_guard
    f = synthetic_frame(trend=0.002)                                   # the close in the upper third: §6.3 lets it be
    m = compute_simple_market_features(f)
    mid = round(m.range_low + (m.range_high - m.range_low) / 2, 2)
    short = plan(order_direction="做空", entry_price=mid, stop_loss_price=round(mid * 1.03, 2),
                 take_profit_price=round(mid * 0.97, 2), take_profit_price_2=round(mid * 0.94, 2), trade_confidence=60)
    out = {"decision": dict(short), "decision_trace": [{"node_id": "6.1", "answer": "是", "reason": "边界清楚"}]}
    DecisionNodeEngine.apply_stage2(out, f, {})
    assert (out.get("terminal") or {}).get("node_id") != "11.3"
    final, changes = a_share_guard(out["decision"], next_limit_up=None)
    assert final["order_type"] == "不下单" and [c["reason"] for c in changes] == ["A 股不能做空"]


def test_a_buy_at_the_range_bottom_or_on_a_breakout_test_is_left_alone():
    # the lower third is where PA buys in a range; after a breakout (§6.5 是) the buy is the breakout's test, no longer
    # inside the range — its old top can sit mid-way in the 40 bars
    from pa_agent.ai.decision_nodes import DecisionNodeEngine
    from pa_agent.ai.market_features import compute_simple_market_features

    from tests.pa_fixtures import synthetic_frame
    f = synthetic_frame(trend=0.002)
    m = compute_simple_market_features(f)
    w = m.range_high - m.range_low
    for entry, extra in ((round(m.range_low + w * 0.2, 2), []),
                         (round(m.range_low + w / 2, 2), [{"node_id": "6.5", "answer": "是", "reason": "存活突破后回踩"}])):
        out = {"decision": plan(entry_price=entry, stop_loss_price=round(entry * 0.97, 2), take_profit_price=round(entry * 1.03, 2),
                                take_profit_price_2=round(entry * 1.06, 2)),
               "decision_trace": [{"node_id": "6.1", "answer": "是", "reason": "边界清楚"}, *extra]}
        DecisionNodeEngine.apply_stage2(out, f, {})
        assert out["decision"]["order_type"] == "限价单" and (out.get("terminal") or {}).get("node_id") != "11.3"
