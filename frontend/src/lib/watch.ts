// 观察条目 (spec §11): a stock you watch — its status, why, what speaks for and against it and your notes (every
// version kept), fail lines and review dates, how it has done since it was added against the market's benchmark (also
// once set aside, so the record is not only the successes), and what happened.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type Market } from "./api"
import { codeOf, fmtPct, fmtPrice, indexName, shortDate } from "./format"
import { toCSV } from "./jobs"

export type WatchStatus = "research" | "watching" | "holding" | "passed" | "closed"
export type TextKind = "reason" | "for" | "against" | "note"
export const STATUSES: [WatchStatus, string][] = [["watching", "观察中"], ["holding", "持有中"], ["research", "待研究"], ["passed", "暂不考虑"], ["closed", "已关闭"]]
export const TEXTS: Record<TextKind, string> = { reason: "为什么关注", for: "支持", against: "反对 / 风险", note: "我的笔记" }

export interface WatchCondition {
  id: number; kind: "fail" | "review" | "manual"; text: string; op: "<" | ">" | null; price: number | null; due: string | null
  created_at: string; fired_day: string | null; fired_value: number | null
  distance?: number; days_left?: number // the line against the latest close; the days to the review
}
export interface WatchEntry {
  listing_id: string; market: Market; name: string | null; status: WatchStatus; status_name: string; source: string
  added_at: string; added_day: string | null; added_price: number | null; updated_at: string
  reason: string; for: string; against: string; note: string; conditions: WatchCondition[]
  lines?: WatchLine[] // the lines drawn on its chart that alert (养箱's trigger)
  performance: { since: number; benchmark: string; bench: number | null; excess: number | null; day: string; close: number } | null
  record: { text: string; at: string }[] // newest first
}
/** A line drawn on the stock's chart with 价格提醒 on: a level (its price, the exchange's then, and its distance from the
 *  latest close) or a sloped line (its two points). */
export interface WatchLine {
  id: string; kind: string; points: { date: string; price: number }[]; price: number | null; distance: number | null
}
const LINE_NAME: Record<string, string> = { horizontalStraightLine: "水平线", segment: "趋势线", rayLine: "射线" }

export function lineText(l: WatchLine, market: Market): string {
  const name = LINE_NAME[l.kind] ?? "画线"
  if (l.price != null) return `${name} ${fmtPrice(l.price, market)}${l.distance != null ? `（${fmtPct(l.distance, 1)}）` : ""}`
  return `${name} ${l.points.map((p) => `${shortDate(p.date)} ${fmtPrice(p.price, market)}`).join(" → ")}`
}

/** 持有中: a fail line its structures suggest (a 金品's small-box bottom, a 假底, a box's bottom × 0.985), in the prices of
 *  its latest close; highest first. */
export interface FailLine { source: "金品" | "假底" | "箱底"; label: string; price: number; since: string; distance: number }

export type NewCondition = Pick<WatchCondition, "kind" | "text"> & Partial<Pick<WatchCondition, "op" | "price" | "due">>

const NEAR = 0.03 // a close within 3% of a fail line is worth a look

/** What to look at next, for the list: a line it crossed or a review that came due, a line it is near, the next
 * review, the nearest line — or nothing. */
export function nextUp(e: WatchEntry, market: Market): { text: string; warn: boolean } | null {
  const c = e.conditions
  const fired = c.find((x) => x.fired_day && x.kind !== "manual")
  if (fired) return fired.kind === "fail" ? { text: `触发了失效线 ${shortDate(fired.fired_day)}`, warn: true } : { text: `到了复核日 ${shortDate(fired.due)}`, warn: true }
  const lines = c.filter((x) => x.kind === "fail" && x.distance != null).sort((a, b) => Math.abs(a.distance!) - Math.abs(b.distance!))
  if (lines[0] && Math.abs(lines[0].distance!) <= NEAR) return { text: `接近失效线 ${fmtPct(lines[0].distance, 1)}`, warn: true }
  const review = c.filter((x) => x.kind === "review").sort((a, b) => (a.due ?? "").localeCompare(b.due ?? ""))[0]
  if (review) return { text: `下次复核 ${shortDate(review.due)}`, warn: false }
  if (lines[0]) return { text: `失效线 ${fmtPrice(lines[0].price, market)}`, warn: false }
  return null
}

export function conditionText(c: WatchCondition): string {
  const what = c.kind === "fail" ? `收盘 ${c.op} ${c.price}` : c.kind === "review" ? `${c.due} 复核` : "自己对照"
  return `${c.text}（${what}）${c.fired_day ? `，${c.fired_day} 触发` : ""}`
}

/** 导出复盘: every entry with how it has done, its texts, conditions and record (oldest first), as CSV. */
export function reviewCSV(entries: WatchEntry[]): string {
  const pct = (v: number | null | undefined) => (v == null ? null : fmtPct(v, 2, false))
  return toCSV(["市场", "代码", "名称", "状态", "来源", "加入日", "加入价", "截至", "至今", "基准", "基准同期", "超额",
    TEXTS.reason, TEXTS.for, TEXTS.against, TEXTS.note, "条件", "记录"],
  entries.map((e) => {
    const p = e.performance
    return [e.market, codeOf(e.listing_id), e.name, e.status_name, e.source, e.added_day, e.added_price, p?.day, pct(p?.since),
      p ? indexName(p.benchmark) : null, pct(p?.bench), pct(p?.excess), e.reason, e.for, e.against, e.note,
      e.conditions.map(conditionText).join("；"), [...e.record].reverse().map((r) => `${r.at.slice(0, 16).replace("T", " ")} ${r.text}`).join("\n")]
  }))
}

const one = (lid: string) => `/watch/${encodeURIComponent(lid)}`
const send = <T,>(path: string, method: string, body?: unknown) =>
  api<T>(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
export const watchEndpoints = {
  entries: () => api<WatchEntry[]>("/watch"),
  setStatus: (lid: string, status: WatchStatus) => send<WatchEntry>(`${one(lid)}/status`, "PUT", { status }),
  // keepalive: a save sent as the page goes (a reload, the app closing) still arrives
  setText: (lid: string, kind: TextKind, text: string) =>
    api<WatchEntry>(`${one(lid)}/text`, { method: "PUT", body: JSON.stringify({ kind, text }), keepalive: true }),
  versions: (lid: string, kind: TextKind) => api<{ text: string; created_at: string }[]>(`${one(lid)}/versions?kind=${kind}`),
  addCondition: (lid: string, c: NewCondition) => send<WatchEntry>(`${one(lid)}/conditions`, "POST", c),
  removeCondition: (lid: string, id: number) => send<WatchEntry>(`${one(lid)}/conditions/${id}`, "DELETE"),
  failLines: (lid: string) => api<FailLine[]>(`${one(lid)}/fail-lines`),
  /** 删除: the entries and all they had (not 已关闭, which keeps the record) */
  remove: (lids: string[]) => api<{ deleted: number }>("/watch/delete", { method: "POST", body: JSON.stringify({ listing_ids: lids }) }),
}

export const useWatchEntries = () => useQuery({ queryKey: ["watch"], queryFn: watchEndpoints.entries })
export const useFailLines = (lid: string, on: boolean) =>
  useQuery({ queryKey: ["watch", lid, "fail-lines"], queryFn: () => watchEndpoints.failLines(lid), enabled: on })
export const useVersions = (lid: string, kind: TextKind, on: boolean) =>
  useQuery({ queryKey: ["watch", lid, "versions", kind], queryFn: () => watchEndpoints.versions(lid, kind), enabled: on })

/** A change to an entry: the list takes the entry the backend sends back; a status change also moves it between the
 * groups (closed: out of them), so the groups and the K-line page's button are fetched again. */
export function useWatchChange<V>(fn: (v: V) => Promise<WatchEntry>) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: (e) => {
      qc.setQueryData<WatchEntry[]>(["watch"], (list) => list?.map((x) => (x.listing_id === e.listing_id ? e : x)))
      void qc.invalidateQueries({ queryKey: ["watch", e.listing_id] })
      void qc.invalidateQueries({ queryKey: ["watchlist"] })
      void qc.invalidateQueries({ queryKey: ["instrument", e.listing_id] })
    },
  })
}
