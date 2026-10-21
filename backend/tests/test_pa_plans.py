"""价格行为分析 (design 2026-10-20 §3, §5.1): the AI's own plan out of its accepted answer, the program's changes with
the upstream rule behind each, and the A-share guards (long only, the confidence gate, cents, the next day's limit-up)."""
from tests.pa_fixtures import STAGE1_BULLISH, plan, stage2
from wb.pa.plans import a_share_guard, ai_original, parse_reply, program_changes


def test_parse_reply_takes_fenced_and_control_char_json():
    assert parse_reply('```json\n{"a": 1}\n```') == {"a": 1} and parse_reply('{"a": "x\ny"}') == {"a": "x\ny"}


def test_ai_original_keeps_prices_but_honours_the_ais_own_no():
    s2 = stage2(plan(order_type="限价单", entry_price=9.5), terminal={"node_id": "10.3", "outcome": "wait", "label": "x"})
    assert ai_original(s2, STAGE1_BULLISH)["order_type"] == "不下单"          # its own verdict: no trade
    ok = stage2(plan(order_type="突破单", entry_price=10.123), terminal={"node_id": "11.2", "outcome": "trade", "label": "x"})
    assert ai_original(ok, STAGE1_BULLISH)["entry_price"] == 10.123             # no snapping here


def test_program_changes_name_each_upstream_rule():
    ai = plan(entry_price=10.0, stop_loss_price=9.6, take_profit_price=10.8)
    final = plan(entry_price=10.0, stop_loss_price=9.2, take_profit_price=10.8)
    assert program_changes(ai, final, stage2(final))[0]["reason"] == \
        "第一止盈离买入价 0.80 元，PA 规定止损也放这么远（第一止盈正好赚 1 倍风险）"   # in the stock's numbers (10-21)
    guarded = plan(order_type="不下单", reasoning="【程序连续性守卫】上一轮方案未失效；改为不下单。 x")
    c = program_changes(ai, guarded, stage2(guarded, terminal={"node_id": "continuity", "outcome": "wait", "label": "x"}))
    assert c[0]["reason"] == "前后衔接守卫：上一轮方案未失效"
    metrics = plan(order_type="不下单")
    trace = [{"node_id": "10.3", "answer": "否", "reason": "看过（程序按 decision 三价校验未通过：TP1 盈亏比 0.8 < 1.0，已改为不下单。）"}]
    c = program_changes(ai, metrics, stage2(metrics, terminal={"node_id": "10.3", "outcome": "reject", "label": "x"}, trace=trace))
    assert c == [{"field": "order_type", "before": "限价单", "after": "不下单", "reason": "盈亏比、胜率或价格顺序没过：TP1 盈亏比 0.8 < 1.0"}]
    # review of 1fc7dec (7): K1 having traded through the plan's prices is no ratio — said alone (pa.ts puts it in words)
    touch = "limit long: K1 low 6.59 already at/below stop 6.59; plan invalid — order_type=不下单"
    trace = [{"node_id": "10.3", "answer": "否", "reason": f"看过（程序按 decision 三价校验未通过：{touch}，已改为不下单。）"}]
    c = program_changes(ai, metrics, stage2(metrics, terminal={"node_id": "10.3", "outcome": "reject", "label": "x"}, trace=trace))
    assert c[0]["reason"] == touch
    brk = plan(order_type="突破单", entry_price=10.05, take_profit_price=10.8)
    trace = [{"node_id": "11.1", "answer": "否", "reason": "y"}, {"node_id": "11.2", "answer": "是", "reason": "cycle_position=normal_channel→突破单。"}]
    c = program_changes(ai, brk, stage2(brk, trace=trace))
    assert [x["reason"] for x in c] == ["按阶段定买法：cycle_position=normal_channel→突破单。", "突破买入价对齐到信号棒最高价加一跳"]
    # a 突破单 without the bar it breaks out of becomes a 限价单 (stage2_normalizer's breakout-without-basis rule): not
    # 「按阶段定买法」 (the comparison with upstream, 10-21)
    bare = plan(order_type="突破单", entry_price=10.05, entry_basis_bar=None, entry_basis_extreme=None)
    c = program_changes(bare, plan(order_type="限价单", entry_price=10.05), stage2(plan(order_type="限价单"), trace=trace))
    assert c == [{"field": "order_type", "before": "突破单", "after": "限价单", "reason": "突破单没写依据哪根 K 线，程序按规定改成限价单"}]


def test_a_share_guard():
    d, c = a_share_guard(plan(order_direction="做空"), next_limit_up=11.0)
    assert d["order_type"] == "不下单" and c[0]["reason"] == "A 股不能做空"
    d, c = a_share_guard(plan(trade_confidence=39), next_limit_up=11.0)
    assert d["order_type"] == "不下单" and "信心不到 40" in c[0]["reason"]
    d, _ = a_share_guard(plan(order_type="突破单", entry_price=10.001, stop_loss_price=9.504), next_limit_up=11.0)
    assert (d["entry_price"], d["stop_loss_price"]) == (10.01, 9.5)
    d, c = a_share_guard(plan(entry_price=11.2), next_limit_up=11.0)
    assert d["order_type"] == "不下单" and "11.00" in c[-1]["reason"] and d["entry_price"] is d["order_direction"] is None
    d, c = a_share_guard(plan(entry_price=10.07), next_limit_up=11.0)
    assert d["entry_price"] == 10.07 and c == []                                   # float noise is no change
    d, c = a_share_guard(plan(order_type="突破单", entry_price=8.05, stop_loss_price=7.6), next_limit_up=11.0)
    assert d["entry_price"] == 8.05 and c == []                                    # 8.05 × 100 is 805.0000000000001
