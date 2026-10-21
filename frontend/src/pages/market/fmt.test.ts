import { describe, expect, it } from "vitest"
import { ladderRows, linePath } from "./fmt"

describe("the day strip's lines", () => {
  it("break where minutes were not recorded instead of drawing through them", () => {
    const pts: [number, number][] = [[1, 10], [2, 12], [3, 11], [14, 20], [15, 21]]
    expect(linePath(pts, (i) => i, (v) => v)).toBe("M1 10L2 12L3 11M14 20L15 21")
    expect(linePath(pts, (i) => i, (v) => v, true)).toBe("M1 10H2V12H3V11M14 20H15V21")
  })
  it("keep a skipped minute or two joined", () => {
    expect(linePath([[1, 1], [3, 2], [6, 3]], (i) => i, (v) => v)).toBe("M1 1L3 2L6 3")
  })
})

describe("the ladder (redesign §2.5)", () => {
  it("folds empty runs from three boards up into one line", () => {
    const lv = (level: number, sealed: number, broken = 0) => ({ level, sealed, broken, names: [], more: 0 })
    const rows = ladderRows([lv(10, 1), lv(9, 0), lv(8, 0), lv(7, 0), lv(6, 1, 1), lv(5, 0), lv(4, 0), lv(3, 1, 2), lv(2, 12, 7), lv(1, 28, 13)])
    expect(rows.map((r) => (r.kind === "gap" ? `${r.from}–${r.to}` : r.level.level))).toEqual([10, "9–7", 6, "5–4", 3, 2, 1])
  })
  it("keeps a single empty level as a gap of one", () => {
    const lv = (level: number, sealed: number) => ({ level, sealed, broken: 0, names: [], more: 0 })
    expect(ladderRows([lv(4, 1), lv(3, 0), lv(2, 3), lv(1, 9)]).map((r) => (r.kind === "gap" ? `${r.from}–${r.to}` : r.level.level))).toEqual([4, "3–3", 2, 1])
  })
})
