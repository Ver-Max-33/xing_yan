// 危险提醒 (design 2026-10-15): the warnings the backend words, for the stocks a page lists and for one stock's chart.
import { useQuery } from "@tanstack/react-query"
import { api, type Market } from "./api"

export type DangerKind = "unlock" | "limit_up" | "strong_burst" | "weak_spike" | "retail"
/** A warning (10-17, plain words): its name says the risk, `reason` this stock's numbers, `risk` what followed in ten years. */
export interface Danger { kind: DangerKind; label: string; date: string; days: number; new: boolean; predicted: boolean; reason: string; risk: string; brief: string }
export interface DangerAnswer { covered: boolean; why: string | null; day: string | null; warnings: Record<string, Danger[]>
  unknown: Record<string, DangerKind[]>; uncovered: Record<string, string>; unlocks_from: string | null; history_from: string | null }
export interface DangerEvent { date: string; kind: DangerKind; label: string }

/** The warnings of the stocks a page lists (one market): one request, the ids sorted so a reorder asks nothing new. */
export function useDanger(market: Market | null | undefined, ids: string[], day?: string | null) {
  const key = [...new Set(ids)].sort()
  return useQuery({
    queryKey: ["danger", market, day ?? null, key.join(",")],
    queryFn: () => api<DangerAnswer>("/danger", { method: "POST", body: JSON.stringify({ market, ids: key, day: day ?? null }) }),
    enabled: !!market && key.length > 0, staleTime: 60_000, placeholderData: (prev) => prev,
  })
}

export function useDangerEvents(listingId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ["danger-events", listingId],
    queryFn: () => api<{ covered: boolean; why: string | null; events: DangerEvent[] }>(`/danger/events?listing_id=${encodeURIComponent(listingId!)}`),
    enabled: !!listingId && enabled, staleTime: 300_000,
  })
}

/** A warning's key in a list: a stock can have two unlocks in one window, on different days. */
export const dangerKey = (w: Danger) => `${w.kind}-${w.date}`

/** What a stock's tags say on hover: each warning's name, why, and what may follow. */
export const dangerTitle = (ws: Danger[]) => ws.map((w) => `${w.label}\n原因：${w.reason}\n可能的风险：${w.risk}`).join("\n\n")

/** What a stock's block says besides its warnings: the money flow it lacks, and what a replayed day cannot know (the
 *  kept history covers the last two years; unlocks were stored from the day this began). */
export function dangerNotes(a: DangerAnswer, listingId: string): string[] {
  const out: string[] = []
  if (a.history_from && a.day && a.day < a.history_from) out.push(`${a.history_from} 以前的危险提醒没有算（只留最近两年）`)
  if (a.unlocks_from && a.day && a.day < a.unlocks_from) out.push(`${a.unlocks_from} 以前的解禁没有存，这一天只按上市日推算首发一年期`)
  if ((a.unknown[listingId] ?? []).includes("retail")) out.push("资金流向不全，散户接盘没法判断")
  return out
}
