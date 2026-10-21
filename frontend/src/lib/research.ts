// 个股研究 (spec §10.3) and its record (§10.4): the evidence pack a research is given, the checked answer citing it,
// what changed since the last one, and what followed each research 20 trading days on (forward only).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type Market } from "./api"
import { fmtPct } from "./format"

export interface Fact {
  id: string; kind: string; source: string; as_of: string; text: string; value: number | null
  url?: string            // an announcement's original
  doc?: string; page?: number // a document you imported: which, and the excerpt's page
}
export interface Pack { listing_id: string; name: string | null; market: Market; day: string; facts: Fact[]; left_out: string[]; hash: string; demo?: boolean }
export interface Item { text: string; refs: string[]; inference: boolean }
export type Lean = "支持" | "反对" | "相当"
export interface ResearchOutput {
  summary: string; support: Item[]; against: Item[]; to_verify: string[]; invalidation: Item[]; next_review: string[]
  lean: Lean; continuity: { verdict: "首次" | "同向" | "反手" | "失效"; text: string; refs: string[] }
}
export interface PackDiff { added: Fact[]; removed: Fact[]; changed: { id: string; before: string; after: string }[] }
export interface Research {
  id: string; listing_id: string; day: string; profile: string; demo: boolean; trigger: "manual" | "daily"
  pack: Pack; pack_hash: string; output: ResearchOutput; status: "ok" | "需复核"; flags: string[]
  call_id: string | null; model: string | null; cost: number | null; created_at: string
  entry_date: string | null; settled_at: string | null; outcome: "settled" | "gone" | "halt" | null
  return20: number | null; excess20: number | null
  diff?: PackDiff | null // the stock's history: against the research before it
  name?: string | null   // the record of every research (without its pack): the stock's name
}
/** 你导入的 IR 资料: a document you added to a stock — its text kept page by page, the excerpt a research gets. */
export interface IrDoc {
  id: string; listing_id: string; title: string; filename: string; pages: number; chars: number; excerpt: string; page: number
  added_at: string
}
export interface Evaluation {
  groups: { lean: Lean; n: number; settled: number; mean_excess: number | null; median_excess: number | null; win: number | null }[]
  pending: number; hold: number; enough: boolean
}

const GROUPS: [string, string[]][] = [["行情", ["px", "vol", "rel", "val"]], ["结构", ["box"]], ["规则", ["rule"]],
  ["财报", ["fin"]], ["公告", ["ann", "doc"]], ["你导入的资料", ["ir"]], ["你的笔记", ["note"]]]

/** The evidence the way you read it: prices, structures, rules, reports, announcements, your notes. */
export function factGroups(facts: Fact[]): { label: string; facts: Fact[] }[] {
  return GROUPS.map(([label, kinds]) => ({ label, facts: facts.filter((f) => kinds.includes(f.kind)) })).filter((g) => g.facts.length)
}

const short = (f: Fact) => {   // a fact's name: an announcement's title, a report's period, a price's label
  const t = f.kind === "ann" ? f.text.split("：").slice(1).join("：") : f.kind === "doc" ? f.text.split("》")[0].slice(1) : f.text.split(/[（：，]/)[0]
  return t.length > 16 ? `${t.slice(0, 16)}…` : t
}

/** What changed in the evidence since the last research, in a line. */
export function diffLine(d: PackDiff): string {
  const parts = [
    d.added.length ? `新增 ${d.added.length} 条：${d.added.slice(0, 3).map(short).join("、")}${d.added.length > 3 ? " 等" : ""}` : "",
    d.removed.length ? `少了 ${d.removed.length} 条` : "",
    d.changed.length ? `变了 ${d.changed.length} 条` : "",
  ].filter(Boolean)
  return parts.length ? parts.join("；") : "证据没有变化"
}

const line = (label: string, items: Item[]) =>
  items.length ? [`${label}：${items.map((i) => `${i.inference ? "（推断）" : ""}${i.text}`).join("；")}`] : []

/** The research as a note (摘到笔记): its date, model and lean, then what it said; an inference says so. */
export function noteText(r: Research): string {
  const o = r.output
  return [`AI 研究 ${r.day}（${r.model ?? "AI"}，倾向${o.lean}）：${o.summary}`, ...line("支持", o.support), ...line("反对", o.against),
    ...line("失效条件", o.invalidation)].join("\n")
}

/** What followed a research: its 20-day excess over the benchmark once settled. */
export function outcomeText(r: Research): string {
  if (r.demo) return "演示数据不结算"
  if (r.outcome === "gone") return "停止交易"
  if (r.outcome === "halt") return "首日没成交"
  return r.outcome === "settled" ? fmtPct(r.excess20, 2) : "待结算"
}

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) })
const q = (lid: string) => `listing_id=${encodeURIComponent(lid)}`
export const researchEndpoints = {
  pack: (lid: string, refresh = false) => api<Pack>(`/research/pack?${q(lid)}${refresh ? "&refresh=true" : ""}`),
  history: (lid: string) => api<Research[]>(`/research?${q(lid)}`),
  run: (lid: string) => post<Research>("/research", { listing_id: lid }),
  recent: () => api<{ records: Research[]; evaluation: Evaluation }>("/research/recent"),
}

export const irEndpoints = {
  list: (lid: string) => api<IrDoc[]>(`/docs/ir?${q(lid)}`),
  one: (id: string) => api<{ doc: IrDoc; pages: string[] }>(`/docs/ir/${id}`),
  add: (lid: string, file: File) => api<IrDoc>(`/docs/ir?${q(lid)}&filename=${encodeURIComponent(file.name)}`,
    { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } }),
  excerpt: (id: string, excerpt: string, page: number) => api<IrDoc>(`/docs/ir/${id}`, { method: "PUT", body: JSON.stringify({ excerpt, page }) }),
  remove: (id: string) => api<{ ok: boolean }>(`/docs/ir/${id}`, { method: "DELETE" }),
}
export const useIrDocs = (lid: string) => useQuery({ queryKey: ["ir", lid], queryFn: () => irEndpoints.list(lid) })

export const usePack = (lid: string | null, on = true) =>
  useQuery({ queryKey: ["research", "pack", lid], queryFn: () => researchEndpoints.pack(lid!), enabled: !!lid && on })
export const useResearchHistory = (lid: string | null, on = true) =>
  useQuery({ queryKey: ["research", "history", lid], queryFn: () => researchEndpoints.history(lid!), enabled: !!lid && on })
export const useResearchRecent = () => useQuery({ queryKey: ["research", "recent"], queryFn: researchEndpoints.recent })

/** A new research of the stock: its history and the record are fetched again. */
export function useRunResearch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: researchEndpoints.run,
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["research", "history", r.listing_id] })
      void qc.invalidateQueries({ queryKey: ["research", "recent"] })
      void qc.invalidateQueries({ queryKey: ["ai"] })
    },
  })
}
