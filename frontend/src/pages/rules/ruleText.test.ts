import { describe, expect, it } from "vitest"
import { applyParams, copyText, paramUses, ruleIdOf, stepOf } from "./ruleText"

const FLOW = `id: box_up
name: "上涨后箱体"
params:
  - {id: box_max_width, value: 0.15, range: [0.08, 0.30], step: 0.01, tune: true}
  - {id: vol_spike, value: 2.0, range: [1.5, 3.0], step: 0.25}
stages: []
`
const BLOCK = `id: mine
name: 我的
params:
  - id: n
    value: 20   # 均线根数
    range: [5, 60]
  - id: strict
    value: false
`

describe("copyText", () => {
  it("gives the copy its own id and name, keeping the quotes", () => {
    const t = copyText(FLOW)
    expect(ruleIdOf(t)).toBe("box_up_copy")
    expect(t).toContain('name: "上涨后箱体（副本）"')
    expect(copyText(BLOCK)).toContain("name: 我的（副本）")
  })
})

describe("applyParams", () => {
  it("writes slider values into flow and block params, keeping the rest of the line", () => {
    const flow = applyParams(FLOW, { box_max_width: 0.18, vol_spike: 2.25 })
    expect(flow.missing).toEqual([])
    expect(flow.text).toContain("{id: box_max_width, value: 0.18, range: [0.08, 0.30], step: 0.01, tune: true}")
    expect(flow.text).toContain("{id: vol_spike, value: 2.25, range")
    const block = applyParams(BLOCK, { n: 30, strict: true })
    expect(block.text).toContain("    value: 30   # 均线根数")
    expect(block.text).toContain("    value: true")
  })

  it("names the parameters it could not find instead of guessing", () => {
    expect(applyParams(FLOW, { nope: 1 }).missing).toEqual(["nope"])
    expect(applyParams(FLOW, { nope: 1 }).text).toBe(FLOW)
  })
})

describe("ruleIdOf", () => {
  it("reads the id line, or null", () => {
    expect(ruleIdOf(BLOCK)).toBe("mine")
    expect(ruleIdOf("name: x")).toBeNull()
  })
})

describe("a rule written as JSON (import takes .json too)", () => {
  const JSON_RULE = JSON.stringify({ id: "j1", name: "站上均线", markets: ["CN"], params: [{ id: "n", value: 20, range: [5, 60], step: 1 }],
    stages: [{ id: "today", kind: "event", when: "C > MA(C, $n)" }], signal_at: "today.at" }, null, 2)

  it("reads its id, so saving it again is its next version, not a new rule", () => {
    expect(ruleIdOf(JSON_RULE)).toBe("j1")
  })
  it("is copied under its own id and name", () => {
    const c = JSON.parse(copyText(JSON_RULE))
    expect([c.id, c.name, c.stages[0].id]).toEqual(["j1_copy", "站上均线（副本）", "today"])
  })
  it("takes slider values into its params", () => {
    const r = applyParams(JSON_RULE, { n: 30, nope: 1 })
    expect(JSON.parse(r.text).params[0].value).toBe(30)
    expect(r.missing).toEqual(["nope"])
  })
})

describe("the step a parameter's slider moves by", () => {
  it("is the rule's own step", () => expect(stepOf({ value: 0.15, range: [0.08, 0.3], step: 0.01 })).toBe(0.01))
  it("is 1 for a whole number in a whole range (the backend takes whole numbers only there)", () =>
    expect(stepOf({ value: 20, range: [5, 60], step: null })).toBe(1))
  it("is a hundredth of the range otherwise", () => expect(stepOf({ value: 0.5, range: [0, 2], step: null })).toBe(0.02))
})

describe("where each parameter is used, for its slider", () => {
  it("lists the conditions, settings and windows that read $param", () => {
    const stages = [
      { id: "rally", kind: "window", during: [{ id: "runup", expr: "RUNUP(H, L) >= $rally" }], within: { stage: "box", bars: "$n" } },
      { id: "box", kind: "structure", use: "BOX", with: { scale: "mid", max_width: "$width" }, at: [{ id: "narrow", expr: "WIDTH <= $width" }] },
      { id: "up", kind: "event", when: "VOL_RATIO(5) >= $vol AND C > $vol2" },
    ]
    expect(paramUses(stages)).toEqual({
      rally: ["rally.runup：RUNUP(H, L) ≥ $rally"],
      n: ["rally 的窗口：$n 根"],
      width: ["box 的 max_width", "box.narrow：WIDTH ≤ $width"],
      vol: ["up：VOL_RATIO(5) ≥ $vol AND C > $vol2"],
      vol2: ["up：VOL_RATIO(5) ≥ $vol AND C > $vol2"],
    })
    expect(paramUses(undefined)).toEqual({})
    // by the rule's names where a check gave them (中箱, 突破量), else by ids
    const names = { stages: { rally: "上涨段", box: "中箱", up: "信号" }, conds: { "rally.runup": "涨幅" }, units: {} }
    expect(paramUses(stages, names)).toMatchObject({
      rally: ["涨幅：RUNUP(H, L) ≥ $rally"], n: ["上涨段 的窗口：$n 根"], width: ["中箱 的 max_width", "box.narrow：WIDTH ≤ $width"],
      vol: ["信号：VOL_RATIO(5) ≥ $vol AND C > $vol2"],
    })
  })
})
