// A sample box from a rectangle drawn on the chart (spec §8.2 样例框, §5.10): its dates and edges, and
// the touches and left-out extremes worth confirming — you tick the ones you agree with.
import type { LabelDraft } from "@/lib/rules"

export interface OHLC { date: string; open: number; high: number; low: number; close: number }
const ISO = /^\d{4}-\d{2}-\d{2}$/

/** `bars`: the chart's settled bars — the corners snap to the first and last of them inside the rectangle, up to the
 *  cutoff (a corner in the empty space right of the last bar, or on a day without trading, would be a date the sample
 *  cannot be saved with); null when no such bar lies inside. */
export function draftFromRect(listingId: string, corners: { date: string; price: number }[], cutoff: string,
  bars?: { date: string }[]): LabelDraft | null {
  const dates = corners.map((c) => c.date).sort()
  let [start, end] = [dates[0], dates[dates.length - 1]]
  if (bars) {
    const inside = bars.map((b) => b.date).filter((d) => ISO.test(d) && d <= cutoff && d >= start && d <= end)
    if (!inside.length) return null
    ;[start, end] = [inside[0], inside[inside.length - 1]]
  }
  const prices = corners.map((c) => c.price)
  return {
    listing_id: listingId, cutoff, is_box: true, category: "flat", start, end,
    top: Math.max(...prices), bottom: Math.min(...prices), scale: null, context: null, position: null,
    touches: [], exclusions: [], note: "",
  }
}

/** Touch candidates: bars inside the box whose high (low) is within `tol` of the top (bottom). Left-out
 *  candidates: the highest high above the top in the 20 bars before the box (箱前冲高), and bars inside
 *  that poked through an edge but closed back within it (刺穿). */
export function candidates(bars: OHLC[], box: { start: string; end: string; top: number; bottom: number }, tol = 0.015) {
  const ok = bars.filter((b) => ISO.test(b.date))
  const inside = ok.filter((b) => b.date >= box.start && b.date <= box.end)
  const touches: LabelDraft["touches"] = []
  for (const b of inside) {
    if (Math.abs(b.high / box.top - 1) <= tol) touches.push({ date: b.date, side: "top", price: b.high })
    if (Math.abs(b.low / box.bottom - 1) <= tol) touches.push({ date: b.date, side: "bottom", price: b.low })
  }
  const exclusions: LabelDraft["exclusions"] = []
  const before = ok.filter((b) => b.date < box.start).slice(-20)
  const peak = before.reduce<OHLC | null>((m, b) => (b.high > box.top * (1 + tol) && (!m || b.high > m.high) ? b : m), null)
  if (peak) exclusions.push({ date: peak.date, kind: "箱前冲高", price: peak.high, reason: "进箱前的冲高针，不作上沿（枭首）" })
  for (const b of inside) {
    if (b.high > box.top * (1 + tol) && b.close <= box.top * (1 + tol)) exclusions.push({ date: b.date, kind: "刺穿", price: b.high, reason: "冲出上沿又收回箱内" })
    if (b.low < box.bottom * (1 - tol) && b.close >= box.bottom * (1 - tol)) exclusions.push({ date: b.date, kind: "刺穿", price: b.low, reason: "刺破下沿又收回箱内" })
  }
  return { touches, exclusions }
}
