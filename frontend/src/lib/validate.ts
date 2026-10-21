// 验证 (spec §9): an event study, a condition check-up or one condition on its own, run in the background one at a
// time and kept with the rule version, the data profiles and the 样本内 / 样本外 split they were computed on.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { toast } from "sonner"
import type { StandIn } from "./ai"
import { api, type Market, type Profile } from "./api"
import type { Calibration } from "./rules"
import { codeOf, fmtPct } from "./format"
import { toCSV } from "./jobs"

export type ValidateKind = "event" | "checkup" | "single" | "backtest" | "robust" | "calibrate"   // 校准 runs in the same queue
export interface Stats { signals: number; effective: number; win: number | null; median: number | null; mean?: number | null }
/** The next day's opening against the signal day's close, by sample, bought or not (the 次日容易低开 claim put to the data). */
export interface Gap { median: number | null; down: number | null; n: number }
export interface Split { oos_start?: string; in: Stats; out: Stats }
export type Group = Stats & { key: string | number }
/** 规则自己的判断 (spec §9.1): its 失效线, 目标位 and days, and how its bought signals ended. */
export interface Judged {
  fail: string | null; target: string | null; days: number; n: number
  exits: Record<"target" | "fail" | "time" | "gone", number | null>; held: number | null
}
// ---- 交易计划 (design 2026-10-09 §3, §6): how a rule's signals are traded, and what that earns per signal day ----
export interface PlanLine { line: string; label: string; share: number }
export interface PlanSpec {
  entry: "close" | "open" | "cross"; stop: PlanLine[]; take: PlanLine[]; exit: { when: string; label: string; share: number }[]
  stall: number | null; days: number; fill: "close" | "open" | null; max_gap?: number | null; confirm_volume?: number | null
}
type Interval = [number | null, number | null]
/** One part of a plan's report (the whole, a half, a year, a group): signal days are its samples. */
export interface PlanPart {
  days: number; expectancy: number | null; ci: Interval; vs_control: number | null; vs_control_ci: Interval
  control_days: number; no_control_days: number; few: boolean
  se?: number | null; excess?: number | null; signals?: number; bought?: number; finished?: number; open?: number; gone?: number
  blocked?: { limit: number; halt: number }; no_room?: number; win?: number | null; avg_win?: number | null; avg_loss?: number | null
  payoff?: number | null; pf?: number | null; held?: number | null; per_day?: number | null; mfe?: number | null; mae?: number | null
  reasons?: Record<string, number>
}
export interface PlanReport extends PlanPart {
  control_expectancy: number | null; per_day_control: number; plan: PlanSpec; labels: Record<string, string>
  years: { year: number; days: number; finished: number; expectancy: number | null; win: number | null; pf: number | null; vs_control: number | null }[]
  split: { oos_start: string; in: PlanPart; out: PlanPart }
  groups: { state?: (PlanPart & { key: string })[]; industry?: (PlanPart & { key: string })[] }
  worst?: { market: Market; listing_id: string; name: string | null; signal_date: string; entry_date: string; exit_date: string; ret: number
    excess: number | null; reason: string; held: number }[]
  cross?: PlanCross | null   // a plan buying by cross (2026-10-11)
  entries?: { compared: number; rows: PlanEntry[] }   // 买法对比 (2026-10-11)
}
/** One way of buying the same rule, with its own sales (design 2026-10-11 §10). */
export interface PlanEntry extends Partial<PlanPart> {
  entry: "cross" | "close" | "open"; label: string; days: number; signals?: number; bought?: number; held?: number | null
  win?: number | null; plan?: PlanSpec
}
/** How a cross plan's crosses went (design 2026-10-11 §10): shares of the crosses, and 真突破 / 假突破 as groups. */
export interface PlanCross {
  crosses: number; per_day: number; at_open: number; closed_above: number; signal: number; kinds: (PlanPart & { key: string })[]
}

export interface Summary extends Stats {
  metric?: "judge" | "hold" | "plan"; judge?: Judged | null   // runs before 1.40: the 20-day excess; from 2026-10-11 the plan
  plan?: PlanReport | null
  blocked: { limit: number; halt: number }; gone: number; q25: number | null; q75: number | null
  mfe: number | null; mae: number | null; few: boolean; gap?: Gap // runs before 1.24 have none
  horizons: { h: number; median: number | null; q25: number | null; q75: number | null; n: number }[]
  hist: { step: number; lo: number; counts: number[] }
  split: Split & { oos_start: string }
  groups: Record<"market" | "year" | "industry" | "state", Group[]>
  worst: { market: Market; listing_id: string; name: string | null; signal_date: string; entry_date: string; r20: number; x20: number; mae: number | null
    exit?: "target" | "fail" | "time" | "gone"; held?: number }[]
}
export interface SplitStats {
  signals: number; effective_in: number; effective_out: number
  win_in: number | null; win_out: number | null; median_in?: number | null; median_out?: number | null
  mean_in?: number | null; mean_out?: number | null   // runs before 1.40 have none
  se_in?: number | null; se_out?: number | null       // by the plan (2026-10-11): the expectancy's standard error
}
export type Verdict = "有效" | "多余" | "有害" | "不一致" | "样本太少"
export interface Ablation {
  condition: string; stage: string; text: string; signals: number
  d_win_in: number | null; d_win_out: number | null; d_median_out?: number | null; verdict: Verdict
  d_mean_in?: number | null; d_mean_out?: number | null   // by the plan (2026-10-11)
}
export interface Scan { param: string; current: number; points: (SplitStats & { value: number; few: boolean })[]; plateau: [number, number] | null }
export interface Checkup {
  base: SplitStats; ablation: Ablation[]; scans: Scan[]; compared: number; oos_start: string
  metric?: "judge" | "hold" | "plan"; judge?: { fail: string | null; target: string | null; days: number } | null
  plan?: PlanSpec   // by the plan (2026-10-11)
  attribution?: Attribution // runs before 1.24 have none
}
// ---- 候选条件归因 (spec §9.5): each candidate feature read on the signal day, cut at its in-sample quintiles ----
export interface AttrGroup { group: number; lo: number | null; hi: number | null; in: Stats; out: Stats }
export interface AttrFeature {
  id: string; label: string; expr: string; market: Market // each market grouped on its own
  verdict: "单调" | "不单调" | "样本太少"; direction: "up" | "down" | null; cuts: number[] | null; groups: AttrGroup[]
}
export interface AttrSuggestion {
  feature: string; label: string; op: ">" | "<="; threshold: number; condition: string; before: SplitStats; after: SplitStats
}
export interface Attribution {
  features: AttrFeature[]; suggestions: AttrSuggestion[]; stage: string | null // the signal stage a filter goes into
  skipped: { id: string; label: string; market: Market; why: string }[]       // features a market cannot give or never knows
  markets?: Market[] // suggestions only when there is one
}

/** A feature's groups, lowest values first: each one's 20-day excess median in one half (by the plan: its expectancy). */
export const groupMedians = (f: AttrFeature, half: "in" | "out", by: "median" | "mean" = "median") =>
  f.groups.map((g) => fmtPct(g[half][by] ?? null, 1)).join(" · ")

/** Adopting a suggestion (§6.4: an edit in the editor until you save it): its condition in the signal stage's `at`,
 *  named after its feature. */
export const adoptAdd = (a: Attribution, s: AttrSuggestion): StandIn =>
  ({ label: s.label, stage: a.stage!, where: "at", expr: s.condition, id: `f_${s.feature}` })
export interface Single {
  expr: string; hold: number
  events: Stats & { split: Split; few: boolean; gap?: Gap }; random: Stats & { split: Split; few: boolean; gap?: Gap }
}
// ---- 组合回测 and 稳健性 (spec §9.2, §9.3) ----
export interface Costs {
  commission: number; min_commission: number; transfer: number; stamp: number; slippage: number
  history?: [string, Rate, number][]   // (until, rate, value): what a rate was on the days before `until` (CN 印花税, 过户费)
}
export type Rate = Exclude<keyof Costs, "history">
export interface BookSettings {
  cash: number; costs: Costs; max_positions: number; hold: number | null; box_stop: boolean
  trail: number | null; target: number | null; lot: number; t_plus_1: boolean
  stall?: number | null; weak_positions?: number | null // the research exits (runs before 1.24 have none)
  own?: boolean // the rule's own 失效线, 目标位 and days in place of hold, box_stop and target (runs before 1.41 have none)
}
export interface Part {
  total: number | null; cagr: number | null; max_drawdown: number | null; drawdown_end?: string | null
  vol: number | null; sharpe: number | null; days: number
  trades: number; open: number; win: number | null; avg_return: number | null; avg_days: number | null; reasons: Record<string, number>
}
export interface Counts {
  blocked_buy: Record<string, number>; blocked_sell: Record<string, number>; skipped: Record<string, number>
  same_bar: number; gap: number; corporate: number; unexplained?: number // runs before 1.24 have none
}
export interface BookReport {
  stats: Part & { exposure: number | null; fees: number; slippage: number; dividends: number; end_nav: number }
  split: { oos_start: string; in: Part; out: Part }
  benchmark: { total: number | null; excess: number | null }
  curve: [string, number, number | null][] // date, NAV ÷ the starting cash, the benchmark ÷ its first close
  counts: Counts; notes: string[]; benchmark_id: string; settings: BookSettings; signals: number
}
export interface Brief {
  total: number | null; cagr: number | null; max_drawdown: number | null; sharpe: number | null; trades: number
  in_total: number | null; out_total: number | null; out_sharpe: number | null; out_trades: number
}
export interface Robust {
  axes: { param: string; values: (number | boolean)[]; current: number | boolean }[]
  cells: ({ at: (number | boolean)[] } & Partial<Record<Market, Brief>>)[]
  costs: ({ k: number } & Partial<Record<Market, Brief>>)[]
  compared: number; notes: string[]
}
export interface TradeRow {
  market: Market; listing_id: string; name: string | null; signal_date: string; entry_date: string; entry_price: number; shares: number
  exit_date: string | null; exit_price: number | null; reason: string | null; pnl: number; ret: number | null; days: number | null
}
export interface LedgerRow {
  market: Market; date: string; listing_id: string; name: string | null; kind: string; shares: number | null
  price: number | null; amount: number; note: string | null
}
export interface NavRow { market: Market; date: string; cash: number; holdings: number; nav: number; positions: number }

export const REASON: Record<string, string> = {
  hold: "持有到期", box: "跌破箱底", fail: "跌破失效线", trail: "移动止损", target: "止盈", stall: "不创新高",
}
export const MOVE: Record<string, string> = {
  buy: "买入", sell: "卖出", fee: "费用", slippage: "滑点", dividend: "分红", split: "送转拆股", unexplained: "因子变化没解释",
}

export interface ValidateRun {
  id: string; kind: ValidateKind; rule_id: string | null; version: number | null; expr: string | null
  markets: Market[]; profiles: Partial<Record<Market, Profile>>; start: string; end: string; oos_start: string
  settings?: Partial<Record<Market, BookSettings>> | null
  // paused: it stopped after a chunk and keeps what it finished (继续); cancelled: given up, nothing kept
  status: "queued" | "running" | "paused" | "cancelled" | "done" | "error"; progress: string | null; error: string | null
  asked?: "pause" | "cancel" | null   // asked for, waiting for the run's next step
  created_at: string; finished_at: string | null
  result?: { summary: Summary } | Checkup | Single | { books: Partial<Record<Market, BookReport>> } | Robust | Calibration | null
}
export interface Setup {
  markets: { market: Market; profile: Profile; benchmark: string; first: string | null; last: string | null }[]
  start: string | null; end: string | null; oos_start: string | null
  books: Partial<Record<Market, BookSettings>> // each market's book by default
  checks?: CourseCheck[] // the stock-picking claims as single-condition tests
}
/** A stock-picking claim put to your data as a single-condition test: its formula and what to look at in the result. */
export interface CourseCheck { id: string; label: string; expr: string; look: string }
/** What a 组合回测 or 稳健性 run asks for: the exits and positions, and each market's costs and cash. */
export interface BookBody {
  book: {
    max_positions: number; hold: number | null; box_stop: boolean; trail: number | null; target: number | null
    stall?: number | null; weak_positions?: number | null // a form kept from before 1.24 has neither
    own?: boolean // a form kept from before 1.41 has none: on
  }
  costs: Partial<Record<Market, Costs>>; cash: Partial<Record<Market, number>>; axes?: string[]
}
/** The book until you change it: ten places, each rule's own judgement (else twenty days and the box's bottom); each
 *  market's own cash and costs. */
export const DEFAULT_BOOK: BookBody = {
  book: { max_positions: 10, hold: 20, box_stop: true, trail: null, target: null, stall: null, weak_positions: null, own: true },
  costs: {}, cash: {},
}
const UNIT: Record<Market, string> = { CN: "元", JP: "円" }
const share = (v: number) => `${+(v * 100).toFixed(4)}%`

/** A book's settings in words — the places, the exits, the cash and the costs a result was computed with. */
export function settingsText(s: BookSettings, market: Market): string {
  const hold = s.hold != null && `持有 ${s.hold} 天`, box = s.box_stop && "跌破箱底", target = s.target != null && `止盈 ${share(s.target)}`
  const trail = s.trail != null && `移动止损 ${share(s.trail)}`, stall = s.stall != null && `${s.stall} 天不创新高`
  const theirs = [hold, box, target].filter(Boolean)   // what the rule's own judgement replaces; a rule without one uses them
  const exits = (s.own ? [`按规则自己的失效线、目标位和天数${theirs.length ? `（规则没写时：${theirs.join("、")}）` : ""}`, trail, stall]
    : [hold, box, trail, target, stall]).filter(Boolean)
  const c = s.costs
  const dated = (k: Rate, text: string) => {   // what the rate was before it last changed
    const was = (c.history ?? []).filter(([, r]) => r === k).map(([until, , v]) => `${until} 前 ${share(v)}`)
    return was.length ? `${text}（${was.join("，")}）` : text
  }
  const costs = [`佣金 ${c.commission ? share(c.commission) : "0"}${c.commission && c.min_commission ? `（最低 ${c.min_commission} ${UNIT[market]}）` : ""}`,
    c.transfer > 0 && dated("transfer", `过户费 ${share(c.transfer)}`), c.stamp > 0 && dated("stamp", `印花税 ${share(c.stamp)}`),
    c.slippage > 0 && `滑点 ${share(c.slippage)}`]
  return [`最多 ${s.max_positions} 只${s.weak_positions != null ? `（大盘弱时 ${s.weak_positions} 只）` : ""}`, exits.join("、"), `初始资金 ${Math.round(s.cash).toLocaleString("en-US")} ${UNIT[market]}`,
    costs.filter(Boolean).join("、")].filter(Boolean).join(" · ")
}

/** The settings a run would use in a market: the form's places, exits, cash and costs over the market's defaults. */
export function formSettings(form: BookBody, market: Market, defaults: BookSettings): BookSettings {
  const mine = form.costs[market] ?? {}
  return { ...defaults, ...form.book, stall: form.book.stall ?? null, weak_positions: form.book.weak_positions ?? null, own: form.book.own ?? true,
    cash: form.cash[market] ?? defaults.cash, costs: { ...defaults.costs, ...mine,   // a rate the form sets: every day
      history: defaults.costs.history?.filter(([, k]) => !(k in mine)) } }
}

/** Whether the form now asks for something else than a run used: the places and exits, and this market's cash and
 *  costs where the form sets them. */
export function bookChanged(ran: BookSettings, form: BookBody, market: Market): boolean {
  const b = form.book, cash = form.cash[market], costs = form.costs[market]
  return b.max_positions !== ran.max_positions || b.hold !== ran.hold || b.box_stop !== ran.box_stop || b.trail !== ran.trail
    || b.target !== ran.target || (b.stall ?? null) !== (ran.stall ?? null) || (b.weak_positions ?? null) !== (ran.weak_positions ?? null)
    || (b.own ?? true) !== (ran.own ?? false)
    || (cash != null && cash !== ran.cash)
    || (costs != null && (Object.keys(costs) as (keyof Costs)[]).some((k) => costs[k] !== ran.costs[k]))
}
export interface OutcomeRow {
  market: Market; listing_id: string; name: string | null; signal_date: string; entry_date: string | null; blocked: string | null
  r1: number | null; x1: number | null; r3: number | null; x3: number | null; r5: number | null; x5: number | null
  r10: number | null; x10: number | null; r20: number | null; x20: number | null; r60: number | null; x60: number | null
  mfe: number | null; mae: number | null; gone: boolean | null; industry: string | null; above200: boolean | null
}

export const HORIZONS = [1, 3, 5, 10, 20, 60] as const
export const BLOCKED: Record<string, string> = { limit: "开盘涨停", halt: "停牌" }
export const VERDICT: Record<Verdict, [string, string]> = {
  // 多余 = removing it moved neither half by more than its error: that does not show it is useless — no advice to delete
  有效: ["有效", "text-muted-foreground"], 多余: ["看不出作用（误差内）", "text-subtle-foreground"], 有害: ["有害 · 看样例", "text-warning"],
  不一致: ["样本内外不一致", "text-subtle-foreground"], 样本太少: ["样本太少", "text-subtle-foreground"],
}

/** Effective samples below which a result is numbers only, no conclusion (spec §9.1; the backend's FEW). */
export const FEW = 30

/** How the 样本外 win rate compares with the 样本内 one (3 points either way is 持平) — said only when both halves
 * have FEW effective samples: 100% against 25% on nine signals is not 「更好」. */
export function drift(s: Split): "有衰减" | "持平" | "更好" | null {
  const a = s.in.win, b = s.out.win
  if (a == null || b == null || s.in.effective < FEW || s.out.effective < FEW) return null
  return b < a - 0.03 ? "有衰减" : b > a + 0.03 ? "更好" : "持平"
}

/** 导出报告: each signal with what followed it. */
export function rowsCSV(rows: OutcomeRow[], oosStart: string): string {
  const pct = (v: number | null | undefined) => (v == null ? null : fmtPct(v, 2, false))
  return toCSV(["市场", "代码", "名称", "信号日", "入场日", "买不到", ...HORIZONS.flatMap((h) => [`${h}日收益`, `${h}日超额`]),
    "20日最大浮盈", "20日最大浮亏", "停止交易", "行业", "大盘在200日线上", "样本"],
  rows.map((r) => [r.market, codeOf(r.listing_id), r.name, r.signal_date, r.entry_date, r.blocked ? BLOCKED[r.blocked] ?? r.blocked : null,
    ...HORIZONS.flatMap((h) => [pct(r[`r${h}`]), pct(r[`x${h}`])]), pct(r.mfe), pct(r.mae), r.gone ? "是" : null, r.industry,
    r.above200 == null ? null : r.above200 ? "是" : "否", r.signal_date >= oosStart ? "样本外" : "样本内"]))
}

/** 导出成交: each trade, why it ended (or that it was still held at the end). */
export function tradesCSV(rows: TradeRow[]): string {
  return toCSV(["市场", "代码", "名称", "信号日", "入场日", "入场价", "股数", "离场日", "离场价", "离场原因", "盈亏", "收益", "持有天数"],
    rows.map((t) => [t.market, codeOf(t.listing_id), t.name, t.signal_date, t.entry_date, t.entry_price, t.shares, t.exit_date, t.exit_price,
      t.exit_date ? REASON[t.reason ?? ""] ?? t.reason : "持有到最后", t.pnl.toFixed(2), t.ret == null ? null : fmtPct(t.ret, 2, false), t.days]))
}

/** 导出资金账本: every cash move, in each market's own currency. */
export function ledgerCSV(rows: LedgerRow[]): string {
  return toCSV(["市场", "日期", "代码", "名称", "类别", "股数", "价格", "金额", "说明"],
    rows.map((r) => [r.market, r.date, codeOf(r.listing_id), r.name, MOVE[r.kind] ?? r.kind, r.shares, r.price, r.amount.toFixed(2), r.note]))
}

/** 导出净值: cash, holdings and NAV each trading day. */
export function navCSV(rows: NavRow[]): string {
  return toCSV(["市场", "日期", "现金", "持仓市值", "净值", "持仓数"],
    rows.map((r) => [r.market, r.date, r.cash.toFixed(2), r.holdings.toFixed(2), r.nav.toFixed(2), r.positions]))
}

export const validateEndpoints = {
  setup: (markets: Market[]) => api<Setup>(`/validate/setup?markets=${markets.join(",")}`),
  runs: (ruleId?: string, kind?: ValidateKind) =>
    api<ValidateRun[]>(`/validate?${new URLSearchParams({ ...(ruleId ? { rule_id: ruleId } : {}), ...(kind ? { kind } : {}) })}`),
  run: (id: string) => api<ValidateRun>(`/validate/${id}`),
  rows: (id: string) => api<OutcomeRow[]>(`/validate/${id}/rows`),
  trades: (id: string) => api<TradeRow[]>(`/validate/${id}/trades`),
  ledger: (id: string) => api<LedgerRow[]>(`/validate/${id}/ledger`),
  nav: (id: string) => api<NavRow[]>(`/validate/${id}/nav`),
  start: (body: { kind: ValidateKind; rule_id?: string; version?: number; expr?: string; markets: Market[]; start?: string; end?: string }
    & Partial<BookBody>) =>
    api<{ id: string }>("/validate", { method: "POST", body: JSON.stringify(body) }),
  /** 暂停 (keeps the chunks it finished), 继续 (from there), 取消 (nothing kept); ok false: nothing to do */
  act: (id: string, what: "pause" | "resume" | "cancel") => api<{ ok: boolean }>(`/validate/${id}/${what}`, { method: "POST" }),
}

export const useValidateSetup = (markets: Market[]) =>
  useQuery({ queryKey: ["validate", "setup", markets.join(",")], queryFn: () => validateEndpoints.setup(markets), enabled: markets.length > 0 })
export const useValidateRuns = (ruleId: string | undefined, kind: ValidateKind) =>
  useQuery({ queryKey: ["validate", "runs", ruleId ?? "", kind], queryFn: () => validateEndpoints.runs(ruleId, kind), enabled: kind === "single" || !!ruleId })
/** A run, asked again every 1.5 s while it waits or runs. */
export const useValidateRun = (id: string | undefined) =>
  useQuery({
    queryKey: ["validate", "run", id], queryFn: () => validateEndpoints.run(id!), enabled: !!id,
    refetchInterval: (q) => (q.state.data && (q.state.data.status === "queued" || q.state.data.status === "running") ? 1500 : false),
  })
/** A kind of run as a page shows it: the latest — what runs, waits, is paused, was cancelled or failed — and the
 *  latest that finished, whose result stays in view while another runs; 暂停、继续、取消 on the latest. */
export function useRunsOf(ruleId: string | undefined, kind: ValidateKind) {
  const qc = useQueryClient()
  const runs = useValidateRuns(ruleId, kind)
  const latest = runs.data?.[0]
  const { data: active } = useValidateRun(latest?.id)
  const { data: shown } = useValidateRun(runs.data?.find((r) => r.status === "done")?.id)
  const moved = active !== undefined && latest !== undefined && active.status !== latest.status   // the list is not polled
  useEffect(() => {
    if (moved) void qc.invalidateQueries({ queryKey: ["validate", "runs"] })
  }, [moved, qc])
  const act = async (what: "pause" | "resume" | "cancel") => {
    if (!active) return
    const { ok } = await validateEndpoints.act(active.id, what)
    if (!ok) toast(what === "resume" ? "这次运行已经不在暂停了" : "这次运行已经结束了")
    void qc.invalidateQueries({ queryKey: ["validate"] })
  }
  return { runs, latest, active, shown, act }
}

export function useStartValidation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: validateEndpoints.start,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["validate", "runs"] }),
  })
}

/** A few wins as a count ("9/9"), where a rate (100%) would claim more than nine samples can. */
export function winsOf(win: number | null, n: number): string {
  return win == null || !n ? "—" : `${Math.round(win * n)}/${n}`
}

/** What a result's samples are judged by: the trade plan, the rule's own judgement (runs before plans), else the
 *  excess 20 days on. */
export const measure = (x: { metric?: "judge" | "hold" | "plan" } | null | undefined) =>
  (x?.metric === "plan" ? "按交易计划：" : x?.metric === "judge" ? "按规则自己的判断：" : "20 日")

const signed = (v: number | null | undefined) => fmtPct(v ?? null, 1)

/** The plan's result in one line: per signal day, its interval, and against stocks bought at random the same days. */
export function planLine(p: PlanPart): string {
  const head = `每个信号日平均 ${signed(p.expectancy)}（95% 区间 ${signed(p.ci[0])} 到 ${signed(p.ci[1])}）`
  if (p.vs_control == null) return head
  return `${head}；比同一天随机买的多 ${signed(p.vs_control)}（${signed(p.vs_control_ci[0])} 到 ${signed(p.vs_control_ci[1])}）`
}

export function entriesNote(n: number): string {
  return `比较了 ${n} 种买法：挑表现最好的那种，是从 ${n} 种里挑的，它的数字会偏好（挑选的偏差第五期算）`
}

export function crossLine(c: PlanCross): string {
  const pct = (v: number) => `${(v * 100).toFixed(1)}%`
  return `越线 ${c.crosses} 次，有越线的日子平均每天 ${c.per_day.toFixed(1)} 只；开盘就在箱顶上方的 ${pct(c.at_open)}，`
    + `收盘站上突破线的 ${pct(c.closed_above)}，收盘成了规则信号的 ${pct(c.signal)}`
}

/** What the comparison with the random control says: an interval wholly over 0, else no difference to see. */
export function planVerdict(p: PlanPart): string {
  if (p.few) return "信号日少于 30 个，不下结论"
  const [lo, hi] = p.vs_control_ci
  if (lo == null || hi == null) return "没有对照"
  return lo > 0 ? "比随机买强" : hi < 0 ? "比随机买差" : "和随机买看不出差别"
}

const sells = (x: number) => (x >= 1 ? "" : x === 0.5 ? "卖一半" : `卖 ${Math.round(x * 100)}%`)

/** The plan step by step, as the report's head reads it. */
export function planSteps(p: PlanSpec): string[] {
  const gap = p.max_gap != null ? `（开盘高出箱顶 ${+(p.max_gap * 100).toFixed(2)}% 以上不追）` : ""
  return [p.entry === "close" ? "信号那天尾盘买" : p.entry === "cross" ? `盘中第一次碰到潜伏的箱顶就买${gap}` : "信号第二天开盘买",
    ...p.stop.map((x) => `收盘跌破${x.label || x.line}就${sells(x.share) || "走"}`),
    ...p.take.map((x) => `碰到${x.label || x.line}${sells(x.share) || "全卖"}`),
    ...p.exit.map((x) => `收盘时${x.label || x.when}就${sells(x.share) || "卖"}`),
    ...(p.stall ? [`连续 ${p.stall} 天不创新高就卖`] : []),
    `最多拿 ${p.days} 个交易日`]
}
export const EXIT: Record<"target" | "fail" | "time" | "gone", string> = { target: "碰到目标位", fail: "跌破失效线", time: "看到最后一天", gone: "停止交易" }
