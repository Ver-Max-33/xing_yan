// 价格行为分析 (design 2026-10-20, dev branch): PA_Agent's two stages on a stock's last 100 daily bars — where it is now,
// where it may go, the AI's own plan and the one after the program's and the A-share checks — each analysis kept and
// settled by A-share rules beside a random control. The words here are what the panel says.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { PlanLine } from "@/chart/structures"
import type { Effort, Provider } from "./ai"
import { api } from "./api"
import { fmtPct } from "./format"

export type OrderType = "限价单" | "突破单" | "市价单" | "不下单"
export interface PAPlan {
  order_type: OrderType; order_direction: "做多" | "做空" | null
  entry_price: number | null; stop_loss_price: number | null; take_profit_price: number | null; take_profit_price_2: number | null
  trade_confidence?: number | null; estimated_win_rate?: number | null
  reasoning?: string; key_factors?: string[]; watch_points?: string[]; risk_assessment?: string; invalidation_condition?: string | null
}
export interface PAChange { field: string; before: unknown; after: unknown; reason: string }
export type TradeStatus = "no_plan" | "short" | "waiting" | "not_bought" | "void" | "holding" | "done" | "gone"
export interface PATrade {
  analysis_id: string; track: "ai" | "final"; target: "tp1" | "tp2"; status: TradeStatus
  entry_date: string | null; entry_price: number | null; exit_date: string | null; exit_price: number | null
  reason: "stop" | "tp1" | "tp2" | "days" | "gone" | null; days: number | null; ret: number | null; r: number | null
  ctrl_n: number | null; ctrl_bought: number | null; ctrl_win: number | null
  ctrl_ret_mean: number | null; ctrl_ret_median: number | null; ctrl_r_mean: number | null; final: number
}
export interface TraceNode {
  node_id: string; question?: string; answer: string; reason?: string; bar_range?: string; branch?: string | null
  overridden_by_ai?: boolean; program_answer?: string; override_reason?: string
}
export interface PAStage1 {
  cycle_position: string; direction: string; diagnosis_confidence?: number; gate_result?: string
  support_levels?: string[]; resistance_levels?: string[]; htf_context?: string; entry_setup?: string; key_signals?: string[]
  risk_warning?: string; gate_trace?: TraceNode[]; market_phase?: string; transition_risk?: string | null
}
export interface PAStage2 {
  decision: PAPlan; decision_trace?: TraceNode[]; terminal?: { node_id: string; outcome: string; label: string }
  next_cycle_prediction?: { cycle: string | null; direction: string | null; probabilities: Record<string, number> | null
    reasoning: string; unpredictable: boolean }
}
type Trades = { tp1: PATrade | null; tp2: PATrade | null }
export interface PAAnalysis {
  id: string; listing_id: string; day: string; profile: string; trigger: string; demo: boolean
  adjusted: number; intraday: number; first_of_day: number
  status: "running" | "ok" | "failed" | "cancelled"; stage: string | null; error: string | null
  model: string | null; cost: number | null; latency_ms: number | null; call_ids: string[]; prompt_hash: string
  input?: { bars: { date: string; open: number; high: number; low: number; close: number; volume: number; mark: string }[]
    k1_close: number; next_limit_up: number | null; next_limit_down: number | null }
  stage1?: PAStage1 | null; stage2?: PAStage2 | null              // not in the record of every analysis (/pa/recent)
  ai_plan: PAPlan | null; final_plan: PAPlan | null; changes: PAChange[] | null; previous_id: string | null
  created_at: string; finished_at: string | null; start_date: string | null
  fwd_outcome: "settled" | "gone" | "halt" | null; r5: number | null; x5: number | null; r20: number | null; x20: number | null
  settled: number; trades?: { ai: Trades; final: Trades }
}
export interface PAPreview {
  supported: boolean; day: string | null; model: string; ready: boolean; reason: string | null; adjusted: boolean; demo: boolean
}
export interface PATask { provider: Provider; model: string; effort: Effort }
export interface PAStatus { name: string; provider: Provider; model: string; effort: Effort; ready: boolean; reason: string | null }
/** One row of the evaluation, read per analysis: a control's numbers are each analysis's mean of its 20 draws, and
 *  bought_share is the share of its plans (a control's: of its draws) that got bought. */
export interface PlanStats {
  n: number; orders: number; bought: number | null; bought_share: number | null; settled: number
  win: number | null; ret_mean: number | null; ret_median: number | null; r_mean: number | null
}
type ByTarget = { tp1: PlanStats; tp2: PlanStats }
export interface PAPlans { ai: ByTarget; ai_control: ByTarget; final: ByTarget; final_control: ByTarget }
export interface PAGroup {
  key: string | null; n: number; settled: number
  x5_mean: number | null; x5_median: number | null; x20_mean: number | null; x20_median: number | null
}
export interface PAEvaluation {
  plans: PAPlans; groups: Record<"program" | "direction" | "cycle" | "next" | "buy", PAGroup[]>
  intraday: { yes: PAPlans; no: PAPlans }; enough: boolean
}

export const CYCLE_ZH: Record<string, string> = {
  spike: "尖峰", micro_channel: "微型通道", tight_channel: "窄通道", normal_channel: "常规通道", broad_channel: "宽通道",
  trending_tr: "趋势型区间", trading_range: "普通区间", extreme_tr: "极端区间", unknown: "看不清",
}
export const DIRECTION_ZH: Record<string, string> = { bullish: "偏多", bearish: "偏空", neutral: "中性" }
export const ORDER_ZH: Record<OrderType, string> = {
  限价单: "跌到买入价再买（挂限价）", 突破单: "涨过买入价再买（突破买）", 市价单: "下一个开盘就买", 不下单: "不买",
}
export const TRADE_ZH: Record<TradeStatus, string> = {
  no_plan: "不买", short: "做空方案（A 股不对账）", waiting: "等成交", not_bought: "没买到", void: "作废（成交前先碰到止损）",
  holding: "持有中", done: "已结算", gone: "已退市",
}
const REASON_ZH = { stop: "先到止损", tp1: "先到第一止盈", tp2: "先到第二止盈", days: "20 个交易日到期按收盘卖", gone: "已退市" } as const
/** The decision tree's nodes PA's program judges before the AI does (the AI may overrule them, with a reason). */
export const PROGRAM_NODES = new Set(["1.1", "1.3", "2.3", "2.4", "2.5", "6.3", "9.1", "9.2", "9.3", "9.5", "11.1", "11.2", "11.3", "11.4"])
const ORDERS: OrderType[] = ["限价单", "突破单", "市价单"]
const ALWAYS_IN: Record<string, string> = { AIL: "是，一边倒向上（总在多头一边）", AIS: "是，一边倒向下（总在空头一边）" }
const RISK_ZH: Record<string, string> = { high: "高", medium: "中", low: "低" }

/** §6.1 beside the stage: whether one side is in control — PA's §2.4 Always In, counted as its own code does (answered 是,
 *  branch AIL / AIS) — and whether the market is changing stage. */
export function stageFacts(s1: PAStage1): { label: string; text: string }[] {
  const n = s1.gate_trace?.find((x) => ["2.4", "2.4.0"].includes(x.node_id.replace("§", "")))
  const side = n && n.answer.trim() === "是" ? ALWAYS_IN[(n.branch ?? "").trim().toUpperCase()] : undefined
  const risk = s1.transition_risk ? `（转换风险${RISK_ZH[s1.transition_risk] ?? s1.transition_risk}）` : ""
  const phase = s1.market_phase === "transitioning" ? `在转换${risk}` : s1.market_phase === "stable" ? `没有，阶段稳定${risk}` : null
  return [{ label: "一边倒", text: side ?? "不是" }, ...(phase ? [{ label: "在转换", text: phase }] : [])]
}

export const isOrder = (p: PAPlan | null | undefined): boolean => !!p && ORDERS.includes(p.order_type)
/** The program said 不买 where the AI's own plan was to buy (a 做空 plan is no buy on A shares). */
export const aiWouldHaveBought = (a: PAAnalysis): boolean =>
  a.status === "ok" && !isOrder(a.final_plan) && isOrder(a.ai_plan) && a.ai_plan?.order_direction === "做多"

/** PA's own codes inside its Chinese sentences, said in the panel's words (10-21) — the stage and
 *  direction names, 一边倒 for Always In, the gate's 等待 / 继续, K 线类型, the swing marks, its strategy files without
 *  ".txt", its formulas' variables and the field names the AI copies. K1, A股 and T+1 read fine as they are; EMA and ATR
 *  stay, said once in brackets (10-21). */
const N = "(-?[\\d.e+-]+)"   // a number as PA's messages print it
const ev = (x: string) => `${Number(x) < 0 ? "亏" : "赚"} ${Number(Math.abs(Number(x)).toPrecision(2))}`
const VARS: Record<string, string> = { d: "斜率", thr: "阈值", diff: "差值", highs: "高点数", lows: "低点数", W: "窗口", range: "K线长度" }
const PHRASES: [RegExp, string | ((...m: string[]) => string)][] = [
  // PA's checks of a plan's numbers (util/trade_metrics.py), written in English: the same numbers, said plainly. Risk and
  // reward equal: PA caps the first target at 1 R and moves a nearer stop out to it — such a plan needs more than 50%
  [new RegExp(`decision prices: trader equation fails at ${N}% win rate \\(risk=${N}, reward=${N}, expectancy≈${N}\\);[^；。]*?unless prices are fixed`, "g"),
    (_, w, risk, reward, e) => Number(risk) === Number(reward)
      ? `程序规定第一止盈最多赚 1 倍风险，止损放到亏的和赚的一样多：止损亏 ${risk}、止盈赚 ${reward}；按 AI 估的胜率 ${w}% 算，` +
        `平均每股约${ev(e)}，不划算——这样胜率要过 50% 才划算`
      : `按 AI 估的胜率 ${w}% 算不划算：止盈赚 ${reward}、止损亏 ${risk}，平均每股约${ev(e)}`],
  // a ratio as PA prints it: 「0.75 : 1」 (compute_risk_reward's ratio_text), or 「0.8:1」
  [new RegExp(`decision prices: risk_reward ([\\d.]+) ?: ?1 is below minimum ([\\d.]+) ?: ?1 for this stance;[^；。]*?10\\.3=否`, "g"),
    "盈亏比 $1:1 低于要求的最低 $2:1"],
  [new RegExp(`decision prices: risk_reward ([\\d.]+) ?: ?1 exceeds maximum ([\\d.]+) ?: ?1 for TP1 after stop adjustment;[^；。]*?10\\.3=否`, "g"),
    "止损外挪后，第一止盈的盈亏比 $1:1 仍高于上限 $2:1"],
  // T+1 (10-21): trade_metrics moved the stop one ATR under the entry, and the target no longer pays for it
  [new RegExp(`decision prices: T\\+1 — stop moved to ${N}, one ATR under entry, makes risk_reward ([\\d.]+) ?: ?1, below minimum ([\\d.]+) ?: ?1;[^；。]*?10\\.3=否`, "g"),
    (_, stop, rr, min) => `A 股当天买了不能卖，止损至少离买入价 1 个 ATR，挪到 ${stop}；这样第一止盈只赚 ${rr} 倍风险，不到要求的 ${Number(min)} 倍`],
  // the reason analyses before 10-21 kept for PA's 1:1 stop move; new ones give the distance
  [/止损往外挪，让第一止盈正好是 1:1/g, "PA 规定第一止盈正好赚 1 倍风险，止损放到和第一止盈一样远"],
  [/decision\.estimated_win_rate: required integer 0–100 when placing an order/g, "没有给出胜率"],
  [/decision prices: entry\/stop\/target must form a valid long[^；。]*?positive risk and reward/g, "买入、止损、止盈的价格顺序不对，算不出盈亏"],
  [/decision\.take_profit_price_2: long plan requires stop < entry < take_profit_price < take_profit_price_2/g,
    "做多要求 止损 < 买入 < 第一止盈 < 第二止盈，这几个价格的顺序不对"],
  [/decision\.take_profit_price_2: required finite number when placing an order/g, "没有给出第二止盈"],
  [/decision\.take_profit_price_2: must be above take_profit_price for long geometry/g, "第二止盈要高于第一止盈"],
  [new RegExp(`limit long \\(planned\\): K1 close ${N} is below entry ${N}; reprice entry or 不下单`, "g"),
    "计划挂的买入价 $2 高过了最新收盘 $1，要重新定价"],
  [new RegExp(`limit long: K1 low ${N} already touched/below entry ${N}; pending buy limit is stale[^；。]*?不下单`, "g"),
    "最新一根的最低价 $1 已经碰到买入价 $2，这个挂单已经过时"],
  [new RegExp(`limit long: K1 close ${N} is below entry ${N}; do not keep a buy limit above market without repricing`, "g"),
    "最新收盘 $1 已经低于买入价 $2，买单不能挂在现价上方"],
  [new RegExp(`limit long: K1 low ${N} already at/below stop ${N}; plan invalid — order_type=不下单`, "g"),
    "最新一根的最低价 $1 已经碰到止损 $2，方案作废"],
  [/Always In Long\s*[（(]AIL[)）]/g, "一边倒向上"], [/Always In Short\s*[（(]AIS[)）]/g, "一边倒向下"],
  [/Always In Long/g, "一边倒向上"], [/Always In Short/g, "一边倒向下"], [/Always In/g, "一边倒"],
  [/([\u4e00-\u9fff]+)\.txt/g, "「$1」"], [/。{2,}/g, "。"], [/\bTP1\b/g, "第一止盈"], [/\bTP2\b/g, "第二止盈"],
  [/\b(d|thr|diff|highs|lows|W|range)=/g, (_, v) => `${VARS[v]}=`], [/\bN\/A\b/g, "无"],   // 2.1's vote: d=0.0230>thr=0.0119
  [/\b20gb\b/gi, "20GB（连续约 20 根 K 线没碰均线）"],   // PA's pattern name, as its own §2.4 defines it
]
const WORDS: Record<string, string> = {
  ...CYCLE_ZH, ...DIRECTION_ZH, AIL: "一边倒向上", AIS: "一边倒向下", wait: "等待", proceed: "继续", reject: "放弃",
  trend_bull: "阳线趋势棒", trend_bear: "阴线趋势棒", outside_bull: "外包阳线", outside_bear: "外包阴线",
  doji: "十字星", inside: "内包线", outside: "外包线", flat: "一字线",
  strengthens_bull: "加强多头", strengthens_bear: "加强空头", weakens_bull: "削弱多头", weakens_bear: "削弱空头",
  weakened_bull: "多头减弱", weakened_bear: "空头减弱", HH: "更高的高点", HL: "更高的低点", LH: "更低的高点", LL: "更低的低点",
  stable: "稳定", transitioning: "在转换", direction: "方向", cycle_position: "阶段", bar_type: "K线类型",
  gate_result: "闸门结论", spike_stage: "尖峰阶段", market_phase: "市场状态", transition_risk: "转换风险", always_in: "一边倒",
  failed: "失败", MM: "等距目标", TP: "止盈", SL: "止损",
  score: "得分", gate: "闸门", Barbwire: "铁丝网", weak: "弱", invalid: "无效", pending: "待定", NaN: "无效值", Brooks: "布鲁克斯",
  entry: "买入", stop: "止损", decision: "方案", KlineFrame: "K线表", node_overrides: "节点改判", order_direction: "下单方向",
  range_atr_ratio: "K线长度与ATR之比", follow_through_1_2: "后两根跟进", follow_through: "跟进",
  aligned: "一致", conflict: "冲突", mixed: "混合", neutral_background: "背景中性",   // 2.2: the long trend beside the near one
  close_breakout: "收盘突破（未确认站稳）", surviving: "站稳的突破", none: "无",          // 突破质量 in the program's warning
  quality: "质量", strong: "强", medium: "中等", climax_triggered: "高潮已触发", detected_patterns: "识别到的形态",
}
/** Brooks's and PA's own abbreviations: they stay, said once in brackets (10-21). */
const NOTE: Record<string, string> = { EMA: "指数均线", ATR: "平均波幅", H1: "第一次回调后的买点", H2: "第二次回调后的买点",
  L1: "第一次反弹后的卖点", L2: "第二次反弹后的卖点", RR: "盈亏比", SCS: "尖峰追单" }
const CJK = "[\\u3000-\\u303f\\u4e00-\\u9fff\\uff00-\\uffef]"   // Chinese and its full-width punctuation
export function plain(text: string): string {
  const t = PHRASES.reduce((x, [re, to]) => x.replace(re, to as (...m: string[]) => string), text)
  const noted = new Set<string>()
  return t.replace(/\b[A-Za-z_][A-Za-z0-9_]*\b/g, (w) => WORDS[w] ?? w)
    .replace(/\b(?:(EMA|ATR)(\d*)|H[12]|L[12]|RR|SCS)\b/g, (w, a?: string, n?: string) =>   // the first time in a sentence:
      noted.has(w) ? w : (noted.add(w), `${w}（${a ? `${n ? `${n} 日` : ""}${NOTE[a]}` : NOTE[w]}）`))   // EMA20（20 日指数均线）
    .replace(new RegExp(`(?<=${CJK}) +(?=${CJK})`, "g"), "")   // the spaces that stood around an English word
}

/** 怎样才买: stage 1's own buying condition, or null when it is a single word (「等待」「否」) — what would make it buy is then
 *  in 要盯的地方. */
export function buyCondition(s1: PAStage1): string | null {
  const t = s1.entry_setup?.trim()
  return t && t.length > 4 ? t : null
}

/** The final plan's lines for the chart: its prices × (the chart's close on day 0 ÷ the close the AI read), so they sit
 *  on the chart's own price basis; from the first open the plan may use to its sale (still open: to the latest bar),
 *  dashed until it is bought. With 不买 there is no plan: the nearest support and resistance it found, dashed. */
export function planLines(a: PAAnalysis, day0CloseOnChart: number | null): PlanLine[] {
  const p = a.final_plan
  const k = day0CloseOnChart != null && a.input?.k1_close ? day0CloseOnChart / a.input.k1_close : 1
  if (!p || !isOrder(p)) {   // 不买: the nearest support and resistance it found, over the last 20 bars it read
    const bars = a.input?.bars ?? []
    const from = bars.length ? bars[Math.max(0, bars.length - 20)].date : a.day
    const nearest = (levels?: string[]) => levels?.map(Number).find((x) => Number.isFinite(x) && x > 0)
    const levels: [PlanLine["kind"], number | undefined][] = [["support", nearest(a.stage1?.support_levels)],
      ["resistance", nearest(a.stage1?.resistance_levels)]]
    return levels.filter(([, v]) => v != null).map(([kind, v]) => ({ kind, price: v! * k, from, to: null, filled: false }))
  }
  const t = a.trades?.final.tp1
  const at = { from: a.start_date ?? a.day, to: t?.exit_date ?? null, filled: !!t?.entry_date }
  const prices: [PlanLine["kind"], number | null][] = [["entry", p.entry_price], ["stop", p.stop_loss_price],
    ["tp1", p.take_profit_price], ["tp2", p.take_profit_price_2]]
  return prices.filter(([, v]) => v != null).map(([kind, v]) => ({ kind, price: v! * k, ...at }))
}

/** What became of a plan, in a line. */
export function tradeText(t: PATrade | null | undefined): string {
  if (!t) return "待结算"
  if (t.status === "gone") return `已退市：按最后一个收盘价，${fmtPct(t.ret, 2)}（扣费）`
  if (t.status === "done") return `已结算：${t.reason ? REASON_ZH[t.reason] : "卖出"}，${fmtPct(t.ret, 2)}（扣费）`
  return TRADE_ZH[t.status]
}

/** The evaluation's plan table (§5.5): the plan the K 线 page shows first, the AI's own one after it for comparison, each
 *  with its random control under it (10-21). */
export function evaluationRows(plans: PAPlans, target: "tp1" | "tp2"): { label: string; row: PlanStats }[] {
  const rows: [string, ByTarget][] = [["程序检查后的方案", plans.final], ["它的随机对照", plans.final_control],
    ["AI 原方案（只用来对比）", plans.ai], ["它的随机对照", plans.ai_control]]
  return rows.map(([label, by]) => ({ label, row: by[target] }))
}

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) })
const q = (lid: string) => `listing_id=${encodeURIComponent(lid)}`
export const paEndpoints = {
  preview: (lid: string) => api<PAPreview>(`/pa/preview?${q(lid)}`),
  start: (lid: string) => post<{ id: string; reused: boolean }>("/pa", { listing_id: lid }),   // reused: the same input
  get: (id: string) => api<PAAnalysis>(`/pa/${id}`),
  history: (lid: string) => api<PAAnalysis[]>(`/pa?${q(lid)}`),
  cancel: (id: string) => post<{ ok: boolean }>(`/pa/${id}/cancel`, {}),
  recent: () => api<{ records: PAAnalysis[]; evaluation: PAEvaluation }>("/pa/recent"),
  settings: () => api<{ task: PATask; status: PAStatus }>("/pa/settings"),
  saveSettings: (t: PATask) => api<PATask>("/pa/settings", { method: "PUT", body: JSON.stringify(t) }),
}

const running = (a: PAAnalysis | undefined) => a?.status === "running"
export const usePAPreview = (lid: string) => useQuery({ queryKey: ["pa", "preview", lid], queryFn: () => paEndpoints.preview(lid) })
export const usePA = (id: string | null) => useQuery({ queryKey: ["pa", "one", id], queryFn: () => paEndpoints.get(id!), enabled: !!id,
  refetchInterval: (query) => (running(query.state.data) ? 2000 : false) })
/** A stock's analyses, newest first; asked again every 2 s while one runs (its stage, then its result). */
export const usePAHistory = (lid: string) => useQuery({ queryKey: ["pa", "history", lid], queryFn: () => paEndpoints.history(lid),
  refetchInterval: (query) => (query.state.data?.some(running) ? 2000 : false) })
export const usePARecent = () => useQuery({ queryKey: ["pa", "recent"], queryFn: paEndpoints.recent })
export const usePASettings = () => useQuery({ queryKey: ["pa", "settings"], queryFn: paEndpoints.settings })

export function useStartPA() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: paEndpoints.start,
    onSuccess: (_r, lid) => {
      void qc.invalidateQueries({ queryKey: ["pa", "history", lid] })
      void qc.invalidateQueries({ queryKey: ["ai"] })
    },
  })
}

export function useSavePASettings() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: paEndpoints.saveSettings, onSuccess: () => void qc.invalidateQueries({ queryKey: ["pa"] }) })
}
