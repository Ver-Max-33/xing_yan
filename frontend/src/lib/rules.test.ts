import { describe, expect, it } from "vitest"
import { keepWithin } from "./rules"

describe("the structures while the next minute's are on their way (spec §8.5)", () => {
  const q = (listingId: string, basis = "qfq", minute: string | null = "2026-10-02T10:41:00+09:00") => ({ queryKey: ["structures", listingId, basis, minute] })
  it("keeps the same stock's on screen, never another stock's or another basis's", () => {
    const prev = { boxes: [{ id: "JP:7203:long:3" }] }
    expect(keepWithin("JP:7203", "qfq")(prev, q("JP:7203"))).toBe(prev)
    expect(keepWithin("JP:6758", "qfq")(prev, q("JP:7203"))).toBeUndefined()
    expect(keepWithin("JP:7203", "hfq")(prev, q("JP:7203"))).toBeUndefined()
    expect(keepWithin("JP:7203", "qfq")(undefined, undefined)).toBeUndefined()
  })
})
