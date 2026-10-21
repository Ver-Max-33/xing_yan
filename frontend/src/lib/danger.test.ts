import { describe, expect, it } from "vitest"
import { type DangerAnswer, dangerKey, dangerNotes, dangerTitle } from "./danger"

const base: DangerAnswer = { covered: true, why: null, day: "2026-10-14", warnings: {}, unknown: {}, uncovered: {}, unlocks_from: null, history_from: null }

describe("dangerNotes (the K-line block's notes, design 2026-10-15 §3)", () => {
  it("says what a replayed day cannot know", () => {
    expect(dangerNotes({ ...base, day: "2024-01-05", unlocks_from: "2026-10-15", history_from: "2024-10-15" }, "CN:600001")).toEqual([
      "2024-10-15 以前的危险提醒没有算（只留最近两年）",
      "2026-10-15 以前的解禁没有存，这一天只按上市日推算首发一年期",
    ])
  })
  it("says the money flow it lacks, and nothing on an ordinary day", () => {
    expect(dangerNotes({ ...base, unknown: { "CN:600001": ["retail"] } }, "CN:600001")).toEqual(["资金流向不全，散户接盘没法判断"])
    expect(dangerNotes(base, "CN:600001")).toEqual([])
  })
})

const W = { kind: "unlock" as const, label: "解禁前易走弱", date: "2026-11-02", days: 3, new: false, predicted: false,
  reason: "11-02 有占总股本 8.5% 的首发原始股解除限售（还有 3 个交易日）。", risk: "解禁后这些股份可以上市交易。……", brief: "" }

describe("dangerKey", () => {
  it("tells two unlocks of one stock apart", () => {
    expect(dangerKey(W)).not.toBe(dangerKey({ ...W, date: "2026-11-09" }))
  })
})

describe("dangerTitle (a stock's tags on hover)", () => {
  it("says each warning's risk, why, and what may follow", () => {
    expect(dangerTitle([W])).toBe("解禁前易走弱\n原因：11-02 有占总股本 8.5% 的首发原始股解除限售（还有 3 个交易日）。\n可能的风险：解禁后这些股份可以上市交易。……")
    expect(dangerTitle([W, { ...W, kind: "limit_up", label: "涨停后易回落" }]).split("\n\n")).toHaveLength(2)
  })
})
