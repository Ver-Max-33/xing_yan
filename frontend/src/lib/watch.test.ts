import { describe, expect, it } from "vitest"
import { lineText, nextUp, reviewCSV, type WatchCondition, type WatchEntry } from "./watch"

const cond = (c: Partial<WatchCondition>): WatchCondition => ({
  id: 1, kind: "fail", text: "", op: "<", price: null, due: null, created_at: "", fired_day: null, fired_value: null, ...c,
})
const entry = (conditions: WatchCondition[]): WatchEntry => ({
  listing_id: "JP:7203", market: "JP", name: "トヨタ自動車", status: "watching", status_name: "观察中", source: "K 线页",
  added_at: "2026-10-01T17:02:00", added_day: "2026-10-01", added_price: 2845.5, updated_at: "",
  reason: "箱体很干净", for: "箱体结构完整\n9/24 上调全年预期", against: "", note: "", conditions,
  performance: { since: 0.012, benchmark: "JP:IDX:TOPIX", bench: 0.004, excess: 0.008, day: "2026-10-02", close: 2880 },
  record: [{ text: "加入观察：K 线页", at: "2026-10-01T17:02:00" }],
})

describe("the 观察 list", () => {
  it("shows what to look at next: a fail line it crossed or is near first, then the next review, then the nearest line", () => {
    const line = cond({ id: 1, price: 2737, distance: -0.038 })
    const review = cond({ id: 2, kind: "review", op: null, due: "2026-11-05", days_left: 35 })
    expect(nextUp(entry([line, review]), "JP")).toEqual({ text: "下次复核 11-05", warn: false })
    expect(nextUp(entry([line]), "JP")).toEqual({ text: "失效线 2,737.0", warn: false })
    expect(nextUp(entry([cond({ ...line, distance: -0.012 }), review]), "JP")).toEqual({ text: "接近失效线 -1.2%", warn: true })
    expect(nextUp(entry([cond({ ...line, fired_day: "2026-10-02" }), review]), "JP")).toEqual({ text: "触发了失效线 10-02", warn: true })
    expect(nextUp(entry([cond({ ...review, fired_day: "2026-11-05" })]), "JP")).toEqual({ text: "到了复核日 11-05", warn: true })
    expect(nextUp(entry([cond({ kind: "manual", op: null, text: "停产延长公告" })]), "JP")).toBeNull()
  })
  it("exports the review as CSV: status, since when, how it has done against the benchmark, the texts and conditions", () => {
    const csv = reviewCSV([entry([cond({ price: 2737, text: "收盘跌回箱顶" })])]).split("\n")
    expect(csv[0]).toBe("﻿市场,代码,名称,状态,来源,加入日,加入价,截至,至今,基准,基准同期,超额,为什么关注,支持,反对 / 风险,我的笔记,条件,记录")
    expect(csv[1]).toBe('JP,7203,トヨタ自動車,观察中,K 线页,2026-10-01,2845.5,2026-10-02,1.20%,TOPIX,0.40%,0.80%,箱体很干净,"箱体结构完整')
    expect(csv[2]).toBe('9/24 上调全年预期",,,收盘跌回箱顶（收盘 < 2737）,2026-10-01 17:02 加入观察：K 线页')
  })
})

describe("the lines that alert on a watched stock (养箱's trigger)", () => {
  it("says a level with its distance and a sloped line by its two points", () => {
    expect(lineText({ id: "a", kind: "horizontalStraightLine", points: [{ date: "2026-09-30", price: 1786.5 }], price: 1786.5, distance: 0.05 }, "CN"))
      .toBe("水平线 1,786.50（+5.0%）")
    expect(lineText({ id: "b", kind: "rayLine", points: [{ date: "2026-06-01", price: 1500 }, { date: "2026-09-30", price: 1600 }], price: null, distance: null }, "CN"))
      .toBe("射线 06-01 1,500.00 → 09-30 1,600.00")
  })
})
