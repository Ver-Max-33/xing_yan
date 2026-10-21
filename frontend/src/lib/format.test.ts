import { describe, expect, it } from "vitest"
import { changeClass, codeOf, fmtBig, fmtPct, fmtPrice, indexName, inUnit, marketOf, shortDate } from "./format"

describe("format", () => {
  it("prices use market precision and grouping", () => {
    expect(fmtPrice(2845.5, "JP")).toBe("2,845.5")
    expect(fmtPrice(1510, "CN")).toBe("1,510.00")
    expect(fmtPrice(null, "CN")).toBe("—")
  })
  it("percentages carry a sign", () => {
    expect(fmtPct(0.0087)).toBe("+0.87%")
    expect(fmtPct(-0.0042)).toBe("-0.42%")
    expect(fmtPct(0.1, 1, false)).toBe("10.0%")
    expect(fmtPct(Number.NaN)).toBe("—")
  })
  it("big numbers follow each market's habit", () => {
    expect(fmtBig(123_456_789, "CN")).toBe("1.23亿")
    expect(fmtBig(45_600_000, "CN")).toBe("4560万")
    expect(fmtBig(56_000, "CN")).toBe("5.6万")
    expect(fmtBig(2_830_000_000, "JP")).toBe("2.83B")
    expect(fmtBig(1_250_000, "JP")).toBe("1.25M")
  })
  it("ids and dates", () => {
    expect(marketOf("JP:130A")).toBe("JP")
    expect(codeOf("JP:130A")).toBe("130A")
    expect(shortDate("2026-10-01")).toBe("10-01")
  })
  it("index names, and the code for an index the table does not know", () => {
    expect(indexName("CN:IDX:000001")).toBe("上证指数")
    expect(indexName("JP:IDX:TOPIX")).toBe("TOPIX")
    expect(indexName("CN:IDX:999999")).toBe("999999")
  })
  it("change colour classes", () => {
    expect(changeClass(0.01)).toBe("text-up")
    expect(changeClass(-0.01)).toBe("text-down")
    expect(changeClass(0)).toBe("text-muted-foreground")
  })
  it("a rule's value reads in its unit", () => {
    expect(inUnit(0.184, "%")).toBe("18.4%")   // a share, ×100
    expect(inUnit(0.02, "%")).toBe("2.0%")     // one decimal always: 2.0% beside 1.8% in a column, not 2%
    expect(inUnit(2.4, "倍")).toBe("2.40 倍")
    expect(inUnit(22, "根")).toBe("22 根")
    expect(inUnit(1.0567)).toBe("1.06")
    expect(inUnit(null, "%")).toBe("")
  })
})
