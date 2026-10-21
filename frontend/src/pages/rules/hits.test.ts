import { describe, expect, it } from "vitest"
import type { Compare } from "@/lib/rules"
import { compareMarkets } from "./hits"

const part = (hits: number): Compare => ({ listings: 10, kept: 1, hits: [hits, hits + 1], right: [0, 0], wrong: [0, 0],
  added: [], lost: [], right_lost: [], wrong_added: [] })

describe("a comparison over a rule's markets", () => {
  it("leaves out a market that cannot be compared and says so, keeping the others", async () => {
    const r = await compareMarkets(["CN", "JP"], async (m) => {
      if (m === "JP") throw new Error("日股还没有数据")
      return part(3)
    })
    expect(r.data.hits).toEqual([3, 4])
    expect(r.skipped).toEqual(["日股没有算进来（日股还没有数据），只比较了 A 股"])
  })
  it("fails as the first market failed when none can be compared", async () => {
    await expect(compareMarkets(["CN"], async () => { throw new Error("没有数据") })).rejects.toThrow("没有数据")
  })
})
