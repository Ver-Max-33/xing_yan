// Number formatting for a dense trading UI: monospace, right-aligned, no locale surprises.
import type { Market } from "./api"

/** The markets as the pages name them (codes stay for compact places: the status bar, a stock's code). */
export const MARKET_NAME: Record<Market, string> = { CN: "A 股", JP: "日股" }
export const MARKETS: Market[] = ["CN", "JP"]
export const marketNames = (ms: readonly Market[]) => ms.map((m) => MARKET_NAME[m]).join(" + ")

export const pricePrecision = (market: Market) => (market === "CN" ? 2 : 1)

export function fmtPrice(v: number | null | undefined, market: Market): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—"
  return v.toLocaleString("en-US", { minimumFractionDigits: pricePrecision(market), maximumFractionDigits: pricePrecision(market) })
}

export function fmtPct(v: number | null | undefined, digits = 2, sign = true): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—"
  const s = (v * 100).toFixed(digits)
  return `${sign && v > 0 ? "+" : ""}${s}%`
}

/** A rule's value in the unit it reads in (spec §5.6): % a share ×100 (0.184 → 18.4%), 倍 a multiple, 根 bars. */
export function inUnit(v: number | null | undefined, unit = ""): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return ""
  if (unit === "%") return `${(v * 100).toFixed(1)}%`   // one decimal always: a column reads 2.0% beside 1.8%
  const n = Number.isInteger(v) ? String(v) : v.toFixed(2)
  return unit ? `${n} ${unit}` : n
}

/** 1.23亿 / 4,560万 style for A-share volumes and turnover; 1.2M / 3.4B style for Japan. */
export function fmtBig(v: number | null | undefined, market: Market): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—"
  const a = Math.abs(v)
  if (market === "CN") {
    if (a >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
    if (a >= 1e4) return `${(v / 1e4).toFixed(a >= 1e6 ? 0 : 1)}万`
    return v.toFixed(0)
  }
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return v.toFixed(0)
}

/** "2026-10-01" -> "10-01" (the year is noise in a daily status line). */
export const shortDate = (d: string | null | undefined) => (d ? d.slice(5) : "—")

export const marketOf = (listingId: string) => listingId.split(":")[0] as Market
export const codeOf = (listingId: string) => listingId.split(":")[1] ?? listingId

/** CSS class for a signed change: red up / green down by default, swapped by the colour setting. */
export const changeClass = (v: number | null | undefined) =>
  v === null || v === undefined || v === 0 ? "text-muted-foreground" : v > 0 ? "text-up" : "text-down"

const INDEX_NAMES: Record<string, string> = {
  "CN:IDX:000001": "上证指数", "CN:IDX:399001": "深证成指", "CN:IDX:399006": "创业板指", "CN:IDX:000688": "科创50",
  "CN:IDX:899050": "北证50", "CN:IDX:000300": "沪深300", "JP:IDX:N225": "日经225", "JP:IDX:TOPIX": "TOPIX",
  "JP:IDX:PRIME": "东证Prime", "JP:IDX:STANDARD": "东证Standard", "JP:IDX:GROWTH": "东证Growth",
}
/** "CN:IDX:000001" -> 上证指数; an index the table doesn't know shows its code. */
export const indexName = (indexId: string) => INDEX_NAMES[indexId] ?? indexId.split(":").pop() ?? indexId
