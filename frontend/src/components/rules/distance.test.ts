import { describe, expect, it } from "vitest"
import type { Detail } from "@/lib/rules"
import { breakDistance, fromEdge } from "./distance"

const watch = (distances: Detail["distances"]) => ({ distances }) as Detail

describe("距上沿, one way everywhere (选股, 今日, 盯盘, the briefing)", () => {
  it("is the rise still needed to the box's upper edge, and over it says by how much", () => {
    expect(fromEdge(11.45 / 12 - 1)).toBe("距上沿 4.8%")      // price ÷ edge − 1 = −4.6% → 4.8% still to rise
    expect(fromEdge(12.1 / 12 - 1)).toBe("已越过上沿 0.8%")
    expect(fromEdge(1 / 0.9 - 1, false)).toBe("距下沿 10.0%")   // the edge below: the fall still needed
    expect(fromEdge(0.95 / 1 - 1, false)).toBe("已跌破下沿 5.0%")
  })

  it("reads a rule's distance against the edge, past the break line waiting for the box", () => {
    const d = (value: number) => watch([{ id: "break_up", value, threshold: 12.18, edge: 12 }])
    expect(breakDistance(d(11.45))).toBe("距上沿 4.8%")
    expect(breakDistance(d(12.1))).toBe("已越过上沿 0.8%")
    expect(breakDistance(d(12.5))).toBe("已越过上沿 4.2% · 待确认")
  })

  it("says a run kept before the edge was recorded for what it measured: the break line", () => {
    expect(breakDistance(watch([{ id: "break_up", value: 10, threshold: 10.18 }]))).toBe("距突破线 1.8%")
    expect(breakDistance(watch([{ id: "break_down", value: 10, threshold: 9.5 }]))).toBe("距跌破线 5.0%")
  })

  it("says the line is crossed but not yet confirmed once the close is past it", () => {
    expect(breakDistance(watch([{ id: "break_up", value: 10.3, threshold: 10.18 }]))).toBe("已越线，待确认")
    expect(breakDistance(watch([{ id: "break_down", value: 9.4, threshold: 9.5 }]))).toBe("已越线，待确认")
  })

  it("has nothing to say without a break distance", () => {
    expect(breakDistance(null)).toBeNull()
    expect(breakDistance(watch([{ id: "volume", value: 0.5, threshold: 2 }]))).toBeNull()
    expect(breakDistance(watch([{ id: "break_up", value: null, threshold: 10 }]))).toBeNull()
  })
})
