import { describe, expect, it } from "vitest"
import type { Settings } from "./api"
import { briefOn, type BriefItem, doing, eventLine, inBrief, itemLink, type JobProgress, type Jobs, type JobState, NO_VIEW, pollEvery, type Screen, screenLink, screenPath, type ScreenRow, screenRows, stageSummary, toCSV, took, viewText, WHERE_PRESETS, whereName } from "./jobs"

describe("the 今日 feed and the 选股 export", () => {
  it("says what happened in the rule's words", () => {
    const base = { market: "CN" as const, day: "2026-10-02", rule_name: "放量突破" }
    expect(eventLine({ ...base, kind: "signal", signal_date: "2026-10-02", from_watch: true })).toBe("放量突破 · 信号 2026-10-02（来自潜伏名单）")
    expect(eventLine({ ...base, kind: "advance", was: "box", stage: "up" })).toBe("放量突破 · box → up")
    expect(eventLine({ ...base, kind: "invalid", stage: "box" })).toBe("放量突破 · 停在 box，没有出信号")
    // a stage by its name (names(): 中箱, 向上突破), never the rule's id for it
    expect(eventLine({ ...base, kind: "advance", was: "box", stage: "up", was_label: "中箱", stage_label: "向上突破" })).toBe("放量突破 · 中箱 → 向上突破")
    expect(eventLine({ ...base, kind: "watch", stage: "box", stage_label: "中箱" })).toBe("放量突破 · 进入潜伏（到 中箱）")
    expect(eventLine({ kind: "data", market: "JP", day: null, status: "delayed", text: "数据还没齐" })).toBe("数据还没齐")
    // the close's verdict on an intraday alert (design 14: 收盘确认 / 撤销 → 今日收件箱)
    expect(eventLine({ kind: "live", market: "JP", day: "2026-10-02", verdict: "confirmed", at: "2026-10-02T10:31:00", text: "上横上 v3 · 盘中满足突破" }))
      .toBe("收盘确认 · 10:31 盘中预警：上横上 v3 · 盘中满足突破")
    expect(eventLine({ kind: "live", market: "JP", day: "2026-10-02", verdict: "revoked", at: "2026-10-02T13:05:00", text: "x" }))
      .toBe("收盘撤销 · 13:05 盘中预警：x")
  })
  it("writes CSV Excel opens: a BOM, quotes where a cell needs them", () => {
    const csv = toCSV(["名称", "说明"], [["贵州茅台", 'a "b", c'], ["x", null]])
    expect(csv).toBe('﻿名称,说明\n贵州茅台,"a ""b"", c"\nx,\n')
  })
  it("quotes a cell with a carriage return alone (Excel breaks the row there otherwise)", () => {
    expect(toCSV(["a"], [["x\ry"]])).toBe('\ufeffa\n"x\ry"\n')
  })
})

describe("the 选股 table", () => {
  it("sums up a row's stages: how long they lasted, then its first measured values", () => {
    const d = {
      listing_id: "CN:600519", date: "2026-09-30", status: "signal" as const, deepest_stage: "hold", closest_miss: null,
      distances: [], unknown_reasons: [],
      bindings: [{ stage: "box", kind: "structure", id: "b1", start: "2026-08-01", end: "2026-09-01", at: null, bars: 22 },
        { stage: "up", kind: "event", id: null, start: null, end: null, at: "2026-09-02", bars: 1 }],
      conditions: [{ stage: "up", id: "volume", kind: "at", expr: "VOL_RATIO(5) >= $vol", op: ">=", value: 2.4, threshold: 2, status: "pass" as const },
        { stage: "hold", id: "keep", kind: "all", expr: "C > box.top", op: null, value: 1, threshold: null, status: "pass" as const }],
    }
    expect(stageSummary(d)).toBe("box 22 根 · volume 2.40")   // kept before names: by ids, as it was
    expect(stageSummary(null)).toBe("")
    // by name and in its unit; a yes / no (a formula event's when, true as 1) has no value to show
    const named = { ...d, conditions: [...d.conditions,
      { stage: "up", id: "when", kind: "when", expr: "CROSS(C, MA(C, 20))", op: null, value: 1, threshold: null, status: "pass" as const }],
      names: { stages: { box: "中箱", up: "向上突破", hold: "站稳" }, conds: { "up.volume": "突破量" }, units: { "up.volume": "倍" } } }
    expect(stageSummary(named)).toBe("中箱 22 根 · 突破量 2.40 倍")
  })
})

describe("how often the daily state is asked for", () => {
  const job = (running: boolean, status: string, progress: JobProgress | null = null) => ({ running, status, progress }) as unknown as JobState
  it("is quick while anything runs, slow while a market only waits for its data, and slower when nothing runs", () => {
    expect(pollEvery({ markets: [job(true, "scanning"), job(false, "done")] } as Jobs)).toBe(2_000)
    expect(pollEvery({ markets: [job(true, "updating", { step: "update", since: "", waiting: true })] } as Jobs)).toBe(15_000)   // the ready window: hours
    expect(pollEvery({ markets: [job(false, "done")] } as Jobs)).toBe(30_000)
    expect(pollEvery(undefined)).toBe(30_000)
    // a turn just begun has not said what it does yet (a scan of demo data is over in seconds): asked for at once
    expect(pollEvery({ markets: [job(true, "done", { step: "manual", since: "" })] } as Jobs)).toBe(2_000)
    expect(pollEvery({ markets: [job(true, "updating", { step: "update", since: "", phase: "日线", done: 3, total: 40 })] } as Jobs)).toBe(2_000)
  })
})

describe("what a running market is doing", () => {
  const at = (progress: JobProgress | null, status = "scanning", message: string | null = null) =>
    ({ market: "CN", running: true, status, message, progress }) as unknown as JobState
  it("names the rule, its step and how far the step is", () => {
    expect(doing(at({ step: "scan", since: "", i: 2, n: 3, rule: "爆大量", phase: "整理命中原因", done: 1200, total: 4290, fraction: 0.6 })))
      .toBe("扫描第 2 / 3 条规则「爆大量」：整理命中原因 1,200 / 4,290")
    expect(doing(at({ step: "scan", since: "", i: 1, n: 1, rule: "爆大量", phase: "读取数据", done: 0, total: 0, fraction: 0.04 })))
      .toBe("扫描第 1 / 1 条规则「爆大量」：读取数据")
    expect(doing(at({ step: "scan", since: "", fraction: 0 }))).toBe("准备扫描")
    // a pause or a cancel asked for: the line still says where the scan is (the controls say what was asked)
    expect(doing(at({ step: "scan", since: "", i: 2, n: 3, rule: "爆大量", phase: "逐只判定", done: 0, total: 0, pausing: true })))
      .toBe("扫描第 2 / 3 条规则「爆大量」：逐只判定")
    expect(doing(at({ step: "scan", since: "", fraction: 0 }, "scanning", "排队中：等别的全市场运行（另一个市场的扫描、验证或校准）做完"))).toBe("排队中：等别的全市场运行（另一个市场的扫描、验证或校准）做完")
  })
  it("says the fetch, the day's research and the check in the same words", () => {
    expect(doing(at({ step: "update", since: "", phase: "日线", done: 3, total: 40 }, "updating"))).toBe("更新数据：日线 3 / 40")
    expect(doing(at({ step: "update", since: "" }, "updating"))).toBe("更新数据")
    expect(doing(at({ step: "research", since: "", done: 1, total: 3 }, "done"))).toBe("AI 研究新信号 1 / 3")
    expect(doing(at({ step: "check", since: "" }, "done"))).toBe("抽样核对")
    expect(doing(at({ step: "update", since: "", waiting: true }, "updating"))).toBe("等当天的数据就绪")
    expect(doing(at(null, "backfilling"))).toBe("补历史")                     // the turn has said nothing yet
    expect(doing(at({ step: "manual", since: "" }, "done"))).toBe("准备中")
  })
  it("says how long it has taken", () => {
    const t0 = new Date("2026-10-05T14:00:00").getTime()
    expect(took("2026-10-05T14:00:00", t0 + 42_000)).toBe("42 秒")
    expect(took("2026-10-05T14:00:00", t0 + 185_000)).toBe("3 分 05 秒")
    expect(took("2026-10-05T14:00:00", t0 + 3_725_000)).toBe("1 小时 02 分")
    expect(took("2026-10-05T14:00:00", t0 - 5_000)).toBe("0 秒")               // a clock a little behind: never negative
  })
})

describe("briefOn", () => {
  it("follows the profile until it is set", () => {
    const s = { "brief.CN": "auto", "brief.JP": "auto", "profile.CN": "prod", "profile.JP": "test" } as unknown as Settings
    expect(briefOn(s, "CN")).toBe(true)
    expect(briefOn(s, "JP")).toBe(false)
    expect(briefOn({ ...s, "brief.JP": "on" } as Settings, "JP")).toBe(true)
    expect(briefOn({ ...s, "brief.CN": "off" } as Settings, "CN")).toBe(false)
  })
})

describe("选股的附加条件", () => {
  it("asks for the rule's list with 同时满足 and every 附加条件, each only when there is one", () => {
    expect(screenPath("tpl_tian_pin")).toBe("/screen/tpl_tian_pin")
    expect(screenPath("tpl_tian_pin", null, ["MA(BAR_LEN, 20) <= 0.03"])).toBe("/screen/tpl_tian_pin?where=MA%28BAR_LEN%2C+20%29+%3C%3D+0.03")
    expect(screenPath("t", null, ["A > 1", " ", "CIRC_MV / 1e8 <= 100"])).toBe("/screen/t?where=A+%3E+1&where=CIRC_MV+%2F+1e8+%3C%3D+100")
    expect(screenPath("a b", { id: "x", status: "watch" }, [])).toBe("/screen/a%20b?also=x&also_status=watch")
    expect(screenPath("t", null, [], "l2:白酒Ⅱ")).toBe("/screen/t?industry=l2%3A%E7%99%BD%E9%85%92%E2%85%A1")   // 行业 from the 市场 page
  })
  it("has 小盘股 ready for each market (日股 has no free float: its 总市值), in 亿 so the numbers are easy to change", () => {
    expect(WHERE_PRESETS.map((p) => [p.label, p.expr])).toEqual([["近期 K 线短小", "MA(BAR_LEN, 20) <= 0.03"],
      ["小盘股", "CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000"], ["多个外资持股", "CN: FOREIGN_HOLDERS >= 2"]])
  })
  it("has 多个外资持股 for A 股 (10-17) and says what 2021 on showed", () => {
    const p = WHERE_PRESETS.find((x) => x.label === "多个外资持股")!
    for (const w of ["陆股通", "日股没有", "2021 年以来"]) expect(p.hint).toContain(w)
    expect(whereName("CN: FOREIGN_HOLDERS >= 2")).toBe("多个外资持股")
  })
})

describe("the list 选股 shows, and the K 线 page's 命中 with it", () => {
  const row = (id: string, status: ScreenRow["status"], extra: Partial<ScreenRow> = {}): ScreenRow => ({
    listing_id: id, market: id.startsWith("JP") ? "JP" : "CN", name: id === "CN:000001" ? "平安银行" : null, industry: null, industry2: null, tags: [],
    status, depth: status === "fail" ? 0 : 1, signal_date: null, detail: null, recent_signal: null, ...extra })
  const s = { rows: [row("CN:000001", "signal"), row("CN:600000", "watch"), row("JP:7203", "watch"), row("CN:600001", "fail"),
    row("CN:600002", "fail", { recent_signal: "2026-09-28" }), row("CN:600003", "unknown")] } as Screen
  const ids = (v: Partial<typeof NO_VIEW>) => screenRows(s, { ...NO_VIEW, ...v }).map((r) => r.listing_id)
  it("keeps the rows of its market, group or funnel step, and search, in the scan's order", () => {
    expect(ids({})).toEqual(["CN:000001", "CN:600000", "JP:7203", "CN:600002", "CN:600003"])   // 全部: not the ones that missed
    expect(ids({ market: "CN", group: "watch" })).toEqual(["CN:600000"])
    expect(ids({ group: "recent" })).toEqual(["CN:000001", "CN:600002"])
    expect(ids({ step: 0 })).toEqual(["CN:000001", "CN:600000", "JP:7203", "CN:600001", "CN:600002", "CN:600003"])
    expect(ids({ q: "平安" })).toEqual(["CN:000001"])
    expect(ids({ q: "7203" })).toEqual(["JP:7203"])
    expect(screenRows(undefined, NO_VIEW)).toEqual([])
  })
  it("says the view in words and links back to it", () => {
    const v = { ...NO_VIEW, rule: "tpl_calm_breakout", also: { id: "tpl_shang_pin", status: "watch" as const },
      where: ["MA(BAR_LEN, 20) <= 0.03", "TURNOVER < 5"], market: "CN" as const, group: "signal" as const }
    expect(viewText(v, "平稳横盘后向上突破", "上品")).toBe("平稳横盘后向上突破 · 同时满足「上品」（命中或潜伏） · 近期 K 线短小、TURNOVER < 5 · 命中 · A 股")
    expect(viewText({ ...NO_VIEW, rule: "r", step: 1 }, "规则", null, "向上突破")).toBe("规则 · 走到「向上突破」")
    expect(screenLink(v)).toBe("/screen?rule=tpl_calm_breakout&also=tpl_shang_pin&also_status=watch&where=MA%28BAR_LEN%2C+20%29+%3C%3D+0.03&where=TURNOVER+%3C+5")
    expect(whereName("CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000")).toBe("小盘股")
    expect(whereName("CIRC_MV / 1e8 <= 100")).toBe("CIRC_MV / 1e8 <= 100")
  })
})


describe("开盘前简报's items (2026-10-10)", () => {
  const item = (x: Partial<BriefItem>): BriefItem => ({ id: 1, rule_id: "tpl_shang_pin", name: "上品", wheres: [], also: null, also_status: null,
    mode: "list", markets: ["CN", "JP"], problem: null, ...x })
  it("knows a 选股 view already in the briefing: the same rule, 附加条件 in any order, the same 同时满足", () => {
    const items = [item({ wheres: ["A > 1", "B > 2"] }), item({ id: 2, also: "tpl_tian_pin", also_status: "watch" })]
    expect(inBrief(items, "tpl_shang_pin", null, ["B > 2", " A > 1"])?.id).toBe(1)
    expect(inBrief(items, "tpl_shang_pin", null, ["A > 1"])).toBeUndefined()
    expect(inBrief(items, "tpl_shang_pin", { id: "tpl_tian_pin", status: "watch" }, [])?.id).toBe(2)
    expect(inBrief(items, "tpl_shang_pin", { id: "tpl_tian_pin", status: "signal" }, [])).toBeUndefined()
  })
  it("links an item back to its 选股 view", () => {
    expect(itemLink(item({ wheres: ["A > 1"], also: "x", also_status: "watch" }))).toBe("/screen?rule=tpl_shang_pin&also=x&also_status=watch&where=A+%3E+1")
  })
})
