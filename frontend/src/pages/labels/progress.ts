// How far the label set is (spec §5.10): 40 boxes — 10 categories, each 2 A-share and 2 Japanese — 8
// "not a box" samples, and 16 of them redrawn a week or more later, blind, as your own baseline.
import { BOX_CATEGORIES, type Label } from "@/lib/rules"

export const PER_MARKET = 2
export const BOX_TARGET = Object.keys(BOX_CATEGORIES).length * PER_MARKET * 2
export const NOT_BOX_TARGET = 8
export const REDRAW_TARGET = 16
const WEEK_MS = 7 * 86_400_000

export interface Progress {
  byCategory: Record<string, { CN: number; JP: number }>
  boxes: number      // toward BOX_TARGET (a category's extra samples beyond 2 + 2 do not count)
  notBoxes: number
  redraws: number
  done: boolean
}

export function progress(labels: Label[]): Progress {
  const byCategory: Progress["byCategory"] = Object.fromEntries(Object.keys(BOX_CATEGORIES).map((c) => [c, { CN: 0, JP: 0 }]))
  let notBoxes = 0
  let redraws = 0
  for (const l of labels) {
    if (l.redraw_of) { redraws++; continue }
    if (!l.is_box) { notBoxes++; continue }
    const m = l.listing_id.startsWith("JP:") ? "JP" : "CN"
    ;(byCategory[l.category] ??= { CN: 0, JP: 0 })[m]++
  }
  const boxes = Object.entries(byCategory).filter(([c]) => c in BOX_CATEGORIES)
    .reduce((n, [, v]) => n + Math.min(v.CN, PER_MARKET) + Math.min(v.JP, PER_MARKET), 0)
  return { byCategory, boxes, notBoxes, redraws,
    done: boxes >= BOX_TARGET && notBoxes >= NOT_BOX_TARGET && redraws >= REDRAW_TARGET }
}

/** A sample can be redrawn once, at least a week after it was drawn (so you do not remember the line). */
export function canRedraw(l: Label, all: Label[], now = new Date()): boolean {
  if (l.redraw_of) return false
  if (all.some((x) => x.redraw_of === l.id)) return false
  return now.getTime() - Date.parse(l.created_at) >= WEEK_MS // written in local time, read as local time
}
