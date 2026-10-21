// The daily cycle and what it found (spec §7.1): where each market stands, the 今日 feed, a rule's latest scan
// for the 选股 page, and the notification channel with its outbox.
import { useQuery } from "@tanstack/react-query"
import { api, type Market, type SecretSource, type Settings } from "./api"
import { inUnit, MARKET_NAME } from "./format"
import { industryName } from "./marketWords"
import type { Detail } from "./rules"

// "interrupted": a 补历史 cut off with what it fetched not yet published — it comes before any update
// "paused": a scan you paused, or one cut off by quitting the app — 继续 goes on from the rule under way;
// "cancelled": a scan you gave up — 今日 shows the last scan's results until you scan again
export type JobStatus = "idle" | "updating" | "scanning" | "backfilling" | "checking" | "interrupted" | "paused" | "cancelled" | "done" | "delayed"
  | "error"
export type ChangeKind = "signal" | "advance" | "invalid" | "watch"
/** A rule's latest scan in a market; one that failed has `error`, one waiting for data that comes later has `waiting`. */
export interface ScanSummary {
  error?: string; waiting?: string
  name: string; version: number; day: string; run_id: string; previous_run: string | null; stages: string[]
  counts: Partial<Record<"signal" | "watch" | "unknown" | "fail", number>>; changes: Record<ChangeKind, number>
}
/** What a market's running turn is doing (null when nothing runs): the step (update, scan, research, check, backfill —
 *  or how the turn began), when the turn began, and the step's own counts. */
export interface JobProgress {
  step: string; since: string; waiting?: boolean           // waiting: the scheduled update, until the day's data is there
  pausing?: boolean; cancelling?: boolean                     // 暂停 / 取消 asked for: the scan stops at its next step
  phase?: string; done?: number; total?: number             // a fetch's dataset or a scan's step, and how far it is
  i?: number; n?: number; rule?: string; fraction?: number  // a scan: the i-th of n rules, and the share of the scan done
}
export interface JobState {
  market: Market; status: JobStatus; message: string | null; update_day: string | null; refetch_day: string | null
  scanned_snapshot: string | null; scan_at: string | null; scan_summary: Record<string, ScanSummary>; updated_at: string | null
  running: boolean; progress: JobProgress | null; latest: { id: string; as_of: string; demo: boolean } | null
  scan_state: string | null   // JSON {snapshot, total, done: {rule@version: run}}: what a paused scan has finished
}

/** How many rules a paused scan finished, of how many ("4 / 14 条规则"), from its kept state. */
export function scanDone(j: JobState): string | undefined {
  try {
    const s = j.scan_state ? (JSON.parse(j.scan_state) as { total?: number; done?: Record<string, string> }) : null
    return s?.total ? `${Object.keys(s.done ?? {}).length} / ${s.total} 条规则` : undefined
  } catch {
    return undefined
  }
}
export interface OutboxStats { pending: number; given_up: number; last_error: string | null }
export interface Jobs { markets: JobState[]; outbox: OutboxStats; feishu: boolean }

export type EventKind = ChangeKind | "data" | "live"
export interface TodayEvent {
  kind: EventKind; market: Market; day: string | null
  listing_id?: string; name?: string | null; rule_id?: string; rule_name?: string; version?: number; run_id?: string
  stage?: string | null; was?: string | null; stage_label?: string | null; was_label?: string | null // by name: 中箱, 向上突破
  signal_date?: string | null; from_watch?: boolean | null; structure?: string | null
  status?: JobStatus; text?: string | null // a data event
  verdict?: "confirmed" | "revoked"; at?: string; price?: number | null; detail?: { lines?: string[] } | null // the close on an intraday alert
}

/** A rule's scan with nothing earlier to compare with (its first): what it found, not a change per stock. */
export interface FirstScan {
  market: Market; rule_id: string; rule_name: string; version: number; day: string
  counts: Partial<Record<"signal" | "watch" | "unknown" | "fail", number>>
}

export interface StageLabel { id: string; label: string; head: string }
export interface ScreenMarket {
  run_id: string; version: number; day: string; snapshot_id: string; created_at: string
  counts: Partial<Record<"signal" | "watch" | "unknown" | "fail", number>>; stages: StageLabel[]; funnel: number[] | null
  listed: number; total: number // rows listed, out of all that reached a stage (the page lists the first ones)
  also?: { day: string | null; count: number; same_day: boolean } // 同时满足: the other rule's scan of this market
  // each 附加条件 here: how many it holds for, why it cannot be read, or skip: written for the other market only (not cut)
  where?: { count?: number; unknown?: number; error?: string; skip?: boolean }[]
}
export interface ScreenRow {
  listing_id: string; market: Market; name: string | null; tags: string[]
  industry: string | null; industry2: string | null   // 申万一级 / 二级 on the scan day (日股: 33 業種, no 二级)
  status: "signal" | "watch" | "unknown" | "fail"; depth: number | null; signal_date: string | null; detail: Detail | null
  repeat?: boolean | null   // 再次突破 (10-13): a signal whose structures completed before — a backtest counts the first only
  recent_signal: string | null // its latest signal in the rule's previous daily scans (近 N 日信号)
  close?: number; change?: number | null; amount?: number | null; spark?: number[]
  [rank: `rank${number}`]: number | null | undefined // the rule's ranking values, in its order
}
export interface Screen {
  rule: { id: string; name: string; version: number; markets: Market[] }
  stages: StageLabel[]; ranking: { expr: string; dir: "asc" | "desc"; label: string; unit: string }[] // label: the formula when it has none
  markets: Record<Market, ScreenMarket | null>; rows: ScreenRow[]
  also?: { id: string; name: string; status: AlsoStatus } // 同时满足另一条规则: the rows are the ones it found too
  where?: { expr: string }[] // 附加条件: the rows are the ones they all hold for on the scan's day
  industry?: { ref: string; level: "l1" | "l2"; name: string } // 行业 (from the 市场 page): the rows in it on the scan's day
}
/** 同时满足另一条规则: the other rule 命中 (signal), or 命中或潜伏 (signal or watch). */
export type AlsoStatus = "signal" | "watch"
export interface Also { id: string; status: AlsoStatus }
/** 附加条件 ready to pick: a formula over the bars, read on the scan's day, for any rule (or write your own; `CN: …; JP: …`
 * writes each market its own). */
export const WHERE_PRESETS = [
  { label: "近期 K 线短小", expr: "MA(BAR_LEN, 20) <= 0.03", hint: "最近 20 根 K 线的平均长度（(最高 − 最低) ÷ 前一天收盘）不超过 3%" },
  // 日股 has no free float (J-Quants): its 总市值, at about the share of stocks A 股's 100 亿 keeps (71% vs 74%, 2026-10)
  { label: "小盘股", expr: "CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000",
    hint: "A 股：流通市值不超过 100 亿元；日股没有流通市值的数据，用总市值不超过 1000 亿日元（两个市场都约七成股票在这条线以下）。以亿为单位，改数字就行" },
  // 10-17: an extra condition, 多个外资持股 (Hong Kong counts as foreign); 30% of A shares on 2026-10-12 (19% without 陆股通)
  { label: "多个外资持股", expr: "CN: FOREIGN_HOLDERS >= 2",
    hint: "A 股：最近一期已公告的报告里，前十大流通股东有 2 家以上外资（QFII、香港和澳门的机构、外国的公司和陆股通都算，个人、H 股代持、国内基金不算）；"
      + "日股没有这项数据，不筛。2021 年以来，这样的股票之后一到三个月和同样大小的股票差不多（2017–2020 年多涨约 2%）" },
]
/** A 附加条件 by name: a ready one by its words, one of your own by its formula. */
export const whereName = (expr: string) => WHERE_PRESETS.find((p) => p.expr === expr)?.label ?? expr

/** 选股's groups of rows: what the page and the K 线 page's 命中 list. */
export type ScreenGroup = "shown" | "signal" | "recent" | "watch" | "unknown"
export const SCREEN_GROUPS: [ScreenGroup, string][] = [
  ["shown", "全部"], ["signal", "命中"], ["recent", "近 5 日信号"], ["watch", "潜伏"], ["unknown", "无法判断"]]
export const inGroup = (r: ScreenRow, g: ScreenGroup) => g === "shown" ? r.status !== "fail" || !!r.recent_signal
  : g === "recent" ? r.status === "signal" || !!r.recent_signal : r.status === g

/** What the 选股 page shows — its rule, 同时满足, 附加条件, market, group (or funnel step) and search — kept as it changes, so
 * the K 线 page's 命中 lists the same stocks, in the same order, for J / K to walk. */
export interface ScreenView {
  rule: string | null; also: Also | null; where: string[]
  market: Market | "all"; group: ScreenGroup; step: number | null; q: string
  industry?: string | null   // 行业 from the 市场 page ("l1:传媒"); a view saved before it had none
}
export const NO_VIEW: ScreenView = { rule: null, also: null, where: [], market: "all", group: "shown", step: null, q: "", industry: null }

/** The rows a view lists: its market, its group or the funnel step it stands on, and its search, in the scan's order. */
export function screenRows(s: Pick<Screen, "rows"> | undefined, v: Pick<ScreenView, "market" | "group" | "step" | "q">): ScreenRow[] {
  const q = v.q.trim()
  return (s?.rows ?? []).filter((r) => (v.market === "all" || r.market === v.market)
    && (v.step === null ? inGroup(r, v.group) : (r.depth ?? -1) >= v.step)
    && (!q || (r.name ?? "").includes(q) || r.listing_id.includes(q.toUpperCase())))
}

/** A view in words: the rule, 同时满足, the 附加条件, the group or step, the market, the search. */
export function viewText(v: ScreenView, ruleName: string | null, alsoName: string | null, stepLabel?: string | null): string {
  return [ruleName ?? v.rule, v.also && `同时满足「${alsoName ?? v.also.id}」${v.also.status === "watch" ? "（命中或潜伏）" : ""}`,
    v.where.length > 0 && v.where.map(whereName).join("、"), v.industry && `行业：${industryName(v.industry.slice(3))}`,
    v.step !== null ? `走到「${stepLabel ?? v.step + 1}」` : v.group !== "shown" && SCREEN_GROUPS.find(([k]) => k === v.group)?.[1],
    v.market !== "all" && MARKET_NAME[v.market], v.q.trim() && `含「${v.q.trim()}」`].filter(Boolean).join(" · ")
}

/** The 选股 page as a view left it (its rule, 同时满足 and 附加条件; the rest the page keeps itself). */
export function screenLink(v: ScreenView): string {
  const q = new URLSearchParams()
  if (v.rule) q.set("rule", v.rule)
  if (v.also) { q.set("also", v.also.id); q.set("also_status", v.also.status) }
  for (const w of v.where) q.append("where", w)
  if (v.industry) q.set("industry", v.industry)
  return `/screen${q.size ? `?${q}` : ""}`
}

/** The 选股 request: the rule's latest scans, with 同时满足 and every 附加条件 when there are. */
export function screenPath(ruleId: string, also?: Also | null, where: string[] = [], industry?: string | null): string {
  const q = new URLSearchParams()
  if (also) { q.set("also", also.id); q.set("also_status", also.status) }
  for (const w of where) if (w.trim()) q.append("where", w.trim())
  if (industry) q.set("industry", industry)
  return `/screen/${encodeURIComponent(ruleId)}${q.size ? `?${q}` : ""}`
}

/** What a row's stages came to: how long each structure and window lasted, and its first measured values — by name,
 * in their units (中箱 22 根 · 突破量 2.40 倍); a value only where a condition compares one (not a yes / no). */
export function stageSummary(d: Detail | null): string {
  if (!d) return ""
  const n = d.names
  const stage = (s: string) => n?.stages[s] ?? s
  const spans = d.bindings.filter((b) => b.kind !== "event" && b.bars).map((b) => `${stage(b.stage)} ${b.bars} 根`)
  const values = d.conditions
    .filter((c) => typeof c.value === "number" && c.op && c.threshold != null && c.kind !== "all" && c.kind !== "any")
    .slice(0, 2).map((c) => {
      const key = `${c.stage}.${c.id}`
      return n ? `${n.conds[key] ?? `${stage(c.stage)} · ${c.id}`} ${inUnit(c.value as number, n.units[key])}`
        : `${c.id} ${Number(c.value).toFixed(2)}`
    })
  return [...spans, ...values].join(" · ")
}

export interface ChannelField { name: string; label: string; required: boolean; hint: string; source: SecretSource }
export interface OutboxItem {
  id: number; idem: string; kind: string; market: string | null; day: string | null; payload: { title: string; line: string }
  created_at: string; next_try_at: string; attempts: number; sent_at: string | null; last_error: string | null
}
export interface Notify {
  channels: Record<string, { name: string; fields: ChannelField[]; ready: boolean; problem: string | null }>
  stats: OutboxStats; recent: OutboxItem[]
  live?: Partial<Record<Market, { name: string; realtime: boolean }>> // each market's intraday source: 盘中提醒 only from a realtime one
}
/** 开盘前简报 (spec §7.1, 1.34): a title, what is said first, the sections, a footer — the card's content. */
export interface BriefLine { kind: "stock" | "group" | "note" | "text"; text: string; listing_id?: string; name?: string; code?: string }
export interface Brief {
  market: Market; day: string; title: string; warning: string | null; head: string[]
  sections: { title: string; lines: BriefLine[] }[]; footer: string; line: string
}
/** 开盘前简报's items (2026-10-10): the 选股 views you put into it, in your order — a rule, its 附加条件, maybe 同时满足 —
 *  listed or counted; `name`: as the briefing names it; `problem`: what keeps it out now (a rule not enabled …). */
export type BriefMode = "list" | "count"
export const BRIEF_MODE: Record<BriefMode, string> = { list: "列出股票", count: "只说数量" }
export interface BriefItem {
  id: number; rule_id: string; name: string; wheres: string[]; also: string | null; also_status: AlsoStatus | null
  mode: BriefMode; markets: Market[]; problem: string | null
}
const sameWheres = (a: string[], b: string[]) => {
  const x = [...new Set(a.map((w) => w.trim()).filter(Boolean))].sort(), y = [...new Set(b.map((w) => w.trim()).filter(Boolean))].sort()
  return x.length === y.length && x.every((w, i) => w === y[i])
}
/** The item a 选股 view already is: the same rule, the same 附加条件 in any order, the same 同时满足 (and its kind). */
export const inBrief = (items: BriefItem[], rule: string, also: Also | null, where: string[]) =>
  items.find((i) => i.rule_id === rule && sameWheres(i.wheres, where)
    && (i.also ?? null) === (also?.id ?? null) && (!also || i.also_status === also.status))
/** An item's 选股 view. */
export const itemLink = (i: BriefItem) =>
  screenLink({ ...NO_VIEW, rule: i.rule_id, also: i.also ? { id: i.also, status: i.also_status ?? "signal" } : null, where: i.wheres })

/** Whether the market's briefing goes out: as set, or (auto) for a market on the formal profile. */
export const briefOn = (s: Settings, m: Market) =>
  s[`brief.${m}`] === "auto" ? s[`profile.${m}`] === "prod" : s[`brief.${m}`] === "on"

export const jobEndpoints = {
  jobs: () => api<Jobs>("/jobs"),
  /** a manual run; `fetch`: whether it fetches, or only scans what there is again (the market open, demo data, a 补历史 to finish) */
  run: (market: Market) => api<{ started: boolean; fetch: boolean }>(`/jobs/${market}/run`, { method: "POST" }),
  /** 补历史: from `start` to the last finished day, resumable; `datasets`: only these (financials after the bars) */
  backfill: (market: Market, start: string, datasets?: string[]) =>
    api<{ started: boolean }>(`/data/${market}/backfill`, { method: "POST", body: JSON.stringify({ start, datasets }) }),
  crosscheck: (market: Market) => api<{ started: boolean }>(`/data/${market}/crosscheck`, { method: "POST" }),
  probe: (market: Market) => api<{ ok: boolean }>(`/data/${market}/probe`, { method: "POST" }),
  today: () => api<{ events: TodayEvent[]; first: FirstScan[] }>("/today"),
  /** 暂停: the scan stops at its next step, keeping the rules it finished; 继续 goes on from the next */
  pause: (market: Market) => api<{ paused: boolean }>(`/jobs/${market}/pause`, { method: "POST" }),
  resume: (market: Market) => api<{ started: boolean }>(`/jobs/${market}/resume`, { method: "POST" }),
  /** 取消: the running (or paused) scan is given up; 今日 keeps the last scan's results */
  cancel: (market: Market) => api<{ cancelled: boolean }>(`/jobs/${market}/cancel`, { method: "POST" }),
  screen: (ruleId: string, also?: Also | null, where: string[] = [], industry?: string | null) => api<Screen>(screenPath(ruleId, also, where, industry)),
  notify: () => api<Notify>("/notify"),
  testNotify: () => api<{ ok: boolean }>("/notify/test", { method: "POST" }),
  /** 预览: the briefing as it would go out now (without the announcements) */
  brief: (market: Market) => api<{ brief: Brief | null }>(`/brief/${market}`),
  /** 发一份到飞书: made now, sent at once (not the day's one) */
  sendBrief: (market: Market) => api<{ ok: boolean }>(`/brief/${market}/send`, { method: "POST" }),
  briefItems: () => api<BriefItem[]>("/brief-items"),
  /** a 选股 view into the briefing, last (422 with why when it could not use it) */
  addBriefItem: (v: { rule_id: string; wheres: string[]; also: string | null; also_status: AlsoStatus | null; mode: BriefMode }) =>
    api<BriefItem[]>("/brief-items", { method: "POST", body: JSON.stringify(v) }),
  briefItemMode: (id: number, mode: BriefMode) => api<BriefItem[]>(`/brief-items/${id}`, { method: "PUT", body: JSON.stringify({ mode }) }),
  moveBriefItem: (id: number, step: -1 | 1) => api<BriefItem[]>(`/brief-items/${id}/move`, { method: "POST", body: JSON.stringify({ step }) }),
  removeBriefItem: (id: number) => api<BriefItem[]>(`/brief-items/${id}`, { method: "DELETE" }),
}
export const useBriefItems = () => useQuery({ queryKey: ["brief-items"], queryFn: jobEndpoints.briefItems })

/** Quick while anything runs (a scan's progress and its end show up at once — a scan of demo data is over in
 * seconds); slow while a market only waits for its data — that can take the whole ready window, hours; slower still
 * when nothing runs. */
export function pollEvery(j: Jobs | undefined): number {
  const running = (j?.markets ?? []).filter((m) => m.running)
  if (!running.length) return 30_000
  return running.every((m) => m.progress?.waiting) ? 15_000 : 2_000
}

const STEP: Partial<Record<JobStatus, string>> = { updating: "更新数据", scanning: "扫描", backfilling: "补历史", checking: "抽样核对" }

/** What a running market is doing, in one line: the rule it scans and that rule's step, or the dataset it fetches,
 *  with how far it is. */
export function doing(j: JobState): string {
  const p = j.progress
  const of = (done = 0, total = 0) => (total ? ` ${done.toLocaleString()} / ${total.toLocaleString()}` : "")
  switch (p?.step) {
    case "scan":   // where it is, still, while a pause or a cancel waits for the step to end (the controls say that)
      return p.rule ? `扫描第 ${p.i} / ${p.n} 条规则「${p.rule}」：${p.phase}${of(p.done, p.total)}` : j.message ?? "准备扫描"
    case "update": return p.phase ? `更新数据：${p.phase}${of(p.done, p.total)}` : p.waiting ? "等当天的数据就绪" : "更新数据"
    case "backfill": return `补历史${p.phase ? `：${p.phase}${of(p.done, p.total)}` : ""}`
    case "research": return `AI 研究新信号${of(p.done, p.total)}`
    case "check": return "抽样核对"
  }
  return STEP[j.status] ?? "准备中"   // a turn just begun, before it says what it does
}

/** How long since `since` (the backend's local time, no zone: the same clock as this one). */
export function took(since: string, now: number): string {
  const s = Math.max(0, Math.floor((now - new Date(since).getTime()) / 1000))
  const two = (n: number) => String(n).padStart(2, "0")
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${two(s % 60)} 秒`
  return `${Math.floor(s / 3600)} 小时 ${two(Math.floor((s % 3600) / 60))} 分`
}
export const useJobs = () => useQuery({ queryKey: ["jobs"], queryFn: jobEndpoints.jobs, refetchInterval: (q) => pollEvery(q.state.data) })
// a scan's progress moves the key on (scan_at): the last answer stays on screen while the next loads — it blanked
// 今日 (and dropped the keys pressed meanwhile) every few seconds while a scan ran
export const useToday = (scanned: string) =>
  useQuery({ queryKey: ["today", scanned], queryFn: jobEndpoints.today, staleTime: 60_000, placeholderData: (prev) => prev })
export const useScreen = (ruleId: string | undefined, scanned: string, also?: Also | null, where: string[] = [], industry: string | null = null) =>
  useQuery({ queryKey: ["screen", ruleId, scanned, also?.id ?? null, also?.status ?? null, where.join("\n"), industry],
    queryFn: () => jobEndpoints.screen(ruleId!, also, where, industry), enabled: !!ruleId,
    // the same rule with the same 同时满足's, 附加条件 and 行业, never another's
    placeholderData: (prev, q) => (q?.queryKey[1] === ruleId && q?.queryKey[3] === (also?.id ?? null) && q?.queryKey[5] === where.join("\n")
      && q?.queryKey[6] === industry ? prev : undefined) })
export const useNotify = () => useQuery({ queryKey: ["notify"], queryFn: jobEndpoints.notify })

/** What the scans are at, as a key: the feed and the screen refetch when a market finishes a scan. */
export const scannedKey = (j: Jobs | undefined) => (j?.markets ?? []).map((m) => `${m.market}:${m.scanned_snapshot}:${m.scan_at}`).join("|")

export const KIND_LABEL: Record<EventKind, string> = {
  signal: "新信号", live: "盘中预警", advance: "潜伏推进", invalid: "失效", watch: "新潜伏", data: "数据",
}

/** One line under the name: what happened, in the rule's words. */
export function eventLine(e: TodayEvent): string {
  if (e.kind === "data") return e.text ?? (e.status === "error" ? "出错" : "数据延迟")
  if (e.kind === "live") return `${e.verdict === "confirmed" ? "收盘确认" : "收盘撤销"} · ${(e.at ?? "").slice(11, 16)} 盘中预警：${e.text ?? ""}`
  const rule = e.rule_name ?? e.rule_id ?? ""
  if (e.kind === "signal") return `${rule} · 信号 ${e.signal_date ?? ""}${e.from_watch ? "（来自潜伏名单）" : ""}`
  const stage = e.stage_label ?? e.stage ?? "?"
  if (e.kind === "advance") return `${rule} · ${e.was_label ?? e.was ?? "?"} → ${stage}`
  if (e.kind === "invalid") return `${rule} · 停在 ${stage}，没有出信号`
  return `${rule} · 进入潜伏（到 ${stage}）`
}

/** A stable key for an event, to remember the ones you set aside (E) for the day. */
export const eventKey = (e: TodayEvent) => [e.kind, e.market, e.listing_id, e.rule_id, e.day, e.stage].join("|")

/** The rows as CSV (UTF-8 with a BOM, so Excel reads the Chinese names). */
export function toCSV(header: string[], rows: (string | number | null | undefined)[][]): string {
  const cell = (v: string | number | null | undefined) => {
    const s = v == null ? "" : String(v)
    return /[",\n\r]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s   // a lone \r is a line break to Excel too
  }
  return "﻿" + [header, ...rows].map((r) => r.map(cell).join(",")).join("\n") + "\n"
}
