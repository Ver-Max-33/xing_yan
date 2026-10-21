import { describe, expect, it } from "vitest"
import { parseNumber, showNumber } from "@/lib/number"

describe("a typed number", () => {
  const most = { int: true, min: 1, max: 50 }
  it("is what was typed, or says why not — never a clamped or default value", () => {
    expect(parseNumber("8", most)).toEqual({ value: 8, error: null })
    expect(parseNumber("18", most)).toEqual({ value: 18, error: null })
    expect(parseNumber("60", most)).toEqual({ value: null, error: "要在 1 到 50 之间" })
    expect(parseNumber("0", most).error).toBe("要在 1 到 50 之间")
    expect(parseNumber("4.5", most).error).toBe("要填整数")
    expect(parseNumber("", most).error).toBe("要填一个数")
    expect(parseNumber("abc", most).error).toBe("不是一个数")
  })
  it("may be empty where empty means none (关, 不限, the default)", () => {
    expect(parseNumber("  ", { optional: true, int: true, min: 1 })).toEqual({ value: null, error: null })
  })
  it("in percent is stored as a share and shown back as typed", () => {
    const fee = { min: 0, max: 5, scale: 100, optional: true }
    expect(parseNumber("0.025", fee)).toEqual({ value: 0.00025, error: null })
    expect(showNumber(0.00025, fee)).toBe("0.025")
    expect(parseNumber("25", fee).error).toBe("要在 0 到 5 之间")
    expect(showNumber(0.08, { scale: 100 })).toBe("8")
  })
  it("keeps exclusive bounds", () => {
    expect(parseNumber("0", { above: 0, below: 100, scale: 100 }).error).toBe("要大于 0")
    expect(parseNumber("100", { above: 0, below: 100, scale: 100 }).error).toBe("要小于 100")
  })
})
