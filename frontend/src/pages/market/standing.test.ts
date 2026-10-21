import { describe, expect, it } from "vitest"
import type { Standings } from "@/lib/market"
import { standingDay, standingLabel, standingOf, standingText } from "./standing"

const ST = {
  market: "CN", mode: "live", day: "2026-10-12", time: "10:42", of: { l1: 31, l2: 127 },
  l1: [{ name: "传媒", chg: 0.0104, rank: 2, tag: "转强", rel5: 0.033, prior15: 0.014, rel20: 0.045, limit_up: 11, parent: null, small: false }],
  l2: [{ name: "影视院线", chg: 0.0658, rank: 1, tag: "一直强", rel5: 0.05, prior15: 0.04, rel20: 0.1, limit_up: 3, parent: "传媒", small: false },
       { name: "小行业Ⅱ", chg: 0.01, rank: null, tag: null, rel5: null, prior15: null, rel20: null, limit_up: 0, parent: "传媒", small: true }],
} as Standings

describe("a stock's industry standing (redesign §6)", () => {
  it("reads like the market page", () => {
    expect(standingText(standingOf(ST, "l2", "影视院线")!, ST.of.l2, "今天")).toBe("+6.58% · 今天第 1 / 127")
    expect(standingText(standingOf(ST, "l2", "影视院线")!, ST.of.l2)).toBe("+6.58% · 第 1 / 127")
    expect(standingText(standingOf(ST, "l2", "小行业Ⅱ")!, ST.of.l2, "今天")).toBe("+1.00%")
    expect(standingOf(ST, "l2", "没有")).toBeNull()
    expect(standingOf(undefined, "l1", "传媒")).toBeNull()
  })
  it("says whose numbers they are", () => {
    expect(standingDay(ST)).toBe("今天")
    expect(standingDay({ ...ST, mode: "close", day: "2026-10-09" })).toBe("10-09")
    expect(standingLabel(ST)).toBe("今天 10:42")
    expect(standingLabel({ ...ST, mode: "close", time: null, day: "2026-10-09" })).toBe("10-09")
  })
})
