// Client for the rule engine's endpoints (wb/api/rules.py, wb/api/labels.py): structures to draw, rules
// and their versions, the single-stock preview, market runs, the sample gallery and your verdicts, the
// missed-hit check, version comparison, box labels and their agreement (spec §5, §6.3, §6.5, §8.2).
import { useQuery } from "@tanstack/react-query"
import { api, type Basis, type Market, type Period } from "./api"

// ---- structures (spec §8.2) ----------------------------------------------------------------------
export interface Segment { from: string; to: string | null; top: number; bottom: number }
export interface Touch { date: string; side: "top" | "bottom"; price: number; known_at: string }
export interface BoxEvent { date: string; event: string; value: number | null; known_at: string }
export interface Box {
  id: string; scale: "short" | "mid" | "long"; status: "established" | "proto"
  start: string; known_at: string; end: string | null; ended_at: string | null; end_reason: string | null
  width: number | null; length: number | null; shape: string | null; context: string | null; position: string | null
  excluded_before: string | null; superseded_at: string | null
  first_known?: string   // the day the box was first drawn (known_at: this version's, the day it was redrawn)
  segments: Segment[]; touches: Touch[]; events: BoxEvent[]
}
export interface PricePoint { date: string; price: number }
export interface Slant {
  id: string; status: string; start: string; known_at: string; end: string | null; ended_at: string | null
  end_reason: string | null; width: number | null; top: PricePoint[]; bottom: PricePoint[]
}
export interface Pivot { date: string; kind: "high" | "low"; price: number; known_at: string }
export interface Swing { start: string; end: string; direction: "up" | "down"; change: number; start_price: number; end_price: number; known_at: string }
// a 金品 is its big box's last small box: superseded_at — when a later small box in the same big box became known
export interface Gold { small_box_id: string; big_box_id: string; kind: string; variant: boolean; known_at: string; superseded_at?: string | null }
// a 假底 (false bottom): the old box's top and bottom (display basis) for the faint lines beside it
export interface FalseBottom {
  box_id: string; source: "金品" | "C 点"; date: string; price: number; known_at: string
  end: string | null; end_reason: "失守" | "新箱" | "到期" | null; box_top: number; box_bottom: number
}
export interface Structures {
  boxes: Box[]; slant: Slant[]; pivots: Pivot[]; swings: Swing[]; gold: Gold[]; false_bottoms: FalseBottom[]
  /** the chart's period they were found on, and the band (突破线 = top × (1 + tol)); a rule's preview: a day's */
  period?: Period; tol?: number
}
/** The boxes a stage's structure id stands for: a 金品 / 箱中箱 is "<big box>><small box>", a box itself. */
export const boxesOf = (id: string) => id.split(">")
export const NO_STRUCTURES: Structures = { boxes: [], slant: [], pivots: [], swings: [], gold: [], false_bottoms: [] }
export interface Pressure {
  date: string | null; box_id: string | null; start?: string | null; end?: string | null
  bottom?: number | null; top?: number | null; bars?: number | null; heavy_down?: number | null
  space: number | null; none_above: boolean; half_year_high?: number | null; one_year_high?: number | null
  space_half_year?: number | null; space_one_year?: number | null
}

// ---- rules ---------------------------------------------------------------------------------------
export interface RuleSummary {
  id: string; name: string; version: number; builtin: boolean; markets: Market[]; doc: string; error: string | null
  problem: string | null // saved, readable, but a formula does not compile: it cannot run, nor be enabled
  group: "builtin" | "box" | "general" | "mine" // 内置选股公式, 箱体模板, 通用模板, yours
  enabled: boolean; archived: boolean; updated_at: string | null; verdicts: { right: number; wrong: number }
}
/** What a rule says it is (the parsed text). */
export interface RuleInfo {
  id: string; name: string; doc: string; markets: Market[]; params: (RuleParam & { tune: boolean })[]; stages?: RuleStage[]
  names?: Names // how its parts read (a check's answer): the sliders say where by them
}
/** 我的理解 (spec §6.1): lines of text and parameter chips ("text" is how a chip reads as plain text). */
export type UnderstandSeg = string | { param: string; value: number | boolean | string; unit?: Unit; text: string }
export interface Understanding { summary: string; sections: { title: string; lines: UnderstandSeg[][] }[] }
export interface RuleCheck {
  ok: boolean; errors: string[]; rule?: RuleInfo; understanding?: Understanding
  warnings?: string[] // a name in the pool that nothing carries (a typo would leave the pool empty without a word)
}
export interface RuleDoc {
  id: string; version: number; builtin: boolean; text: string
  versions: { version: number; hash: string; created_at: string }[]
  rule: RuleInfo | null; error: string | null // a stored version that no longer loads comes back with its text and the reason
}
/** How a value reads (spec §5.2): % a share shown ×100 (0.15 → 15%), 倍 a multiple, 根 bars. */
export type Unit = "" | "%" | "倍" | "根"
export interface RuleParam { id: string; value: number | boolean | string; range: [number, number] | null; step: number | null; unit?: Unit }
/** A stage as the rule's JSON has it — what the parameter hints read. */
export interface RuleStage {
  id: string; kind: string; when?: string | null; all?: string | null; any?: string | null; with?: Record<string, unknown>
  at?: { id: string; expr: string }[]; during?: { id: string; expr: string }[]
  before?: StageRef | null; after?: StageRef | null; within?: StageRef | null
}
export interface StageRef { stage: string; bars?: unknown; gap?: unknown }
/** Which rule to run: a saved one (id, version) or text being edited; slider values on top. */
export interface RuleRef { rule_id?: string; version?: number; text?: string; params?: Record<string, number | boolean | string> }

export interface Signal { listing_id: string; signal_date: string; [stage: string]: string | number | null }
export interface Preview {
  listing_id: string; snapshot_id: string; warnings: string[]
  rule: { id: string; version: number; name: string; params: RuleParam[]; stages?: RuleStage[]; mas?: number[] } // mas: the close MAs it reads
  signals: Signal[]; structures: Structures
}
export interface Condition {
  stage: string; id: string; kind: string; expr: string; op: string | null
  value: number | boolean | null; threshold: number | null; status: "pass" | "fail" | "unknown"
}
export interface Binding {
  stage: string; kind: string; id: string | null; start: string | null; end: string | null; at: string | null
  bars?: number | null // how many bars the stage spans
}
export interface Detail {
  listing_id: string; date: string; status: "signal" | "watch" | "unknown" | "fail" | null
  bindings: Binding[]; conditions: Condition[]; deepest_stage: string | null
  closest_miss: Condition | null; distances: { id: string; value: number | null; threshold: number | null; edge?: number | null }[]
  unknown_reasons: { kind: string; [k: string]: unknown }[]
  names?: Names // how the rule's parts read; a result kept before 1.36 has none and reads by ids
}
/** How a rule's parts read (spec §5.6): stages and conditions by name (key "stage.condition"), each with its unit. */
export interface Names { stages: Record<string, string>; conds: Record<string, string>; units: Record<string, string> }
export interface Band { stage: string; label?: string; kind: string; start: string | null; end: string | null } // label: the stage's name
export interface PreviewDetail { detail: Detail; lines: string[]; bands: Band[] }

export interface RunSummary {
  id: string; rule_id: string; version: number; market: Market; snapshot_id: string; mode: "date" | "range"
  day: string | null; start: string | null; end: string | null; counts: Record<string, number>; warnings: string[]
}
export interface RunRecord {
  id: string; rule_id: string; version: number; rule_hash: string; market: Market; snapshot_id: string; mode: string
  day: string | null; start: string | null; end: string | null; counts: Record<string, number>; created_at: string
}
export interface Hit { listing_id: string; signal_date: string; name: string | null; days: number; [stage: string]: unknown }
/** Which listings to try a rule on: a fixed random sample of the market, or one of your watch groups. */
export interface Sample { n?: number; seed?: number; group_id?: number }
/** 内置模板库 (spec §6.6): a template's card. */
export interface Template {
  id: string; group: "box" | "general"; name: string; shape: string; basis: string; describe: string
  markets: Market[]; doc: string; examples?: { listing_id: string; date: string; note: string }[]
}
/** 网格校准 (spec §6.5): shares of your marks per group — right hits found, wrong hits dropped, box samples agreeing. */
export interface CalScores { right: number | null; wrong: number | null; boxes: number | null }
export interface CalPoint { values: Record<string, number | boolean>; tune: CalScores; test: CalScores; hits: number }
export interface Calibration {
  params: string[]; axes: Record<string, (number | boolean)[]>; combos: number; structural: string[]
  current: CalPoint; candidates: CalPoint[]; explained: { label: string; values: Record<string, number | boolean> | null }[]
  samples: { right: number; wrong: number; boxes: number; tune: number; test: number
    by_group?: Record<"tune" | "test", { right: number; wrong: number; boxes: number }> }  // what each half's shares are of
  warning: string | null
}
/** What a 校准 run ran on: a draft's text (null: the saved version), the slider values, the years and the sample. */
export interface CalibrationSettings { text: string | null; params: Record<string, number | boolean | string>; years: number; sample: { n: number; seed: number } }
export interface Gallery { listings: number; hits: Hit[] }
export interface Compare {
  listings: number; added: Hit[]; lost: Hit[]; kept: number; hits: [number, number]; right: [number, number]; wrong: [number, number]
  right_lost: Hit[]; wrong_added: Hit[]
}
export interface Missed {
  status: "hit" | "missed" | "no_bars"; days?: string[]; day?: string | null; deepest_stage?: string | null
  miss?: Condition | null; names?: Names | null; lines?: string[]; param?: string | null; relaxed?: number | null
  hits_before?: number | null; hits_after?: number | null; note?: string | null
}
export type Verdict = "right" | "wrong"
export interface HitLabel { listing_id: string; signal_date: string; verdict: Verdict; rule_version: number | null }

// ---- labels (spec §5.10) -------------------------------------------------------------------------
export interface LabelDraft {
  listing_id: string; cutoff: string; is_box: boolean; category: string; start: string; end: string
  top: number; bottom: number; scale?: "short" | "mid" | "long" | null; context?: "上横" | "下横" | null
  position?: "低位" | "中继" | "高位" | null; touches: { date: string; side: "top" | "bottom"; price: number }[]
  exclusions: { date: string; kind: string; price: number; reason: string }[]; redraw_of?: string | null; note?: string
}
export interface Label extends LabelDraft { id: string; grp: "tune" | "test"; verdict: "algo_wrong" | "me_wrong" | "both_ok" | null; created_at: string; updated_at: string }
export interface LabelResult {
  id: string; listing_id: string; category: string; is_box: boolean; grp: "tune" | "test"; verdict: Label["verdict"]
  box_id: string | null; box_scale?: string; consistent: boolean; false_draw: boolean | null
  top_diff?: number; bottom_diff?: number; overlap?: number; start_diff?: number; end_diff?: number | null; same_first_break?: boolean | null
}
export interface GroupSummary {
  boxes: number; consistent: number; rate: number | null; by_category: Record<string, [number, number]>
  not_boxes: number; false_draws: number; pending: number
}
export interface Agreement {
  labels: LabelResult[]; summary: { tune: GroupSummary; test: GroupSummary }
  self: { pairs: number; consistent: number; rate: number | null }
  acceptance: { passed: boolean; checks: Record<string, boolean>; too_strict: boolean }
  thresholds: { edge: number; overlap: number; start_bars: number; start_share: number }
}
export const BOX_CATEGORIES: Record<string, string> = {
  down_base: "下横（下跌后的底部箱）", up_relay: "上横（上涨中继箱）", down_relay: "下跌中继", high_flat: "高位横盘",
  flat: "平箱", curved: "曲箱", slant: "斜箱", break_return: "破箱回箱", nested: "大箱套小箱（含金品）",
  short_n: "突破后单底短 N 小箱",
}
export const NOT_BOX_CATEGORIES: Record<string, string> = {
  trend_pause: "单边趋势里的短暂停顿", wide_chop: "宽幅乱震", one_high_one_low: "只有一高一低的雏形", other: "其他看着像、按定义不算的",
}

const post = <T>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) })
const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) })
const qs = (p: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [k, String(v)])).toString()

export const ruleEndpoints = {
  structures: (listing_id: string, basis: Basis, live?: boolean, period: Period = "day") =>
    api<Structures & { listing_id: string; snapshot_id: string | null; basis: Basis }>(
      `/structures?${qs({ listing_id, basis, live: live || undefined, period: period === "day" ? undefined : period })}`),
  pressure: (listing_id: string, basis: Basis, day?: string | null) => api<Pressure>(`/pressure?${qs({ listing_id, basis, day })}`),
  dates: (listing_id: string) => api<string[]>(`/dates?${qs({ listing_id })}`),
  sample: (market: Market, n: number, seed: number) => api<string[]>(`/sample?${qs({ market, n, seed })}`),
  rules: () => api<RuleSummary[]>("/rules"),
  rule: (id: string, version?: number) => api<RuleDoc>(`/rules/${encodeURIComponent(id)}?${qs({ version })}`),
  check: (text: string) => post<RuleCheck>("/rules/check", { text }),
  /** `create`: a new rule — refused (409) when its id is taken, never a new version of another rule */
  save: (text: string, create = false) => post<{ id: string; version: number; new: boolean }>("/rules", { text, create }),
  setState: (id: string, state: { enabled?: boolean; archived?: boolean }) => put<{ ok: boolean }>(`/rules/${encodeURIComponent(id)}/state`, state),
  preview: (ref: RuleRef, listing_id: string, basis: Basis) => post<Preview>("/preview", { ...ref, listing_id, basis }),
  detail: (ref: RuleRef, listing_id: string, day: string) => post<PreviewDetail>("/preview/detail", { ...ref, listing_id, day }),
  run: (ref: RuleRef, market: Market, opts: { day?: string; start?: string; end?: string } = {}) =>
    post<RunSummary>("/runs", { ...ref, market, ...opts }),
  runs: (rule_id?: string, limit = 20) => api<RunRecord[]>(`/runs?${qs({ rule_id, limit })}`),
  gallery: (ref: RuleRef, market: Market, sample: Sample, years = 2) => post<Gallery>("/gallery", { ...ref, market, sample, years }),
  compare: (a: RuleRef, b: RuleRef, market: Market, sample: Sample, years = 2) => post<Compare>("/rules/compare", { a, b, market, sample, years }),
  /** 校准 as a run in 验证's queue: followed, paused, continued and cancelled there (lib/validate useRunsOf) */
  calibrate: (ref: RuleRef, sample: Sample, years = 2) => post<{ id: string }>("/rules/calibrate", { ...ref, sample, years }),
  templates: () => api<Template[]>("/templates"),
  missed: (ref: RuleRef, listing_id: string, start: string, end: string, sample: Sample, years = 2) =>
    post<Missed>("/rules/missed", { ...ref, listing_id, start, end, sample, years }),
  hitLabels: (rule_id: string) => api<HitLabel[]>(`/hit_labels?${qs({ rule_id })}`),
  setHitLabel: (rule_id: string, listing_id: string, signal_date: string, verdict: Verdict | null, version?: number) =>
    put<{ ok: boolean }>("/hit_labels", { rule_id, listing_id, signal_date, verdict, version }),
  labels: (listing_id?: string, basis: Basis = "qfq") => api<Label[]>(`/labels?${qs({ listing_id, basis })}`),
  addLabel: (draft: LabelDraft, basis: Basis) => post<{ id: string }>("/labels", { ...draft, basis }),
  labelVerdict: (id: string, verdict: Label["verdict"]) => put<{ id: string }>(`/labels/${id}/verdict`, { verdict }),
  deleteLabel: (id: string) => api<{ ok: boolean }>(`/labels/${id}`, { method: "DELETE" }),
  /** §5.10 agreement in one market, or (null) over the whole label set, as the P2 acceptance line counts it */
  agreement: (market: Market | null, settings: Record<string, number | string | boolean> = {}) =>
    post<Agreement>("/labels/agreement", market ? { market, settings } : { settings }),
}

// ---- hooks -----------------------------------------------------------------------------------------
/** The rule list, fresh whenever a page that shows it opens (rules can also be saved from the command line). */
export const useRules = () => useQuery({ queryKey: ["rules"], queryFn: ruleEndpoints.rules, refetchOnMount: "always" })
export const useRule = (id: string | undefined, version?: number) =>
  useQuery({ queryKey: ["rule", id, version], queryFn: () => ruleEndpoints.rule(id!, version), enabled: !!id })
export const useHitLabels = (ruleId: string | undefined) =>
  useQuery({ queryKey: ["hit_labels", ruleId], queryFn: () => ruleEndpoints.hitLabels(ruleId!), enabled: !!ruleId })
/** While a new minute's structures load, the ones on screen stay — the same stock's on the same basis only. */
export const keepWithin = (listingId: string | undefined, basis: Basis, period?: Period) =>
  <T,>(prev: T | undefined, q?: { queryKey: readonly unknown[] }): T | undefined =>
    q?.queryKey[1] === listingId && q?.queryKey[2] === basis && (period === undefined || q?.queryKey[4] === period) ? prev : undefined

/** `live`: the minute the market's prices are as of — the structures with today's bar still forming, asked again each minute. */
export const useStructures = (listingId: string | undefined, basis: Basis, enabled = true, live: string | null = null,
  period: Period = "day") =>
  useQuery({
    queryKey: ["structures", listingId, basis, live, period], queryFn: () => ruleEndpoints.structures(listingId!, basis, !!live, period),
    // another period's while this one loads would put its boxes on the wrong bars: only this period's
    enabled: !!listingId && enabled, placeholderData: keepWithin(listingId, basis, period),
  })
export const usePressure = (listingId: string | undefined, basis: Basis, day: string | null, enabled = true) =>
  useQuery({ queryKey: ["pressure", listingId, basis, day], queryFn: () => ruleEndpoints.pressure(listingId!, basis, day), enabled: !!listingId && enabled })
/** Your sample boxes on one stock, or (no stock) all of them. */
export const useTemplates = () => useQuery({ queryKey: ["templates"], queryFn: ruleEndpoints.templates, staleTime: Infinity })
export const useLabels = (listingId?: string, basis: Basis = "qfq", enabled = true) =>
  useQuery({ queryKey: ["labels", listingId ?? "*", basis], queryFn: () => ruleEndpoints.labels(listingId, basis), enabled })
export const useRuns = (ruleId?: string, limit = 20, enabled = true) =>
  useQuery({ queryKey: ["runs", ruleId ?? "*", limit], queryFn: () => ruleEndpoints.runs(ruleId, limit), enabled })

