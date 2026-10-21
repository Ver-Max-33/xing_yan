import { describe, expect, it } from "vitest"
import type { Label } from "@/lib/rules"
import { canRedraw, progress } from "./progress"

const label = (o: Partial<Label>): Label => ({
  id: "x", listing_id: "CN:600000", cutoff: "2026-05-01", is_box: true, category: "flat", start: "2026-01-05", end: "2026-03-01",
  top: 11, bottom: 10, touches: [], exclusions: [], grp: "tune", verdict: null, created_at: "2026-09-01T10:00:00", updated_at: "2026-09-01T10:00:00",
  redraw_of: null, ...o,
})

describe("progress", () => {
  it("counts boxes per category and market toward 2 + 2, not-boxes toward 8, redraws toward 16", () => {
    const p = progress([
      label({ id: "a" }), label({ id: "b" }), label({ id: "c" }),               // a third CN flat box counts once over the target
      label({ id: "d", listing_id: "JP:7203" }),
      label({ id: "e", is_box: false, category: "wide_chop" }),
      label({ id: "f", redraw_of: "a" }),                                          // a redraw is a baseline, not a sample
    ])
    expect(p.byCategory.flat).toEqual({ CN: 3, JP: 1 })
    expect(p.boxes).toBe(3)                                                         // min(3, 2) + min(1, 2)
    expect(p.notBoxes).toBe(1)
    expect(p.redraws).toBe(1)
    expect(p.done).toBe(false)
  })
})

describe("canRedraw", () => {
  it("takes an original a week after it was drawn, once", () => {
    const now = new Date("2026-09-10T00:00:00Z")
    const orig = label({ id: "a", created_at: "2026-09-01T10:00:00" })
    expect(canRedraw(orig, [orig], now)).toBe(true)
    expect(canRedraw(label({ id: "b", created_at: "2026-09-05T10:00:00" }), [], now)).toBe(false)   // too fresh
    expect(canRedraw(orig, [orig, label({ id: "r", redraw_of: "a" })], now)).toBe(false)            // already redrawn
    expect(canRedraw(label({ id: "r", redraw_of: "a" }), [], now)).toBe(false)                      // a redraw itself
  })
})
