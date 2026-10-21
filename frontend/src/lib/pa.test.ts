import { describe, expect, it } from "vitest"
import { aiWouldHaveBought, buyCondition, CYCLE_ZH, plain, evaluationRows, ORDER_ZH, type PAAnalysis, type PAPlan, type PAPlans, type PAStage1, type PATrade, planLines, type PlanStats, stageFacts, tradeText } from "./pa"

const LIMIT_950: PAPlan = { order_type: "限价单", order_direction: "做多", entry_price: 9.5, stop_loss_price: 9.0,
  take_profit_price: 10.0, take_profit_price_2: 10.5 }
const NO_BUY: PAPlan = { order_type: "不下单", order_direction: null, entry_price: null, stop_loss_price: null,
  take_profit_price: null, take_profit_price_2: null }

const trade = (over: Partial<PATrade> = {}): PATrade => ({
  analysis_id: "a1", track: "final", target: "tp1", status: "waiting", entry_date: null, entry_price: null, exit_date: null,
  exit_price: null, reason: null, days: null, ret: null, r: null, ctrl_n: null, ctrl_bought: null, ctrl_win: null,
  ctrl_ret_mean: null, ctrl_ret_median: null, ctrl_r_mean: null, final: 0, ...over,
})
const soldAt = (d: string) => ({ ai: { tp1: null, tp2: null },
  final: { tp1: trade({ status: "done", entry_date: "2026-10-01", exit_date: d, reason: "tp1", ret: 0.05 }), tp2: null } })

function analysis({ k1_close = 10, final_plan = LIMIT_950, trades }: { k1_close?: number; final_plan?: PAPlan; trades?: PAAnalysis["trades"] }): PAAnalysis {
  return {
    id: "a1", listing_id: "CN:600000", day: "2026-09-30", profile: "prod", trigger: "manual", demo: false, adjusted: 1,
    intraday: 0, first_of_day: 1, status: "ok", stage: null, error: null, model: "deepseek-v4-pro", cost: 0.6, latency_ms: 90_000,
    call_ids: ["c1", "c2"], prompt_hash: "h", input: { bars: [], k1_close, next_limit_up: 11, next_limit_down: 9 },
    stage1: null, stage2: null, ai_plan: final_plan, final_plan, changes: [], previous_id: null,
    created_at: "2026-09-30T20:00:00+08:00", finished_at: null, start_date: "2026-10-01", fwd_outcome: null,
    r5: null, x5: null, r20: null, x20: null, settled: 0, trades,
  }
}

describe("价格行为分析", () => {
  it("maps every stage and order to plain words", () => {
    expect(CYCLE_ZH.trending_tr).toBe("趋势型区间")
    expect(ORDER_ZH["不下单"]).toBe("不买")
    expect(Object.keys(CYCLE_ZH)).toHaveLength(9)
  })

  it("puts the plan's lines on the chart's basis and ends them at the sale", () => {
    const lines = planLines(analysis({ k1_close: 10, final_plan: LIMIT_950, trades: soldAt("2026-10-05") }), 12.5)
    expect(lines.find((l) => l.kind === "entry")).toEqual({ kind: "entry", price: 11.875, from: "2026-10-01", to: "2026-10-05", filled: true })
    expect(lines.map((l) => l.kind)).toEqual(["entry", "stop", "tp1", "tp2"])
    expect(planLines(analysis({ final_plan: LIMIT_950 }), null)[0]).toEqual(
      { kind: "entry", price: 9.5, from: "2026-10-01", to: null, filled: false })        // no close on the chart: its own prices
    expect(planLines(analysis({ final_plan: NO_BUY }), 10)).toEqual([])
  })

  it("says what happened to a plan", () => {
    expect(tradeText(trade({ status: "done", reason: "tp1", ret: 0.032 }))).toBe("已结算：先到第一止盈，+3.20%（扣费）")
    expect(tradeText(trade({ status: "void" }))).toBe("作废（成交前先碰到止损）")
    expect(tradeText(trade({ status: "gone", reason: "gone", ret: -0.05 }))).toBe("已退市：按最后一个收盘价，-5.00%（扣费）")
    expect(tradeText(null)).toBe("待结算")
  })

  it("lays the evaluation rows out with the plan that is placed first, each control after its plan", () => {
    // 10-21: the checked plan first, the AI's own one named for comparison
    const st = (n: number): PlanStats => ({ n, orders: n, bought: 0, bought_share: 0, settled: 0, win: null, ret_mean: null,
      ret_median: null, r_mean: null })
    const by = (n: number) => ({ tp1: st(n), tp2: st(n + 100) })
    const plans: PAPlans = { ai: by(1), ai_control: by(2), final: by(3), final_control: by(4) }
    expect(evaluationRows(plans, "tp1").map((r) => [r.label, r.row.n])).toEqual(
      [["程序检查后的方案", 3], ["它的随机对照", 4], ["AI 原方案（只用来对比）", 1], ["它的随机对照", 2]])
    expect(evaluationRows(plans, "tp2")[0].row.n).toBe(103)
  })

  it("says the stop the program moved in the words new analyses use", () => {     // records kept before 10-21's wording
    expect(plain("止损往外挪，让第一止盈正好是 1:1")).toBe("PA 规定第一止盈正好赚 1 倍风险，止损放到和第一止盈一样远")
  })

  it("says whether one side is in control and whether the stage is changing (§6.1)", () => {   // final review, Important 4
    const s1 = (over: Partial<PAStage1>): PAStage1 => ({ cycle_position: "normal_channel", direction: "bullish", ...over })
    expect(stageFacts(s1({ gate_trace: [{ node_id: "2.4", answer: "是", branch: "AIL" }], market_phase: "transitioning",
      transition_risk: "high" }))).toEqual([{ label: "一边倒", text: "是，一边倒向上（总在多头一边）" },
      { label: "在转换", text: "在转换（转换风险高）" }])
    expect(stageFacts(s1({ gate_trace: [{ node_id: "2.4", answer: "否", branch: "AIS" }], market_phase: "stable" }))).toEqual(
      [{ label: "一边倒", text: "不是" }, { label: "在转换", text: "没有，阶段稳定" }])               // PA counts only an answered 是
  })

  it("says the AI would have bought only when its own plan was a long order", () => {      // final review (Minor 10, promoted)
    expect(aiWouldHaveBought({ ...analysis({ final_plan: NO_BUY }), ai_plan: LIMIT_950 })).toBe(true)
    expect(aiWouldHaveBought({ ...analysis({ final_plan: NO_BUY }), ai_plan: { ...LIMIT_950, order_direction: "做空" } })).toBe(false)
    expect(aiWouldHaveBought(analysis({ final_plan: LIMIT_950 }))).toBe(false)                    // the final plan buys anyway
  })

  it("shows 怎样才买 only when the model wrote a condition, not one word", () => {
    const s1 = (entry_setup?: string): PAStage1 => ({ cycle_position: "trading_range", direction: "neutral", entry_setup })
    expect(buyCondition(s1("等待"))).toBeNull()
    expect(buyCondition(s1(" 否 "))).toBeNull()
    expect(buyCondition(s1())).toBeNull()
    expect(buyCondition(s1("回踩 15.12 附近出现阳线信号棒后挂限价"))).toBe("回踩 15.12 附近出现阳线信号棒后挂限价")
  })

  it("draws the nearest support and resistance when it says 不买, on the chart's basis, over the last 20 bars", () => {
    const bars = Array.from({ length: 100 }, (_, i) => ({ date: `d${String(i).padStart(3, "0")}`, open: 4, high: 4.1, low: 4, close: 4.08,
      volume: 1, mark: "" }))
    const a: PAAnalysis = { ...analysis({ k1_close: 4.08, final_plan: NO_BUY }), input: { bars, k1_close: 4.08, next_limit_up: 4.49,
      next_limit_down: 3.67 }, stage1: { cycle_position: "trending_tr", direction: "bearish", support_levels: ["4.06", "4.05"],
      resistance_levels: ["区间上沿", "4.15", "4.24"] } }
    expect(planLines(a, 8.16)).toEqual([
      { kind: "support", price: 8.12, from: "d080", to: null, filled: false },             // ×2: the chart is 后复权 here
      { kind: "resistance", price: 8.3, from: "d080", to: null, filled: false }])          // the first level that is a price
    expect(planLines({ ...a, stage1: { cycle_position: "trending_tr", direction: "bearish" } }, null)).toEqual([])
  })

  it("says PA's own codes in plain Chinese and leaves what reads fine", () => {     // 10-21: mixed Chinese and English read badly
    expect(plain("阶段一闸门结论为「wait」，在节点 2.5 停止：结构不清，闸门不通过，等待。。未进入阶段二策略分支评估。"))
      .toBe("阶段一闸门结论为「等待」，在节点 2.5 停止：结构不清，闸门不通过，等待。未进入阶段二策略分支评估。")
    expect(plain("当前周期 normal_channel，方向 bullish。")).toBe("当前周期常规通道，方向偏多。")   // no space left between Chinese words
    expect(plain("判定为Always In Long（AIL）（结构弱）；阶段一方向为看空且处于AIS")).toBe("判定为一边倒向上（结构弱）；阶段一方向为看空且处于一边倒向下")
    expect(plain("direction=neutral 且 §2.4 非 Always In：禁止无强信号的方向性方案")).toBe("方向=中性且 §2.4 非一边倒：禁止无强信号的方向性方案")
    expect(plain("K2 bar_type=outside_bear，做多强信号棒类型=['trend_bull']")).toBe("K2 K线类型=外包阴线，做多强信号棒类型=['阳线趋势棒']")
    expect(plain("LH 与 LL 结构，参考 下跌通道分析识别.txt")).toBe("更低的高点与更低的低点结构，参考「下跌通道分析识别」")
    expect(plain("K1 收在 EMA20 下方；A股 T+1，当日破位只能次日处理")).toBe("K1 收在 EMA20（20 日指数均线）下方；A股 T+1，当日破位只能次日处理")
    expect(plain("TP1 11.99、TP2 12.26，SL 放在 11.54")).toBe("第一止盈 11.99、第二止盈 12.26，止损放在 11.54")
  })

  it("says PA's program messages about a plan's numbers in Chinese (三全食品, 10-21)", () => {
    // PA caps the first target at 1 R and moves a nearer stop out to it: a 1:1 plan needs more than 50% to be worth it
    expect(plain("盈亏比、胜率或价格顺序没过：decision prices: trader equation fails at 48% win rate (risk=0.28, reward=0.28, " +
      "expectancy≈-0.0112); 10.3 must be 否 and order_type=不下单 unless prices are fixed"))
      .toBe("盈亏比、胜率或价格顺序没过：程序规定第一止盈最多赚 1 倍风险，止损放到亏的和赚的一样多：止损亏 0.28、止盈赚 0.28；" +
        "按 AI 估的胜率 48% 算，平均每股约亏 0.011，不划算——这样胜率要过 50% 才划算")
    expect(plain("decision prices: trader equation fails at 40% win rate (risk=0.3, reward=0.2, expectancy≈-0.1); 10.3 must be 否 " +
      "and order_type=不下单 unless prices are fixed")).toBe("按 AI 估的胜率 40% 算不划算：止盈赚 0.2、止损亏 0.3，平均每股约亏 0.1")
    expect(plain("limit long: K1 low 9.8 already touched/below entry 9.85; pending buy limit is stale — use 市价单, reprice, or 不下单"))
      .toBe("最新一根的最低价 9.8 已经碰到买入价 9.85，这个挂单已经过时")
    expect(plain("decision.take_profit_price_2: long plan requires stop < entry < take_profit_price < take_profit_price_2"))
      .toBe("做多要求止损 < 买入 < 第一止盈 < 第二止盈，这几个价格的顺序不对")
    expect(plain("decision prices: risk_reward 0.8:1 is below minimum 1.00:1 for this stance; adjust take_profit/stop_loss or " +
      "set order_type=不下单 with 10.3=否")).toBe("盈亏比 0.8:1 低于要求的最低 1.00:1")
    // PA prints its ratio as 「0.75 : 1」 (compute_risk_reward's ratio_text): the phrase has to read that too
    expect(plain("decision prices: risk_reward 0.75 : 1 is below minimum 1.00:1 for this stance; adjust take_profit/stop_loss " +
      "or set order_type=不下单 with 10.3=否")).toBe("盈亏比 0.75:1 低于要求的最低 1.00:1")
    // T+1 (10-21): the stop one ATR under the entry, and the target no longer pays for it
    expect(plain("decision prices: T+1 — stop moved to 8.94, one ATR under entry, makes risk_reward 0.75 : 1, below minimum " +
      "1.00:1; set order_type=不下单 with 10.3=否"))
      .toBe("A 股当天买了不能卖，止损至少离买入价 1 个 ATR（平均波幅），挪到 8.94；这样第一止盈只赚 0.75 倍风险，不到要求的 1 倍")
  })

  it("says the decision path's formulas and copied field names in Chinese, EMA / ATR kept with a note (10-21)", () => {
    // PA's program (2.1's five-signal vote) and what the AI wrote in six real results: 300463, 002216, 002398 …
    expect(plain("五信号投票（阈值±3）：EMA斜率:+1(d=0.0230>thr=0.0119)；收盘重心(加权):-1(diff=-0.2623<-thr=-0.0238)；" +
      "波段结构:0(枢轴不足,highs=0,lows=0)；中窗口重心(加权):0(diff=0.0084,W=20)。综合score=0（≥+3→多头，≤-3→空头，否则中性）"))
      .toBe("五信号投票（阈值±3）：EMA（指数均线）斜率:+1(斜率=0.0230>阈值=0.0119)；收盘重心(加权):-1(差值=-0.2623<-阈值=-0.0238)；" +
        "波段结构:0(枢轴不足,高点数=0,低点数=0)；中窗口重心(加权):0(差值=0.0084,窗口=20)。综合得分=0（≥+3→多头，≤-3→空头，否则中性）")
    expect(plain("加权收盘高于EMA占比75.0%≥65%；EMA斜率向上；最大价格区间0.70×ATR(≤1.5×阈值,浅回撤✓)"))   // a note once per sentence
      .toBe("加权收盘高于EMA（指数均线）占比75.0%≥65%；EMA斜率向上；最大价格区间0.70×ATR（平均波幅）(≤1.5×阈值,浅回撤✓)")
    expect(plain("EMA20 与 ATR14 全为 NaN，重叠N/A")).toBe("EMA20（20 日指数均线）与 ATR14（14 日平均波幅）全为无效值，重叠无")
    expect(plain("K1 range_atr_ratio=0.900 ≤ 2.0，信号棒长度在可接受范围内")).toBe("K1 K线长度与ATR（平均波幅）之比=0.900 ≤ 2.0，信号棒长度在可接受范围内")
    expect(plain("K1 range_atr_ratio无法计算（ATR预热不足或range=0）")).toBe("K1 K线长度与ATR（平均波幅）之比无法计算（ATR预热不足或K线长度=0）")
    expect(plain("K1为已收盘K线（KlineFrame内所有K线均已收盘），可作为信号棒。")).toBe("K1为已收盘K线（K线表内所有K线均已收盘），可作为信号棒。")
    expect(plain("无交易计划方向（order_direction缺失），§9.2不适用。")).toBe("无交易计划方向（下单方向缺失），§9.2不适用。")
    expect(plain("K1的follow_through_1_2='pending'→等待。")).toBe("K1的后两根跟进='待定'→等待。")
    expect(plain("做多：entry=11.71, stop=11.54, TP1=11.99。")).toBe("做多：买入=11.71, 止损=11.54, 第一止盈=11.99。")
    expect(plain("（程序校正：计划型限价单，接受 weak/invalid 或无信号棒）（程序按 decision 三价校验未通过）"))
      .toBe("（程序校正：计划型限价单，接受弱/无效或无信号棒）（程序按方案三价校验未通过）")
    expect(plain("是否出现Barbwire / 极窄区间？")).toBe("是否出现铁丝网 / 极窄区间？")
    expect(plain("按Brooks并列原则；但§2.5否不触发gate=wait，可在 node_overrides 中提交")).toBe("按布鲁克斯并列原则；但§2.5否不触发闸门=等待，可在节点改判中提交")
    expect(plain("W底成形，d 值未知")).toBe("W底成形，d 值未知")   // a lone letter is left alone: only d= / W= are PA's own
    expect(plain("长程K100-K41偏多与近期K40-K1中性关系mixed")).toBe("长程K100-K41偏多与近期K40-K1中性关系混合")   // 603236, 10-21
    expect(plain("K1为doji长上影；K2为inside，K3为flat，K4为outside")).toBe("K1为十字星长上影；K2为内包线，K3为一字线，K4为外包线")   // 605058
    // the re-run of 12 (10-21): Brooks's own abbreviations kept with a note, the rest said in Chinese
    expect(plain("出现H1或H2做多信号；未使用H2/L2二次入场，RR=1.0，未使用SCS"))
      .toBe("出现H1（第一次回调后的买点）或H2（第二次回调后的买点）做多信号；未使用H2/L2（第二次反弹后的卖点）二次入场，" +
        "RR（盈亏比）=1.0，未使用SCS（尖峰追单）")
    expect(plain("K1阳线收于高位，quality为medium；观察detected_patterns中的20gb与climax_triggered风险"))
      .toBe("K1阳线收于高位，质量为中等；观察识别到的形态中的20GB（连续约 20 根 K 线没碰均线）与高潮已触发风险")
    // the program's own warning (folded in the path): its breakout quality, said as PA's own labels say it
    expect(plain("；突破质量=failed：不宜把区间高度 MM 当主目标；突破质量=close_breakout：不宜追"))
      .toBe("；突破质量=失败：不宜把区间高度等距目标当主目标；突破质量=收盘突破（未确认站稳）：不宜追")
  })
})
