// 指标库 (design 2026-10-10): the catalog, an indicator's text, the check, saving, and an indicator over a chart's
// own bars (backend wb/indicators). The editor's form and its YAML (toYaml / fromYaml) are one indicator.
import { useQuery } from "@tanstack/react-query"
import { api, type Basis, type Market, type Period } from "./api"

export type IndicatorGroup = "common" | "setup" | "ict" | "mine"
export type Pane = "auto" | "main" | "sub"
export interface IndParam { id: string; value: number; range?: [number, number] | null; step?: number | null }
export interface IndicatorSummary {
  id: string; name: string; doc: string; group: IndicatorGroup; pane: Pane; markets: Market[]; builtin: boolean
  version: number; updated_at: string | null; archived: boolean; params: IndParam[]; levels: number[]; source: string
  error: string | null
}
export interface Indicator {
  id: string; name: string; doc: string; group: IndicatorGroup; pane: Pane; params: IndParam[]; levels: number[]
  markets: Market[]; source: string; formula: string; version: number
}
export interface IndicatorDoc { id: string; version: number; text: string; indicator: Indicator; builtin: boolean }

/** One thing an indicator draws: its parts (`cols`: value; cond, price, p1, p2, top, bottom, v1, v2) by output. */
export interface DrawItem {
  kind: "line" | "dots" | "stick" | "colorstick" | "volstick" | "linestick" | "stickline" | "icon" | "text" | "number"
    | "band" | "zone" | "level"
  name: string; cols: Record<string, string>; color: string | null; color2: string | null; width: number; dash: boolean
  hidden: boolean; icon: number | null; text: string | null; empty: boolean; end: number; maxn: number; price: boolean
}
export interface Zone { item: number; s: number; at: number; e: number | null; top: number; bottom: number }
export interface Level { item: number; s: number; at: number; e: number | null; price: number }
export interface IndicatorSeries {
  id: string; version: number; name: string; error?: string
  pane?: "main" | "sub"; items?: DrawItem[]; t?: number[]; values?: Record<string, (number | boolean | null)[]>
  zones?: Zone[]; levels?: Level[]; levels_ref?: number[]
}
export interface CheckResult {
  ok: boolean; errors: { line: number; message: string }[]; unsupported: { why: string; names: string[] }[]
  reduced: string | null; items: { kind: DrawItem["kind"]; name: string; color: string | null; hidden: boolean; line: number }[]
  pane?: "main" | "sub"
  sample: { listing_id: string; error?: string; rows?: Record<string, number | null>[]; zones?: number; levels?: number } | null
  indicator: Indicator | null   // the text read (an imported YAML fills the form from it)
}

export const GROUP_NAME: Record<IndicatorGroup, string> = { common: "常用指标", setup: "组合设置", ict: "ICT 概念", mine: "我的指标" }

/** A group's order (the library page, the K 线 menu): over the candles first, then the panes below, by name — yours as made. */
export function ordered(rows: IndicatorSummary[], group: IndicatorGroup): IndicatorSummary[] {
  const of = rows.filter((r) => r.group === group)
  return group === "mine" ? of
    : of.sort((a, b) => Number(a.pane === "sub") - Number(b.pane === "sub") || a.name.localeCompare(b.name, "zh"))
}

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) })
const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) })
const qs = (p: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => [k, String(v)])).toString()

export const indicatorEndpoints = {
  catalog: () => api<{ groups: Record<IndicatorGroup, string>; indicators: IndicatorSummary[] }>("/indicators"),
  get: (id: string) => api<IndicatorDoc>(`/indicators/${encodeURIComponent(id)}`),
  check: (text: string, listing_id?: string | null, market?: Market) =>
    post<CheckResult>("/indicators/check", { text, listing_id, market }),
  /** `create`: a copy keeps its own id; refused (409) when that id is taken */
  save: (text: string, create = false) => post<{ id: string; version: number; created: boolean }>("/indicators", { text, create }),
  setState: (id: string, archived: boolean) => put<{ ok: boolean }>(`/indicators/${encodeURIComponent(id)}/state`, { archived }),
  series: (listing_id: string, id: string, period: Period, basis: Basis, live: boolean) =>
    api<IndicatorSeries>(`/indicator-series?${qs({ listing_id, id, period, basis, live: live || undefined })}`),
}

/** The catalog, fresh whenever a page that shows it opens. */
export const useIndicators = () => useQuery({ queryKey: ["indicators"], queryFn: indicatorEndpoints.catalog, refetchOnMount: "always" })
export const useIndicator = (id: string | undefined) =>
  useQuery({ queryKey: ["indicator", id], queryFn: () => indicatorEndpoints.get(id!), enabled: !!id && id !== "new" })

// ---- the editor's form as YAML ------------------------------------------------------------------------------
export interface IndicatorForm {
  id: string; name: string; doc: string; pane: Pane; params: IndParam[]; levels: string; markets: Market[]; source: string
  formula: string
}
export const EMPTY_FORM: IndicatorForm = {
  id: "", name: "", doc: "", pane: "auto", params: [], levels: "", markets: ["CN", "JP"], source: "", formula: "",
}

const quote = (s: string) => JSON.stringify(s)   // a JSON string is a YAML string: any text, safely
const num = (v: number) => (Number.isFinite(v) ? String(v) : "0")

/** The form as the YAML the backend keeps (wb/indicators): every free text quoted, the formula a literal block. */
export function toYaml(f: IndicatorForm): string {
  const lines = [...(f.id ? [`id: ${f.id}`] : []), `name: ${quote(f.name.trim())}`]
  if (f.doc.trim()) lines.push(`doc: ${quote(f.doc.trim())}`)
  lines.push(`pane: ${f.pane}`)
  if (f.params.length) {
    lines.push("params:")
    for (const p of f.params) {
      const range = p.range ? `, range: [${num(p.range[0])}, ${num(p.range[1])}]` : ""
      const step = p.step ? `, step: ${num(p.step)}` : ""
      lines.push(`  - {id: ${quote(p.id.trim())}, value: ${num(p.value)}${range}${step}}`)
    }
  }
  const levels = parseLevels(f.levels)
  if (levels.length) lines.push(`levels: [${levels.join(", ")}]`)
  lines.push(`markets: [${f.markets.join(", ")}]`)
  if (f.source.trim()) lines.push(`source: ${quote(f.source.trim())}`)
  const body = f.formula.replace(/\r\n?/g, "\n").replace(/\s+$/, "")
  // |2: the block's indent said, not guessed from its first line (a formula may begin with spaces)
  lines.push("formula: |2", ...(body ? body.split("\n").map((l) => `  ${l}`) : ["  "]))
  return `${lines.join("\n")}\n`
}

export const parseLevels = (s: string): number[] =>
  s.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean).map(Number).filter((x) => Number.isFinite(x))

export function fromIndicator(ind: Indicator): IndicatorForm {
  return { id: ind.id, name: ind.name, doc: ind.doc, pane: ind.pane, params: ind.params.map((p) => ({ ...p })),
    levels: ind.levels.join(", "), markets: [...ind.markets], source: ind.source, formula: ind.formula.replace(/\n$/, "") }
}

/** 粘贴通达信公式: a bare formula (no `formula:` key) is the formula of a new indicator; its parameters, unknown
 *  names the check will name, are the user's to add. YAML (an export) is read as it is, on the server. */
export const looksLikeYaml = (text: string) => /^\s*(name|formula|id)\s*:/m.test(text) && /^\s*formula\s*:/m.test(text)
