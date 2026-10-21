// 股票资料 · 公司 (spec §10.3, backend wb/docs/profile.py): who the company is, how big, who holds it and what to watch,
// each part led by one line. A-shares from Tushare, asked once a day; what was announced by the day asked.
import { useQuery } from "@tanstack/react-query"
import { api } from "./api"

export interface Holder {
  name: string; type: string | null; group: string; ratio: number | null; shares: number | null; change: number
  state: "新进" | "增持" | "减持" | "不变"
}
export interface HolderTable { period: string; known: string; rows: Holder[]; left: string[]; sum: number }
export interface Segment { name: string; sales: number | null; share: number | null; margin: number | null }
export interface Profile {
  listing_id: string; as_of: string; source?: string; fetched_at?: string
  unavailable: string | null; errors: Record<string, string>
  company?: {
    name: string | null; main_business: string | null; introduction: string | null; region: string | null; setup_date: string | null
    employees: number | null; chairman: string | null; website: string | null; industry: string | null; industry2?: string | null   // 申万一级 / 二级 on the day
    segments: { period: string; known: string; items: Segment[] } | null
  }
  scale?: { date: string; total_mv: number | null; circ_mv: number | null; pe_ttm: number | null; pb: number | null; dv_ttm: number | null } | null
  holders?: {
    top10: HolderTable | null; float10: HolderTable | null; groups: Record<string, number>
    numbers: { period: string; known: string; number: number | null; change: number | null }[]
    foreign: { hk: { date: string; ratio: number | null; shares: number | null; series: { date: string; ratio: number | null }[] } | null
               named: { name: string; ratio: number | null }[] }
  }
  risk?: {
    pledge: { date: string; ratio: number | null; count: number | null } | null
    unlocks: { date: string; shares: number; ratio: number; holders: number; types: string[] }[]
    trades: { ann_date: string; holder: string; holder_type: string; direction: "增持" | "减持"; shares: number | null; ratio: number | null
              after_ratio: number | null; avg_price: number | null; from: string | null; to: string | null }[]
  }
  finance?: { period: string; period_name: string; known: string; gross_margin: number | null; net_margin: number | null; roe: number | null
              revenue_yoy: number | null; profit_yoy: number | null; debt_to_assets: number | null } | null
  summary?: Record<"company" | "scale" | "holders" | "risk" | "finance", string>
}

export const profileEndpoint = (lid: string, refresh = false) =>
  api<Profile>(`/profile?listing_id=${encodeURIComponent(lid)}${refresh ? "&refresh=true" : ""}`)

export const useProfile = (lid: string | null, on = true) =>
  useQuery({ queryKey: ["profile", lid], queryFn: () => profileEndpoint(lid!), enabled: !!lid && on, staleTime: 10 * 60_000 })

/** 亿 for large amounts, 万 below (元 or 股). */
export function yi(v: number | null | undefined, unit = ""): string {
  if (v === null || v === undefined) return "—"
  return Math.abs(v) >= 1e8 ? `${(v / 1e8).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿${unit}`
    : `${(v / 1e4).toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 万${unit}`
}
