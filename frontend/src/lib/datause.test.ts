import { describe, expect, it } from "vitest"
import { type DataUse, forExport } from "./datause"

const use = (jpExport: boolean): DataUse => ({
  sources: [{ id: "tushare", name: "Tushare", store: true, llm: true, notify: true, export: true },
    { id: "jquants", name: "J-Quants", store: true, llm: true, notify: true, export: jpExport }],
  markets: { CN: "tushare", JP: "jquants" },
})

describe("数据使用开关 (spec §4.10): what a CSV may carry", () => {
  it("leaves out the rows of a market whose source is kept from export, and names it", () => {
    const rows = [{ m: "CN" as const, v: 1 }, { m: "JP" as const, v: 2 }]
    expect(forExport(use(false), rows, (r) => r.m)).toEqual({ rows: [{ m: "CN", v: 1 }], left: ["日股（J-Quants）"] })
    expect(forExport(use(true), rows, (r) => r.m)).toEqual({ rows, left: [] })
    expect(forExport(undefined, rows, (r) => r.m)).toEqual({ rows, left: [] })      // not known yet: the switches' defaults
  })
})
