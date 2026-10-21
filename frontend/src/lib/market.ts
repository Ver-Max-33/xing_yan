// 市场环境与板块强度 (spec §7.3): a market's breadth by day, its indexes' state and its industries; and the 题材 tags
// you put on stocks. A count the data cannot give (limit flags in a test profile) comes as null: shown as 未知.
import { useQuery } from "@tanstack/react-query"
import { api, type Market } from "./api"
import type { SessionState } from "./live"

export interface BreadthDay {
  date: string; total: number; up: number; down: number
  limit_up: number | null; broken: number | null; limit_down: number | null; high60: number; low60: number
}
export interface IndexState {
  index_id: string; date: string; close: number; chg: number | null; ma60: number | null; ma200: number | null
  dist60: number | null; dist200: number | null; state: -1 | 0 | 1 | null
  box: { scale: string; start: string; end: string | null; top: number; bottom: number; width: number; ended: boolean; end_reason: string | null } | null
}
export interface Sector {
  industry: string; members: number; up_ratio: number | null; chg: number | null; ret5: number | null; ret20: number | null
  limit_up: number | null; small: boolean; rank5: number | null; rank20: number | null
}
export interface MarketView {
  day: string | null; breadth: BreadthDay[]; streaks: Record<"1" | "2" | "3" | "4" | "5+", number> | null
  sectors: Sector[]; indexes: IndexState[]; known: { limits: boolean; industry: boolean }
}

export const STATE_NAME: Record<string, [string, string]> = {
  "1": ["上升", "text-up"], "0": ["震荡", "text-muted-foreground"], "-1": ["下降", "text-down"], null: ["未知", "text-subtle-foreground"],
}

const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) })
export const marketEndpoints = {
  view: (market: Market) => api<MarketView>(`/market/${market}`),
  allTags: () => api<{ tag: string; n: number }[]>("/tags"),
  tags: (listingId: string) => api<string[]>(`/tags/${encodeURIComponent(listingId)}`),
  setTags: (listingId: string, tags: string[]) => put<string[]>(`/tags/${encodeURIComponent(listingId)}`, { tags }),
}

export const useMarketView = (market: Market, scanned: string) =>
  useQuery({ queryKey: ["market", market, scanned], queryFn: () => marketEndpoints.view(market), staleTime: 5 * 60_000 })
export const useAllTags = () => useQuery({ queryKey: ["tags"], queryFn: marketEndpoints.allTags })
export const useTags = (listingId: string | undefined) =>
  useQuery({ queryKey: ["tags", listingId], queryFn: () => marketEndpoints.tags(listingId!), enabled: !!listingId })

// ---- 盘中看板 (design 2026-10-12): the board, the day's record and the words around them ---------------------------
export interface Limits {
  known: boolean; limit_up: number | null; broken: number | null; limit_down: number | null; seal_rate: number | null
  pool: number | null; promoted: number | null; promo_rate: number | null; height: number | null; top_sealed: boolean | null
}
export interface LadderName { listing_id: string; name: string | null; sealed: boolean; chg: number | null }
export interface LadderLevel { level: number; sealed: number; broken: number; names: LadderName[]; more: number }
export interface LimitStock { listing_id: string; name: string | null; level: number | null; sealed: boolean; chg: number | null }
/** 一直强 / 转强 / 转弱 / 一直弱 (redesign §3): the last 5 bars against the market, read with the 15 before them. */
export type Tag = "一直强" | "转强" | "转弱" | "一直弱"
export interface Industry {
  name: string; members: number; traded: number; up: number; chg: number | null; up_ratio: number | null; limit_up: number | null
  amount_ratio: number | null
  rel5: number | null; prior15: number | null; rel20: number | null   // against the market: the last 5 bars, the 15 before, 20
  tag: Tag | null; rank: number | null; parent: string | null; small: boolean   // parent: a 二级's 一级
  leaders: { listing_id: string; name: string | null; chg: number | null; sealed: boolean }[]
}
export interface Top { level: "l1" | "l2"; name: string; chg: number; up_ratio: number | null; second: number | null }
export interface IndexNow { last: number; chg: number | null; dist60: number | null; state: -1 | 0 | 1 | null; to_top: number | null; to_bottom: number | null }
export interface Board {
  at: string; day: string; traded: number; up: number; down: number; flat: number; amount: number; projected: number | null
  limits: Limits; ladder: LadderLevel[]; limit_list: LimitStock[]
  industries: { l1: Industry[]; l2: Industry[] }; top: Top | null; top1: { name: string; chg: number; second: number | null } | null
  indexes: Record<string, IndexNow>
  impossible?: number   // a live board's quotes left out: prices beyond today's limit prices (not trades)
}
export interface Read { label: "偏强" | "偏弱" | "持平"; better: string[]; worse: string[] }
export interface PhaseSummary {
  as_of: string; phase: string; days: number; pending: string | null; path: { phase: string; days: number }[]
  recent: { date: string; phase: string; limit_up: number | null; up_ratio: number; net_high: number }[]
}
export interface MarketEvent { i: number; at: string; kind: string; tone: "up" | "warn" | "neutral"; title: string; detail: string }
export interface TapeRow {
  i: number; at: string; traded: number; up: number; down: number; flat: number; amount: number; limit_up: number | null; broken: number | null
  limit_down: number | null; seal_rate: number | null; pool: number | null; promoted: number | null; promo_rate: number | null
  height: number | null; top_sealed: boolean | null; top_name: string | null; top_chg: number | null; second_chg: number | null; sh_last: number | null
}
export type BoardMode = "live" | "auction" | "auction_result" | "close"
export interface BoardResponse {
  market: Market; mode: BoardMode; session: { state: SessionState; label: string; day: string; time: string }
  as_of: string | null; stale: boolean; why_not: string | null; error: string | null; fetched_at: string | null; board: Board | null
  read: Read | null; phase: PhaseSummary | null; events: MarketEvent[]; yesterday: TapeRow | null
  amount_before: number | null   // the whole market's amount the day before
}
/** Each industry's standing as the 市场 page shows it now (redesign §6, §8): 选股's column and the K-line's chip. */
export type Standing = Pick<Industry, "name" | "chg" | "rank" | "tag" | "rel5" | "prior15" | "rel20" | "limit_up" | "parent" | "small">
export interface Standings {
  market: Market; mode: BoardMode; day: string | null; time: string | null   // time: the live board's "HH:MM"
  of: { l1: number; l2: number }; l1: Standing[]; l2: Standing[]
}
export interface TapeResponse {
  day: string; rows: TapeRow[]; prev_day: string | null; prev_rows: { i: number; limit_up: number | null; sh_last: number | null }[]
  index: { prev: number; sum59: number | null; sum199: number | null; ma60_prev: number | null; ma60_5: number | null; box: { top: number; bottom: number } | null } | null
}

/** The board moves while the market is open: asked every 10 s while the page is in front, once a minute otherwise. */
export const boardInterval = (d: BoardResponse | undefined) =>
  typeof document !== "undefined" && document.visibilityState === "visible" && d && d.session.state !== "closed" ? 10_000 : 60_000
export const useBoard = (market: Market) =>
  useQuery({ queryKey: ["market", "board", market], queryFn: () => api<BoardResponse>(`/market/${market}/board`), refetchInterval: (q) => boardInterval(q.state.data) })
export const useTape = (market: Market, enabled = true) =>
  useQuery({ queryKey: ["market", "tape", market], queryFn: () => api<TapeResponse>(`/market/${market}/tape`), refetchInterval: 60_000, enabled })
/** The industries' standings (选股, K 线): every 30 s while the market shows live numbers, else every 5 minutes. */
export const useIndustries = (market: Market | null | undefined, enabled = true) =>
  useQuery({
    queryKey: ["market", "industries", market], enabled: enabled && !!market, staleTime: 20_000,
    queryFn: () => api<Standings>(`/market/${market}/industries`),
    refetchInterval: (q) => (q.state.data && (q.state.data.mode === "live" || q.state.data.mode === "auction_result") ? 30_000 : 300_000),
  })
/** When the market board last fetched (the feed's `board` events): the K-line's forming bar refreshes with it. */
export const useBoardAt = (market: Market | null) =>
  useQuery<string | null>({ queryKey: ["market", "boardAt", market], queryFn: () => null, enabled: false, staleTime: Infinity })
