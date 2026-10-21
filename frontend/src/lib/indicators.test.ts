import { describe, expect, it } from "vitest"
import { EMPTY_FORM, fromIndicator, type IndicatorSummary, looksLikeYaml, ordered, parseLevels, toYaml } from "./indicators"

describe("the editor's form as the YAML the backend keeps", () => {
  it("quotes every free text, keeps the formula as it is written in a block, and leaves out what is empty", () => {
    const yaml = toYaml({ ...EMPTY_FORM, name: "主力: 动能 \"A\"", doc: "看#主力", pane: "sub", levels: "20，50 80",
      params: [{ id: "N", value: 14, range: [2, 100], step: 1 }, { id: "TH", value: 80 }],
      formula: "A:=MA(C,N);\r\nB:A, COLORRED;\n\n  C:B*2;   \n" })
    expect(yaml).toBe([
      'name: "主力: 动能 \\"A\\""', 'doc: "看#主力"', "pane: sub", "params:",
      '  - {id: "N", value: 14, range: [2, 100], step: 1}', '  - {id: "TH", value: 80}', "levels: [20, 50, 80]",
      "markets: [CN, JP]", "formula: |2", "  A:=MA(C,N);", "  B:A, COLORRED;", "  ", "    C:B*2;", ""].join("\n"))
    expect(toYaml({ ...EMPTY_FORM, id: "u1", name: "x", formula: "" })).toContain("id: u1\nname: \"x\"")
  })
  it("reads an indicator back into the form", () => {
    const f = fromIndicator({ id: "rsi", name: "RSI", doc: "", group: "common", pane: "sub", params: [{ id: "N1", value: 6 }],
      levels: [20, 50, 80], markets: ["CN"], source: "", formula: "RSI1:RSI(N1);\n", version: 1 })
    expect([f.levels, f.formula, f.markets]).toEqual(["20, 50, 80", "RSI1:RSI(N1);", ["CN"]])
  })
  it("reference lines are read as you type them; a pasted 通达信 formula is not taken for YAML", () => {
    expect(parseLevels(" 20, 50，80  x -100 ")).toEqual([20, 50, 80, -100])
    expect(looksLikeYaml("MA5:MA(CLOSE,5);\nDIF:EMA(C,12)-EMA(C,26);")).toBe(false)
    expect(looksLikeYaml("name: x\nformula: |\n  A:C;")).toBe(true)
  })
})

describe("a group's order", () => {
  const row = (id: string, name: string, pane: IndicatorSummary["pane"], group: IndicatorSummary["group"] = "common") =>
    ({ id, name, pane, group } as IndicatorSummary)
  it("over the candles first, then the panes below, by name; yours as made", () => {
    const rows = [row("rsi", "RSI", "sub"), row("sar", "SAR", "main"), row("bbi", "BBI", "main"), row("u2", "乙", "sub", "mine"),
      row("u1", "甲", "main", "mine")]
    expect(ordered(rows, "common").map((r) => r.id)).toEqual(["bbi", "sar", "rsi"])
    expect(ordered(rows, "mine").map((r) => r.id)).toEqual(["u2", "u1"])
  })
})
