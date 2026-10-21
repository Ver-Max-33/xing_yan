// The 市场 board's number words and small helpers (design 2026-10-12), apart from its components.
import { useEffect, useRef, useState } from "react"
import type { LadderLevel, MarketEvent } from "@/lib/market"

export const count = (v: number | null | undefined) => (v == null ? "未知" : v.toLocaleString("en-US"))
export const rate = (v: number | null | undefined) => (v == null ? "未知" : `${Math.round(v * 100)}%`)
/** 6,070 亿 / 1.18 万亿: A-share turnover the way it is talked about. */
export function yuan(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—"
  return v >= 1e12 ? `${(v / 1e12).toFixed(2)} 万亿` : `${Math.round(v / 1e8).toLocaleString("en-US")} 亿`
}
/** 8.79 兆円 / 4,120 億円: the Tokyo market's turnover the way it is talked about. */
export function yen(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—"
  return v >= 1e12 ? `${(v / 1e12).toFixed(2)} 兆円` : `${Math.round(v / 1e8).toLocaleString("en-US")} 億円`
}
/** The clock time of the record's minute i (1 … 240; 120 is 11:30, 240 is 15:00). */
export function minuteLabel(i: number): string {
  const m = i <= 120 ? 9 * 60 + 30 + i : 13 * 60 + (i - 120)
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`
}
/** The exchange's clock time of an ISO-like string ("2026-10-12T10:42:30" → "10:42"). */
export const hm = (t: string) => t.slice(11, 16)

export const DOT: Record<string, string> = { up: "bg-up", warn: "bg-warning", neutral: "bg-muted-foreground" }

const GAP = 3   // minutes without a row: a line breaks there rather than drawing through what was not recorded

/** An SVG path through (minute, value) points — straight, or in steps for counts — broken where minutes are missing. */
export function linePath(points: [number, number][], x: (i: number) => number, y: (v: number) => number, step = false): string {
  const n = (v: number) => String(Math.round(v * 10) / 10)
  return points.map(([i, v], k) => {
    if (!k || i - points[k - 1][0] > GAP) return `M${n(x(i))} ${n(y(v))}`
    return step ? `H${n(x(i))}V${n(y(v))}` : `L${n(x(i))} ${n(y(v))}`
  }).join("")
}

export const eventKey = (e: MarketEvent) => `${e.kind}-${e.i}-${e.title}`

export type LadderRow = { kind: "level"; level: LadderLevel } | { kind: "gap"; from: number; to: number }
/** 连板天梯's rows (redesign §2.5): empty levels from three boards up, one after another, fold into one line ("9–7 板 没有"). */
export function ladderRows(ladder: LadderLevel[]): LadderRow[] {
  const out: LadderRow[] = []
  for (const l of ladder) {
    const empty = l.level >= 3 && l.sealed + l.broken === 0
    const last = out.at(-1)
    if (empty && last?.kind === "gap" && last.to === l.level + 1) last.to = l.level
    else if (empty) out.push({ kind: "gap", from: l.level, to: l.level })
    else out.push({ kind: "level", level: l })
  }
  return out
}

/** ▲ / ▼ for 30 seconds after `value` moved by `margin` or more against what it was up to 30 s before; a later move
 *  restarts the 30 seconds. */
export function useMoved(value: number | null | undefined, margin: number): "up" | "down" | null {
  const seen = useRef<{ t: number; v: number }[]>([])
  const [moved, setMoved] = useState<{ dir: "up" | "down"; until: number } | null>(null)
  useEffect(() => {
    if (value == null) return
    const now = Date.now()
    seen.current = [...seen.current.filter((x) => now - x.t <= 30_000), { t: now, v: value }]
    const first = seen.current[0].v
    if (Math.abs(value - first) >= margin) setMoved({ dir: value > first ? "up" : "down", until: now + 30_000 })
  }, [value, margin])
  useEffect(() => {
    if (!moved) return
    const id = window.setTimeout(() => setMoved(null), Math.max(0, moved.until - Date.now()))
    return () => window.clearTimeout(id)
  }, [moved])
  return moved?.dir ?? null
}
