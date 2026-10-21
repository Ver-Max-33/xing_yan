// 距上沿, one way app-wide (spec §5.5): from the price to the box's own upper edge (上沿 — where the alert is set),
// the rise still needed; over it, by how much — and past the rule's break line (上沿 × (1 + tol)) it waits for the box to
// confirm. 选股, K 线's rule list and 盯盘 read it from here; the backend says the same in the explanation
// (wb/engine/l3/explain.py, _edge) and the briefing (wb/notify/brief.py).
import { fmtPct } from "@/lib/format"
import type { Detail } from "@/lib/rules"

const p = (v: number) => fmtPct(v, 1, false)

/** `r` = price ÷ edge − 1; `up`: the upper edge (上沿), else the lower (下沿). */
export function fromEdge(r: number, up = true): string {
  if (up) return r < 0 ? `距上沿 ${p(1 / (1 + r) - 1)}` : `已越过上沿 ${p(r)}`
  return r > 0 ? `距下沿 ${p(1 - 1 / (1 + r))}` : `已跌破下沿 ${p(-r)}`
}

export function breakDistance(d: Pick<Detail, "distances"> | null | undefined): string | null {
  const x = d?.distances.find((k) => k.id === "break_up" || k.id === "break_down")
  if (!x || !x.value || x.threshold == null) return null
  const up = x.id === "break_up"
  if (!x.edge) {   // a run kept before the edge was recorded: measured to the break line, and said so
    if (up ? x.value >= x.threshold : x.value <= x.threshold) return "已越线，待确认"
    return `${up ? "距突破线" : "距跌破线"} ${p(Math.abs(x.threshold / x.value - 1))}`
  }
  const text = fromEdge(x.value / x.edge - 1, up)
  return (up ? x.value >= x.threshold : x.value <= x.threshold) ? `${text} · 待确认` : text
}
