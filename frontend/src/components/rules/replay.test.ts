import { describe, expect, it } from "vitest"
import { dayIndex } from "./replay"

const DATES = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]

describe("dayIndex", () => {
  it("puts no day (the latest bar) at the end", () => {
    expect(dayIndex(DATES, null)).toBe(3)
  })

  it("finds a trading day, and a weekend at the Friday before it", () => {
    expect(dayIndex(DATES, "2026-09-28")).toBe(2)
    expect(dayIndex(DATES, "2026-09-27")).toBe(1)
  })

  it("clamps days outside the listing's history", () => {
    expect(dayIndex(DATES, "2026-01-01")).toBe(0)
    expect(dayIndex(DATES, "2027-01-01")).toBe(3)
  })
})
