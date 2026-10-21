// 数据使用开关 (spec §4.10): each data source's four uses — kept here, sent to an LLM, in notifications, exported — and
// where each market's prices come from in its profile now. A CSV leaves out the rows of a market whose source is kept
// from export, and says which.
import { type QueryClient, useQuery } from "@tanstack/react-query"
import { toast } from "sonner"
import { api, type Market } from "./api"
import { MARKET_NAME } from "./format"

export interface DataSource { id: string; name: string; store: boolean; llm: boolean; notify: boolean; export: boolean }
export interface DataUse { sources: DataSource[]; markets: Record<Market, string> }
export const USES: [keyof Omit<DataSource, "id" | "name">, string, string][] = [
  ["store", "本地存储", "关掉就不从这个来源取数据（取来的都存在本机）"],
  ["llm", "发送给 LLM", "关掉以后证据包里没有它算出的事实，AI 研究会写“资料不足”"],
  ["notify", "出现在通知中", "关掉以后飞书消息里不写它的价格"],
  ["export", "导出", "关掉以后导出的 CSV 不含这个市场的行"],
]

const query = { queryKey: ["datause"], queryFn: () => api<DataUse>("/datause") }
export const useDataUse = () => useQuery(query)
/** The switches as they are now, loaded if they were not (an export must not go before it knows). */
export const loadDataUse = (qc: QueryClient) => qc.fetchQuery({ ...query, staleTime: 0 })

/** The rows a CSV may carry: a market whose source is kept from export is left out and named ("日股（J-Quants）"). */
export function forExport<T>(d: DataUse | undefined, rows: T[], market: (r: T) => Market): { rows: T[]; left: string[] } {
  if (!d) return { rows, left: [] }
  const kept = (m: Market) => d.sources.find((s) => s.id === d.markets[m])?.export === false
  const left = (Object.keys(d.markets) as Market[]).filter((m) => kept(m) && rows.some((r) => market(r) === m))
  return {
    rows: rows.filter((r) => !kept(market(r))),
    left: left.map((m) => `${MARKET_NAME[m]}（${d.sources.find((s) => s.id === d.markets[m])?.name ?? d.markets[m]}）`),
  }
}

/** The rows to write into a CSV under the switches: the markets left out said once; null when nothing is left. */
export async function exportRows<T>(qc: QueryClient, rows: T[], market: (r: T) => Market): Promise<T[] | null> {
  const { rows: kept, left } = forExport(await loadDataUse(qc), rows, market)
  if (left.length) toast.warning(`按 设置 → 数据使用，${left.join("、")}的行没有导出`)
  if (!kept.length) {
    toast.error("没有可以导出的行")
    return null
  }
  return kept
}
