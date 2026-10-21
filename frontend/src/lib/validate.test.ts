import { describe, expect, it } from "vitest"
import {
  adoptAdd, type AttrFeature, type Attribution, bookChanged, type BookSettings, type Costs, drift, FEW, formSettings, groupMedians, ledgerCSV, measure, planLine,
  crossLine, entriesNote, planSteps, planVerdict, REASON, rowsCSV, settingsText, tradesCSV, VERDICT, winsOf,
} from "./validate"

describe("验证", () => {
  it("says how 样本外 compares with 样本内", () => {
    const s = (a: number | null, b: number | null) => ({ in: { signals: 1, effective: 50, win: a, median: 0 }, out: { signals: 1, effective: 40, win: b, median: 0 } })
    expect(drift(s(0.596, 0.54))).toBe("有衰减")
    expect(drift(s(0.55, 0.56))).toBe("持平")
    expect(drift(s(0.5, 0.6))).toBe("更好")
    expect(drift(s(null, 0.6))).toBeNull()
  })
  it("compares the halves only when both have enough effective samples (spec §9.1: under 30, numbers, no conclusion)", () => {
    const h = (effective: number, win: number) => ({ signals: effective, effective, win, median: 0 })
    expect(FEW).toBe(30)
    expect(drift({ in: h(4, 0.25), out: h(5, 1) })).toBeNull()             // 100% against 25% on nine signals: no 「更好」
    expect(drift({ in: h(29, 0.5), out: h(40, 0.7) })).toBeNull()
    expect(drift({ in: h(30, 0.5), out: h(30, 0.6) })).toBe("更好")
  })
  it("counts a few wins instead of turning them into a rate", () => {
    expect(winsOf(1, 9)).toBe("9/9")
    expect(winsOf(0.25, 4)).toBe("1/4")
    expect(winsOf(null, 0)).toBe("—")
  })
  it("does not call a condition 多余 because removing it stayed within the error", () => {
    expect(VERDICT.多余[0]).toBe("看不出作用（误差内）")
    expect(VERDICT.多余[0]).not.toContain("删")
  })
  it("exports each signal with its outcomes, what could not be bought and its sample", () => {
    const row = {
      market: "CN" as const, listing_id: "CN:000001", name: "平安银行", signal_date: "2025-03-03", entry_date: "2025-03-04",
      blocked: "limit", r1: 0.01, x1: 0.005, r3: null, x3: null, r5: null, x5: null, r10: null, x10: null, r20: 0.1, x20: 0.08,
      r60: null, x60: null, mfe: 0.12, mae: -0.03, gone: false, industry: "银行", above200: true,
    }
    const csv = rowsCSV([row], "2025-01-01").split("\n")
    expect(csv[0]).toBe("﻿市场,代码,名称,信号日,入场日,买不到,1日收益,1日超额,3日收益,3日超额,5日收益,5日超额,10日收益,10日超额,20日收益,20日超额,60日收益,60日超额,20日最大浮盈,20日最大浮亏,停止交易,行业,大盘在200日线上,样本")
    expect(csv[1]).toBe("CN,000001,平安银行,2025-03-03,2025-03-04,开盘涨停,1.00%,0.50%,,,,,,,10.00%,8.00%,,,12.00%,-3.00%,,银行,是,样本外")
  })
})

describe("组合回测 (spec §9.2)", () => {
  it("exports each trade with why it ended, and one still held at the end", () => {
    const t = {
      market: "CN" as const, listing_id: "CN:600000", name: "浦发银行", signal_date: "2025-03-03", entry_date: "2025-03-04",
      entry_price: 10.2, shares: 4900, exit_date: "2025-03-18", exit_price: 9.6, reason: "box", pnl: -3062.4, ret: -0.0612, days: 11,
    }
    const csv = tradesCSV([t, { ...t, listing_id: "CN:600001", name: null, exit_date: null, exit_price: null, reason: null, ret: null, days: null }]).split("\n")
    expect(csv[0]).toBe("\ufeff市场,代码,名称,信号日,入场日,入场价,股数,离场日,离场价,离场原因,盈亏,收益,持有天数")
    expect(REASON.box).toBe("跌破箱底")
    expect(csv[1]).toBe("CN,600000,浦发银行,2025-03-03,2025-03-04,10.2,4900,2025-03-18,9.6,跌破箱底,-3062.40,-6.12%,11")
    expect(csv[2]).toContain(",4900,,,持有到最后,")
  })
  it("exports the cash ledger with each kind of move named", () => {
    const row = { market: "JP" as const, date: "2025-06-27", listing_id: "JP:7203", name: "トヨタ自動車", kind: "dividend", shares: 500, price: 45, amount: 22500, note: null }
    expect(ledgerCSV([row]).split("\n")[1]).toBe("JP,2025-06-27,7203,トヨタ自動車,分红,500,45,22500.00,")
  })
})

describe("the settings a book ran with", () => {
  const ran: BookSettings = {
    cash: 1_000_000, costs: { commission: 0.00025, min_commission: 5, transfer: 0.00001, stamp: 0.0005, slippage: 0.001 },
    max_positions: 10, hold: 20, box_stop: true, trail: null, target: 0.2, lot: 100, t_plus_1: true, own: false,
  }
  const book = { max_positions: 10, hold: 20, box_stop: true, trail: null, target: 0.2, own: false }
  it("says the places, the exits, the cash and the costs", () => {
    expect(settingsText(ran, "CN")).toBe("最多 10 只 · 持有 20 天、跌破箱底、止盈 20% · 初始资金 1,000,000 元 · 佣金 0.025%（最低 5 元）、过户费 0.001%、印花税 0.05%、滑点 0.1%")
    expect(settingsText({ ...ran, cash: 10_000_000, costs: { commission: 0, min_commission: 0, transfer: 0, stamp: 0, slippage: 0.001 }, hold: null,
      box_stop: false, trail: 0.08, target: null }, "JP")).toBe("最多 10 只 · 移动止损 8% · 初始资金 10,000,000 円 · 佣金 0、滑点 0.1%")
  })
  it("puts the form over a market's defaults: what a run would use, which the folded form says in words", () => {
    const form = { book: { ...book, max_positions: 4, stall: null, weak_positions: null }, costs: {}, cash: { CN: 500_000 } }
    const s = formSettings(form, "CN", ran)
    expect([s.max_positions, s.cash, s.costs.commission, s.t_plus_1]).toEqual([4, 500_000, 0.00025, true])   // the rest: the market's own
    expect(bookChanged(s, form, "CN")).toBe(false)
    expect(settingsText(s, "CN")).toContain("最多 4 只")
  })
  it("says what a rate was before the day it changed, unless the form sets that rate for every day", () => {
    const dated: BookSettings = { ...ran, costs: { ...ran.costs, history: [["2023-08-28", "stamp", 0.001], ["2022-04-29", "transfer", 0.00002]] } }
    expect(settingsText(dated, "CN")).toContain("过户费 0.001%（2022-04-29 前 0.002%）、印花税 0.05%（2023-08-28 前 0.1%）、")
    const s = formSettings({ book: { ...book, stall: null, weak_positions: null }, costs: { CN: { stamp: 0.0008 } as Costs }, cash: {} }, "CN", dated)
    expect(settingsText(s, "CN")).toContain("过户费 0.001%（2022-04-29 前 0.002%）、印花税 0.08%、")
  })
  it("tells when the form above no longer says what the run used", () => {
    expect(bookChanged(ran, { book, costs: {}, cash: {} }, "CN")).toBe(false)
    expect(bookChanged(ran, { book: { ...book, hold: 10 }, costs: {}, cash: {} }, "CN")).toBe(true)
    expect(bookChanged(ran, { book, costs: {}, cash: { CN: 500_000 } }, "CN")).toBe(true)
    expect(bookChanged(ran, { book, costs: { CN: { ...ran.costs, slippage: 0.002 } }, cash: {} }, "CN")).toBe(true)
    expect(bookChanged(ran, { book, costs: { JP: { ...ran.costs, slippage: 0.002 } }, cash: { JP: 1 } }, "CN")).toBe(false)   // another market's
  })
  it("says when each rule's own judgement replaces the days, the box line and the target (a rule without one uses them)", () => {
    expect(settingsText({ ...ran, own: true, trail: 0.08 }, "CN")).toBe("最多 10 只 · 按规则自己的失效线、目标位和天数（规则没写时：持有 20 天、"
      + "跌破箱底、止盈 20%）、移动止损 8% · 初始资金 1,000,000 元 · 佣金 0.025%（最低 5 元）、过户费 0.001%、印花税 0.05%、滑点 0.1%")
    expect(bookChanged({ ...ran, own: true }, { book: { ...book, own: true }, costs: {}, cash: {} }, "CN")).toBe(false)
    const { own: _, ...before } = book   // a form kept from before 1.41: on; a run from before it was not
    expect(formSettings({ book: before, costs: {}, cash: {} }, "CN", ran).own).toBe(true)
    expect(bookChanged({ ...ran, own: undefined }, { book: before, costs: {}, cash: {} }, "CN")).toBe(true)
    expect(REASON.fail).toBe("跌破失效线")
  })
  it("says the research exits too: N days without a new high, fewer places in a weak market", () => {
    const more = { ...ran, stall: 5, weak_positions: 5 }
    expect(settingsText(more, "CN")).toBe("最多 10 只（大盘弱时 5 只） · 持有 20 天、跌破箱底、止盈 20%、5 天不创新高 · 初始资金 1,000,000 元 · "
      + "佣金 0.025%（最低 5 元）、过户费 0.001%、印花税 0.05%、滑点 0.1%")
    expect(bookChanged(more, { book: { ...book, stall: 5, weak_positions: 5 }, costs: {}, cash: {} }, "CN")).toBe(false)
    expect(bookChanged(more, { book: { ...book, stall: 3, weak_positions: 5 }, costs: {}, cash: {} }, "CN")).toBe(true)
    expect(bookChanged(ran, { book: { ...book, weak_positions: 2 }, costs: {}, cash: {} }, "CN")).toBe(true)
  })
})

describe("候选条件归因 (spec §9.5)", () => {
  const st = (median: number | null) => ({ signals: 10, effective: 40, win: 0.5, median })
  const f: AttrFeature = {
    id: "dist20", label: "离 20 日线", expr: "MA_DIST(20)", market: "CN", verdict: "单调", direction: "up", cuts: [0.01, 0.02, 0.03, 0.04],
    groups: [-0.02, -0.01, 0, 0.01, 0.02].map((m, k) => ({ group: k, lo: null, hi: null, in: st(m), out: st(m === 0 ? null : m) })),
  }
  it("lists a feature's group medians from its lowest values up", () => {
    expect(groupMedians(f, "in")).toBe("-2.0% · -1.0% · 0.0% · +1.0% · +2.0%")
    expect(groupMedians(f, "out")).toBe("-2.0% · -1.0% · — · +1.0% · +2.0%")
  })
  it("adds an adopted filter to the signal stage's at, named after its feature", () => {
    const a: Attribution = { features: [f], suggestions: [], stage: "today", skipped: [] }
    const split = { signals: 1, effective_in: 1, effective_out: 1, win_in: 0.5, win_out: 0.5, median_in: 0, median_out: 0 }
    expect(adoptAdd(a, { feature: "dist20", label: "离 20 日线", op: ">", threshold: 0.01, condition: "MA_DIST(20) > 0.01", before: split, after: split }))
      .toEqual({ label: "离 20 日线", stage: "today", where: "at", expr: "MA_DIST(20) > 0.01", id: "f_dist20" })
  })
})

describe("按交易计划 (design 2026-10-09 §6, §10)", () => {
  const part = { days: 120, expectancy: 0.012, ci: [0.004, 0.02] as [number, number], vs_control: 0.008, vs_control_ci: [0.001, 0.015] as [number, number],
    control_days: 118, no_control_days: 2, few: false }
  it("says the expectancy per signal day with its interval, and against stocks bought at random the same days", () => {
    expect(planLine(part)).toBe("每个信号日平均 +1.2%（95% 区间 +0.4% 到 +2.0%）；比同一天随机买的多 +0.8%（+0.1% 到 +1.5%）")
    expect(planLine({ ...part, vs_control: null, vs_control_ci: [null, null] })).toBe("每个信号日平均 +1.2%（95% 区间 +0.4% 到 +2.0%）")
    expect(planVerdict(part)).toBe("比随机买强")
    expect(planVerdict({ ...part, vs_control_ci: [-0.002, 0.015] })).toBe("和随机买看不出差别")
    expect(planVerdict({ ...part, few: true })).toBe("信号日少于 30 个，不下结论")
  })
  it("reads the plan in words and names a result's yardstick", () => {
    const plan = { entry: "close" as const, stop: [{ line: "g.small_bottom", label: "金品底", share: 1 }], take: [{ line: "g.big_top", label: "大箱顶", share: 0.5 }],
      exit: [{ when: "C < MA(C, 5)", label: "跌破 5 日线", share: 1 }], stall: null, days: 60, fill: null }
    expect(planSteps(plan)).toEqual(["信号那天尾盘买", "收盘跌破金品底就走", "碰到大箱顶卖一半", "收盘时跌破 5 日线就卖", "最多拿 60 个交易日"])
    expect(entriesNote(3)).toBe("比较了 3 种买法：挑表现最好的那种，是从 3 种里挑的，它的数字会偏好（挑选的偏差第五期算）")
    expect(crossLine({ crosses: 240, per_day: 1.6, at_open: 0.125, closed_above: 0.42, signal: 0.38, kinds: [] }))
      .toBe("越线 240 次，有越线的日子平均每天 1.6 只；开盘就在箱顶上方的 12.5%，收盘站上突破线的 42.0%，收盘成了规则信号的 38.0%")
    expect(planSteps({ ...plan, entry: "cross", max_gap: 0.03, confirm_volume: 1.5 }).slice(0, 2))
      .toEqual(["盘中第一次碰到潜伏的箱顶就买（开盘高出箱顶 3% 以上不追）", "收盘跌破金品底就走"])
    expect(measure({ metric: "plan" })).toBe("按交易计划：")
    expect(measure({ metric: "judge" })).toBe("按规则自己的判断：")
    expect(measure({ metric: "hold" })).toBe("20 日")
  })
})
