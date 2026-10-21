// Pure data helpers for the chart: backend rows -> KLineData, and drawing anchors between
// display prices (qfq/hfq/raw) and the stored (date, raw price) form (spec §8.2).
import type { KLineData } from "klinecharts"
import type { Bar, DrawingPoint } from "@/lib/api"

/** A KLineData bar that also carries every backend field (indicators, limits, scale). */
export type WbBar = KLineData & Omit<Bar, "volume">

export const DAY_MS = 86_400_000

export function toKLine(rows: Bar[]): WbBar[] {
  return rows.map((r) => ({ ...r, timestamp: r.t, volume: r.volume ?? undefined, turnover: r.amount ?? undefined }))
}

/** A new minute's last two bars against the chart's last one (`end`): the bars to put in, from the chart's last one on —
 *  it again (the same day a minute on, or the official bar once it is published), then the one after it if any;
 *  "reload" when the chart's last bar is not among them (a week's or a month's bar that took today's date, or a day
 *  that has gone); null with nothing to place. */
export function liveStep<T extends { timestamp: number }>(end: number, rows: T[]): T[] | "reload" | null {
  if (!rows.length) return null
  const i = rows.findIndex((r) => r.timestamp === end)
  return i < 0 ? "reload" : rows.slice(i)
}

/** The bars that are final: without today's bar still forming (spec §8.5) — a sample box never stands on a process value. */
export const settled = <T extends { temp?: boolean }>(bars: T[]): T[] => bars.filter((b) => !b.temp)

export const isoDate = (ts: number) => new Date(ts).toISOString().slice(0, 10)

/** A minute chart's time axis: no tick past the last bar (klinecharts labels the slot after it: 16:00 after a 60-minute
 *  chart's 15:00, a time that never trades) nor within 40 px of the left edge (its label, drawn centred, is cut there);
 *  the first tick and each day's first say the day ("10-12 09:35"), as 09:35 on two days reads the same. */
export function minuteTicks<T extends { coord: number; value: number | string; text: string }>(ticks: T[], last: number | undefined): T[] {
  const kept = ticks.filter((t) => t.coord >= 40 && (last === undefined || Number(t.value) <= last))
  return kept.map((t, i) => {
    const day = isoDate(Number(t.value))
    return i === 0 || isoDate(Number(kept[i - 1].value)) !== day ? { ...t, text: `${day.slice(5)} ${t.text}` } : t
  })
}
/** No tick past the last bar (a daily chart labelled its empty room with days to come: 2026-11-06 — and, in a replay,
 *  days after the replay day). */
export const upTo = <T extends { value: number | string }>(ticks: T[], last: number | undefined): T[] =>
  last === undefined ? ticks : ticks.filter((t) => Number(t.value) <= last)

/** The week (its Monday) or month a bar belongs to, as the backend groups them (ISO weeks, calendar months); a day's bar
 *  its own timestamp. A stock's week is dated by its own last trading day, an index's by the index's: a week the stock did
 *  not trade on its last day has to be matched by the week, not the date (大盘副图). */
export function bucketOf(ts: number, period: string): number {
  if (period !== "week" && period !== "month") return ts
  const d = new Date(ts)
  return period === "month" ? Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) : ts - ((d.getUTCDay() + 6) % 7) * DAY_MS
}

export const dateTs = (d: string) => Date.UTC(Number(d.slice(0, 4)), Number(d.slice(5, 7)) - 1, Number(d.slice(8, 10)))
/** A bar's date as the chart's timestamp: a day at its midnight, a minute bar ("YYYY-MM-DDTHH:MM", its end) at that minute. */
export const tsOf = (d: string) => (d.length > 10 ? Date.parse(`${d}:00Z`) : dateTs(d))

/** Index of the first bar whose timestamp is >= ts (bars sorted ascending); bars.length if none. */
export function lowerBound(bars: { timestamp: number }[], ts: number): number {
  let lo = 0
  let hi = bars.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (bars[mid].timestamp < ts) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** The bar a date belongs to: the same day for daily bars, the containing week/month otherwise
 *  (those bars are dated by their last trading day). Past the last bar -> the last bar. */
export function barFor(bars: WbBar[], ts: number): WbBar | undefined {
  if (bars.length === 0) return undefined
  return bars[Math.min(lowerBound(bars, ts), bars.length - 1)]
}

/** Daily scale change points [date, display/raw] from /api/bars. A stored anchor converts with the
 *  scale of its own day: a week bar carries its last day's scale, which is wrong for earlier days
 *  of that week whenever an ex-date falls inside it. */
export type Scales = [string, number][]

export function scaleAt(scales: Scales, date: string): number {
  if (scales.length === 0) return 1
  let s = scales[0][1] // before the first change point the first scale applies
  for (const [d, v] of scales) {
    if (d > date) break
    s = v
  }
  return s
}

/** Display point -> stored anchor: the raw price on that date. */
export function toRawPoint(p: { timestamp?: number; value?: number }, scales: Scales): DrawingPoint | null {
  if (p.timestamp === undefined || p.value === undefined) return null
  const date = isoDate(p.timestamp)
  return { date, price: p.value / scaleAt(scales, date) }
}

/** Stored anchor -> display point on the current basis and period. An anchor before the first
 *  loaded bar keeps its own date (the chart places points by timestamp even off the loaded range). */
export function toDisplayPoint(bars: WbBar[], p: DrawingPoint, scales: Scales): { timestamp: number; value: number } {
  const ts = dateTs(p.date)
  const bar = bars.length && ts >= bars[0].timestamp ? barFor(bars, ts) : undefined
  return { timestamp: bar && bar.timestamp >= ts ? bar.timestamp : ts, value: p.price * scaleAt(scales, p.date) }
}

/** After a drag, re-anchor only the points that moved. An untouched point may sit on a week bar
 *  (dated Friday) while its anchor is Tuesday: converting it back would move the anchor. */
export function reanchor(stored: DrawingPoint[], shown: { timestamp: number; value: number }[],
  now: { timestamp?: number; value?: number }[], scales: Scales): DrawingPoint[] {
  return now.flatMap((p, i) => {
    const o = shown[i]
    if (o && stored[i] && p.timestamp === o.timestamp && p.value !== undefined && Math.abs(p.value - o.value) < 1e-9) {
      return [stored[i]]
    }
    const a = toRawPoint(p, scales)
    return a ? [a] : []
  })
}
