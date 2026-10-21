// Thin client for the local backend (spec §3.1). The session token travels as the `wb_token`
// cookie set by Electron (same origin); in `vite dev` the dev proxy adds the header instead.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { MARKET_NAME } from "./format"

export type Market = "CN" | "JP"
/** m1…m60: the K-line page's minute bars (10-12) — only candles, volume and the chart's own indicators on them */
export type MinutePeriod = "m1" | "m5" | "m15" | "m30" | "m60"
export type Period = "day" | "week" | "month" | MinutePeriod
export const isMinute = (p: Period): p is MinutePeriod => /^m\d+$/.test(p)
export const PERIOD_NAME: Record<Period, string> = { day: "日线", week: "周线", month: "月线", m1: "1 分钟线", m5: "5 分钟线",
  m15: "15 分钟线", m30: "30 分钟线", m60: "60 分钟线" }
export type Basis = "qfq" | "hfq" | "raw"
export type Profile = "test" | "prod"

export class ApiError extends Error {
  status: number
  data: unknown // the response body when it was JSON (e.g. an AI answer that failed its checks keeps `raw`)
  constructor(status: number, message: string, data: unknown = null) {
    super(message)
    this.status = status
    this.data = data
  }
  /** The backend's reason as it wrote it (in Chinese), without "Error:" in front. */
  toString() {
    return this.message
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  })
  if (!r.ok) {
    const text = await r.text()
    let reason = text || r.statusText
    let data: unknown = null
    try {
      data = JSON.parse(text)
      const detail = (data as { detail?: unknown }).detail // FastAPI's {"detail": "…"}
      if (typeof detail === "string") reason = detail
    } catch { /* not JSON: keep the text */ }
    throw new ApiError(r.status, reason, data)
  }
  return (await r.json()) as T
}

const qs = (p: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => [k, String(v)])).toString()

// ---- types (mirror wb/api/app.py) ----------------------------------------------------------
export interface SnapshotInfo { id: string; as_of: string; created_at: string; note: string }
export interface MarketStatus {
  market: Market
  profile: Profile
  snapshot: SnapshotInfo | null
  quality: Record<string, number>
  crosscheck: { checked: number; ok: number }
  pending_units: number
  session: { state: "pre" | "trading" | "lunch" | "after" | "closed"; label: string } // where the market is in its day (盯盘)
}
export interface SearchHit {
  listing_id: string; market: Market; code: string; name: string | null; name_en: string | null
  board: string | null; status: string | null
}
export interface Instrument extends SearchHit {
  exchange: string | null; industry: string | null; list_date: string | null; delist_date: string | null
  ind_l1: string | null; ind_l2: string | null   // the app's one industry scheme: 申万 一级 / 二级 (日股: 33 業種) — `industry` is the source's own label
  index_id: string; watch_groups: number[]
  quality: { check_name: string; n: number; first: string | null; last: string | null }[]
}
export interface Bar {
  date: string; t: number
  open: number; high: number; low: number; close: number; volume: number | null; amount: number | null
  pre_close: number | null; up_limit: number | null; down_limit: number | null
  limit_up: boolean; limit_down: boolean; complete: boolean; scale: number
  ma5: number | null; ma10: number | null; ma20: number | null; ma60: number | null; ma60_up: boolean | null
  vol_ma5: number | null; vol_ma10: number | null
  dif: number | null; dea: number | null; macd: number | null; macd_cross: "below" | "above" | null
  macd_cross_ma60: "below" | "above" | null // the same cross by price against MA60 (§8.6 switch)
  k: number | null; d: number | null; j: number | null
  boll_mid: number | null; boll_up: number | null; boll_low: number | null
  amp60: number | null; amp120: number | null; new_high_120: boolean | null; new_high_250: boolean | null
  year_half: number | null; year_year: number | null; year_gap: number | null // 年内向左 (backend)
  vol_mult: number | null; vol_class: "limit_double" | "down_half" | "ground" | null
  turnover_rate: number | null // 换手率 %, a day's (A 股): 筹码分布 spreads it
  temp?: boolean // today's bar still forming (spec §8.5 盘中未确认): asked for with `live`
}
export interface BarsResponse {
  listing_id: string; snapshot_id: string | null; period: Period; basis: Basis; adjusted: boolean
  rows: Bar[]; more: boolean
  scales: [string, number][] // daily (date, display/raw) change points for converting drawing anchors
}
export interface Quote { listing_id: string; name?: string | null; close?: number; change?: number | null; date?: string; at?: string }   // at: in the session, the quote's time
export interface WatchItem extends Quote { note: string; added_at: string }
export interface WatchGroup { id: number; name: string; sort: number; n: number; items: WatchItem[] }
export interface DrawingPoint { date: string; price: number }
export interface Drawing {
  id: string; listing_id: string; kind: string; points: DrawingPoint[]; styles: Record<string, unknown>
  alert: boolean; created_at: string; updated_at: string
}
export type BriefSwitch = "auto" | "on" | "off" // auto: on for a market on the formal profile
export type Settings = {
  "profile.CN": Profile; "profile.JP": Profile; theme: "dark" | "light" | "system"; colors: "red_up" | "green_up"
  "ui.font": "system" | "geist"; "ui.size": number // 设置 → 外观: the UI's font and the root size in px (16: the components' own)
  "live.quiet_minutes": number // 盘中提醒 (spec §7.2)
  // the layers of design 2026-10-10 §3 (竞价、逼近、触线、持有中收盘前、大盘急跌、行情中断), the approach's zone and the market's fall (%)
  "live.auction": boolean; "live.near": boolean; "live.touch": boolean; "live.hold": boolean; "live.crash": boolean; "live.feed": boolean
  "live.zone": number; "live.crash_pct": number
  "market.tape": boolean // 市场 (design 2026-10-12 §8): the live thread's whole-market record of A shares
  "minutes.prefetch": boolean // 分钟线提前取 (10-13): the whole market's 1-minute bars of 60 days, fetched outside the session
  "research.daily": number // 个股研究 (spec §10.3): each market's new signals researched after the daily scan, at most this many
  "notify.live": boolean // 盘中提醒 to Feishu, from a realtime source only (spec §7.1, 1.34)
  // 开盘前简报 (spec §7.1, 1.34): per market on / off and when (its own clock); how much of each section
  "brief.CN": BriefSwitch; "brief.JP": BriefSwitch; "brief.time.CN": string; "brief.time.JP": string
  "brief.per_rule": number; "brief.near_pct": number; "brief.near_max": number; "brief.empty": boolean
} & { [key: `datause.${string}`]: boolean } // 数据使用开关 (spec §4.10)
export type SecretSource = "env" | ".env" | "keychain" | null
export interface Capability {
  provider: string; dataset: string; available: boolean; history_start: string | null
  latest_date: string | null; rate_per_min: number | null; detail: string
  last_error: string | null; last_error_unit: string | null; last_error_at: string | null // spec §4.9 最近一次错误
}
export interface QualityIssue { dataset: string; check_name: string; severity: string; listing_id: string | null; date: string | null; detail: string }
export interface Crosscheck {
  snapshot_id: string | null
  summary: { reference: string; field: string; n: number; ok: number }[]
  mismatches: { reference: string; listing_id: string; date: string; field: string; stored: number; ref_value: number }[]
}

// ---- endpoints ------------------------------------------------------------------------------
export const endpoints = {
  status: () => api<{ markets: MarketStatus[] }>("/status"),
  search: (q: string, limit = 20) => api<SearchHit[]>(`/search?${qs({ q, limit })}`),
  instrument: (id: string) => api<Instrument>(`/instrument/${encodeURIComponent(id)}`),
  /** `live`: end with today's bar still forming while its market's day is open (spec §8.5) */
  bars: (p: { listing_id: string; period: Period; basis: Basis; end?: string; limit?: number; live?: boolean }) =>
    api<BarsResponse>(`/bars?${qs(p)}`),
  indexBars: (p: { index_id: string; period: Period; end?: string; limit?: number }) =>
    api<{ index_id: string; rows: Bar[]; more: boolean }>(`/index_bars?${qs(p)}`),
  watchlist: () => api<WatchGroup[]>("/watchlist"),
  /** `source`: where it was watched from, kept with its 观察条目 (spec §11) */
  addWatch: (groupId: number, listingId: string, source = "") =>
    api(`/watchlist/${groupId}/items`, { method: "POST", body: JSON.stringify({ listing_id: listingId, source }) }),
  /** many at once (选股's 全选 → 加入观察): one request; `added` counts the ones not watched already */
  addWatchMany: (groupId: number, listingIds: string[], source = "") =>
    api<{ ok: boolean; added: number }>(`/watchlist/${groupId}/items`, { method: "POST", body: JSON.stringify({ listing_ids: listingIds, source }) }),
  removeWatch: (groupId: number, listingId: string) =>
    api(`/watchlist/${groupId}/items/${encodeURIComponent(listingId)}`, { method: "DELETE" }),
  addGroup: (name: string) => api<{ id: number }>("/watchlist/groups", { method: "POST", body: JSON.stringify({ name }) }),
  deleteGroup: (id: number) => api(`/watchlist/groups/${id}`, { method: "DELETE" }),
  recent: () => api<Quote[]>("/recent"),
  touchRecent: (listingId: string) => api("/recent", { method: "POST", body: JSON.stringify({ listing_id: listingId }) }),
  drawings: (listingId: string) => api<Drawing[]>(`/drawings?${qs({ listing_id: listingId })}`),
  saveDrawing: (d: Pick<Drawing, "id" | "listing_id" | "kind" | "points" | "styles" | "alert">) =>
    api(`/drawings/${d.id}`, { method: "PUT", body: JSON.stringify(d) }),
  deleteDrawing: (id: string) => api(`/drawings/${id}`, { method: "DELETE" }),
  settings: () => api<Settings>("/settings"),
  putSetting: (key: keyof Settings, value: string | boolean | number) =>
    api<Settings>("/settings", { method: "PUT", body: JSON.stringify({ key, value }) }),
  secrets: () => api<Record<string, SecretSource>>("/secrets"),
  putSecret: (name: string, value: string) =>
    api<Record<string, SecretSource>>(`/secrets/${name}`, { method: "PUT", body: JSON.stringify({ value }) }),
  capabilities: (market: Market) => api<Capability[]>(`/capabilities?${qs({ market })}`),
  snapshots: (market: Market) => api<(SnapshotInfo & { datasets: string[] })[]>(`/data/${market}/snapshots`),
  quality: (market: Market) => api<QualityIssue[]>(`/data/${market}/quality`),
  crosscheck: (market: Market) => api<Crosscheck>(`/data/${market}/crosscheck`),
}

// ---- hooks ----------------------------------------------------------------------------------
export const useStatus = () => useQuery({ queryKey: ["status"], queryFn: endpoints.status, refetchInterval: 60_000 })
/** Whether a market's current data profile holds data — a market may have none while the other has (日股 on 测试档 with
 * nothing in it, 2026-10-11) — and the words for one that holds none. Before the status is in, every market is taken to
 * have data: nothing is hidden on a guess. */
export function useDataIn() {
  const { data } = useStatus()
  const of = (m: Market) => data?.markets.find((x) => x.market === m)
  return {
    has: (m: Market) => !data || !!of(m)?.snapshot,
    none: (m: Market) => `${MARKET_NAME[m]}现在的数据档（${of(m)?.profile === "prod" ? "正式档" : "测试档"}）里还没有数据：`
      + `到 数据 → ${MARKET_NAME[m]}「补历史…」取数据，或在 设置 → 数据 换数据档`,
  }
}
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: endpoints.settings, staleTime: Infinity })
export const useWatchlist = () => useQuery({ queryKey: ["watchlist"], queryFn: endpoints.watchlist })
export const useRecent = () => useQuery({ queryKey: ["recent"], queryFn: endpoints.recent })
export const useInstrument = (id: string | undefined) =>
  useQuery({ queryKey: ["instrument", id], queryFn: () => endpoints.instrument(id!), enabled: !!id })

export function usePutSetting() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ key, value }: { key: keyof Settings; value: string | boolean | number }) => endpoints.putSetting(key, value),
    // shown at once (a switch flips when you click it); put back as stored if the backend refuses
    onMutate: ({ key, value }) => qc.setQueryData<Settings>(["settings"], (s) => (s ? { ...s, [key]: value } : s)),
    onError: () => void qc.invalidateQueries({ queryKey: ["settings"] }),
    onSuccess: (s) => {
      qc.setQueryData(["settings"], s)
      void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "settings" })
    },
  })
}

/** In or out of a group; a stock in a group is watched (its 观察条目), out of every group it is closed. */
export function useToggleWatch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ groupId, listingId, add, source }: { groupId: number; listingId: string; add: boolean; source?: string }) =>
      add ? endpoints.addWatch(groupId, listingId, source) : endpoints.removeWatch(groupId, listingId),
    onSuccess: (_d, v) => {
      void qc.invalidateQueries({ queryKey: ["watchlist"] })
      void qc.invalidateQueries({ queryKey: ["watch"] })
      void qc.invalidateQueries({ queryKey: ["instrument", v.listingId] })
    },
  })
}
