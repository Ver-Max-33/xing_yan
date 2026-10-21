import { describe, expect, it } from "vitest"
import { candidates, draftFromRect } from "./labelDraft"

const bar = (date: string, o: number, h: number, l: number, c: number) => ({ date, open: o, high: h, low: l, close: c })
const bars = [
  bar("2026-01-02", 10, 13.0, 10, 12.8),   // a run-up spike before the box: 箱前冲高
  bar("2026-0105", 12, 12.0, 11.6, 11.8),  // malformed date is ignored
  bar("2026-01-05", 12, 12.01, 11.6, 11.8), // touches the top
  bar("2026-01-06", 11.6, 11.7, 11.0, 11.2), // touches the bottom
  bar("2026-01-07", 11.2, 12.6, 11.4, 11.9), // a spike through the top, closing inside: 刺穿
  bar("2026-01-08", 11.6, 11.75, 11.5, 11.7),
  bar("2026-01-09", 11.7, 12.0, 11.01, 11.6), // touches both
]

describe("a sample box drawn as a rectangle", () => {
  it("takes its dates and edges from the two corners, whatever order they were drawn in", () => {
    const d = draftFromRect("CN:600000", [{ date: "2026-01-09", price: 11.0 }, { date: "2026-01-05", price: 12.0 }], "2026-01-20")!
    expect([d.start, d.end, d.top, d.bottom, d.cutoff]).toEqual(["2026-01-05", "2026-01-09", 12.0, 11.0, "2026-01-20"])
    expect(d.is_box).toBe(true)
  })
  it("snaps to the bars inside it up to the cutoff, so a corner past the last bar or on a weekend still saves", () => {
    const days = [{ date: "2026-01-05" }, { date: "2026-01-06" }, { date: "2026-01-07" }, { date: "2026-01-08" }]
    const corners = (a: string, b: string) => [{ date: a, price: 11 }, { date: b, price: 12 }]
    const d = draftFromRect("CN:600000", corners("2026-01-03", "2026-01-10"), "2026-01-07", days)!   // a Saturday; past the cutoff
    expect([d.start, d.end]).toEqual(["2026-01-05", "2026-01-07"])
    expect(draftFromRect("CN:600000", corners("2026-01-09", "2026-01-12"), "2026-01-08", days)).toBeNull()   // nothing to stand on
  })
  it("offers the touches near its edges and the extremes it leaves out", () => {
    const c = candidates(bars, { start: "2026-01-05", end: "2026-01-09", top: 12.0, bottom: 11.0 }, 0.015)
    expect(c.touches.map((t) => `${t.date}:${t.side}`)).toEqual(
      ["2026-01-05:top", "2026-01-06:bottom", "2026-01-09:top", "2026-01-09:bottom"])
    expect(c.exclusions.map((e) => `${e.date}:${e.kind}`)).toEqual(["2026-01-02:箱前冲高", "2026-01-07:刺穿"])
    expect(c.exclusions[1].price).toBe(12.6)
  })
})
