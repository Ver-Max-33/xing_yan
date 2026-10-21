import { describe, expect, it } from "vitest"
import { type ChipBar, chips } from "./cyq"
import { EM_688682 } from "./cyq.fixture"

const bars: ChipBar[] = EM_688682.split(";").map((r) => {
  const [, o, c, h, l, t] = r.split(",").map(Number)
  return { open: o, close: c, high: h, low: l, turnover_rate: t }
})

describe("筹码分布", () => {
  it("is 东方财富's, number for number, on their own bars", () => {
    const r = chips(bars, bars.length - 1)!
    expect((r.profit * 100).toFixed(2)).toBe("39.53")
    expect(r.avgCost.toFixed(2)).toBe("34.72")
    expect([r.p90.low.toFixed(2), r.p90.high.toFixed(2), (r.p90.concentration * 100).toFixed(2)]).toEqual(["28.69", "50.27", "27.33"])
    expect([r.p70.low.toFixed(2), r.p70.high.toFixed(2), (r.p70.concentration * 100).toFixed(2)]).toEqual(["30.60", "45.19", "19.26"])
    expect(r.prices.length).toBe(150)
  })

  it("reads the 210 bars up to the day, and says when fewer were there", () => {
    const longer = [...bars.slice(0, 40), ...bars]   // older bars beyond the window change nothing
    const a = chips(longer, longer.length - 1)!
    expect(a.profit).toBeCloseTo(chips(bars, bars.length - 1)!.profit, 12)
    expect(a.bars).toBe(210)
    expect(chips(bars, 99)!.bars).toBe(100)
  })

  it("is unknown without any turnover (日股 has none)", () => {
    expect(chips(bars.map((b) => ({ ...b, turnover_rate: null })), bars.length - 1)).toBeNull()
  })
  it("is the same in any basis: the day's own prices, a one-price day included", () => {
    const one = bars.map((b, i) => (i === bars.length - 30 ? { ...b, open: b.close, high: b.close, low: b.close } : b))
    for (const bs of [bars, one]) {
      const a = chips(bs, bs.length - 1)!
      const b = chips(bs.map((x) => ({ ...x, open: x.open * 7, high: x.high * 7, low: x.low * 7, close: x.close * 7, scale: 7 })), bs.length - 1)!
      expect(b.profit).toBeCloseTo(a.profit, 12)      // 0.3.0: 50.09% vs 76.16% with the one-price day
      expect(b.avgCost / 7).toBeCloseTo(a.avgCost, 9)
    }
  })
})
