import { describe, expect, it } from "vitest"
import type { Board, BoardResponse, PhaseSummary } from "./market"
import { yuan } from "@/pages/market/fmt"
import { amountLine, dayWord, freshness, heatStep, industryName, moodNote, phaseDays, readText, sessionNote, stockIndustry, tagSentence, whyLine } from "./marketWords"

const board = (b: Partial<Board> = {}): Board => ({
  at: "2026-10-12T10:42:30", day: "2026-10-12", traded: 5560, up: 2104, down: 3282, flat: 186, amount: 6.07e11, projected: 1.18e12,
  limits: { known: true, limit_up: 38, broken: 17, limit_down: 7, seal_rate: 38 / 55, pool: 58, promoted: 18, promo_rate: 0.31, height: 6, top_sealed: false },
  ladder: [], limit_list: [], industries: { l1: [], l2: [] },
  top: { level: "l2", name: "半导体", chg: 0.031, up_ratio: 0.78, second: 0.024 }, top1: { name: "电子", chg: 0.02, second: 0.01 },
  indexes: { "CN:IDX:000001": { last: 3812.4, chg: -0.0031, dist60: 0.032, state: 0, to_top: -0.04, to_bottom: 0.02 } }, ...b,
})
const phase = (p: Partial<PhaseSummary> = {}): PhaseSummary => ({ as_of: "2026-10-09", phase: "主升", days: 5, pending: null, path: [], recent: [], ...p })
const SESSION = { state: "trading" as const, label: "上午盘", day: "2026-10-12", time: "10:42:30" }
const resp = (r: Partial<BoardResponse> = {}): BoardResponse => ({
  market: "CN", mode: "live", session: SESSION, as_of: "2026-10-09",
  stale: false, why_not: null, error: null, fetched_at: "2026-10-12T10:42:30+08:00", board: board(), read: { label: "偏弱", better: [], worse: ["封板率低", "晋级率低", "最高板炸板"] },
  phase: phase(), events: [], yesterday: null, amount_before: 1.21e12, ...r,
})
const at = (t: string) => Date.parse(`2026-10-12T${t}+08:00`)
const NOW = at("10:42:40")

describe("the toolbar says whose numbers these are (redesign §5)", () => {
  it("names the day and the moment in every state", () => {
    expect(sessionNote(resp({ mode: "close", session: { ...SESSION, state: "pre", label: "未开盘", time: "08:50:00" } }), NOW).label)
      .toBe("截至 10-09 收盘 · 下一个交易日开盘后换成盘中的数")
    expect(sessionNote(resp({ mode: "close", session: { ...SESSION, state: "closed", label: "休市", day: "2026-10-11" } }), NOW).label)
      .toBe("截至 10-09 收盘 · 下一个交易日开盘后换成盘中的数")
    expect(sessionNote(resp({ mode: "close", as_of: "2026-10-12", session: { ...SESSION, state: "after", label: "已收盘", time: "20:00:00" } }), NOW).label)
      .toBe("截至 10-12 收盘")
    expect(sessionNote(resp({ mode: "auction", session: { ...SESSION, state: "pre", label: "未开盘", time: "09:20:00" } }), NOW).label)
      .toBe("集合竞价中 · 显示 10-09 收盘")
    expect(sessionNote(resp({ mode: "auction_result", session: { ...SESSION, state: "pre", label: "未开盘", time: "09:27:00" } }), NOW))
      .toMatchObject({ label: "A 股 集合竞价结果 09:25", dot: "live" })
    expect(sessionNote(resp(), NOW)).toMatchObject({ label: "A 股 上午盘 10:42:30", dot: "live", fresh: { text: "10 秒前更新", late: false } })
    // the numbers' own time, not the clock's: the source failed after 10:42:30, the page asked at 10:45:30
    expect(sessionNote(resp({ session: { ...SESSION, time: "10:45:30" } }), at("10:45:30")).label).toBe("A 股 上午盘 10:42:30")
    expect(sessionNote(resp({ session: { ...SESSION, state: "lunch", label: "午休", time: "12:10:00" } }), at("12:10:00")))
      .toMatchObject({ label: "A 股 午休 · 11:30 的数", dot: null })
    expect(sessionNote(resp({ session: { ...SESSION, state: "after", label: "已收盘", time: "16:00:00" } }), at("16:00:00")))
      .toMatchObject({ label: "A 股 今天收盘 · 盘中记录", dot: null })
    expect(sessionNote(resp({ market: "JP", mode: "close", session: { ...SESSION, state: "closed", label: "休市" } }), NOW).label)
      .toBe("截至 10-09 收盘（东京）")
  })
  it("turns amber when the live record is late or failing", () => {
    expect(sessionNote(resp(), at("10:45:30"))).toMatchObject({ dot: "warn", fresh: { late: true } })
  })
  it("never says 今天 when the numbers are not today's", () => {
    expect(dayWord(resp())).toBe("今天")
    expect(dayWord(resp({ mode: "auction_result" }))).toBe("今天")
    expect(dayWord(resp({ mode: "close" }))).toBe("10-09")
    expect(dayWord(resp({ mode: "auction" }))).toBe("10-09")
  })
})

describe("the line under the toolbar (redesign §5)", () => {
  it("says why the page shows a close, and when yesterday's update failed", () => {
    expect(whyLine(resp())).toBeNull()
    expect(whyLine(resp({ mode: "close", error: "TickFlow 500：Internal Server Error" }))).toBe("盘中行情取不到（TickFlow 500：Internal Server Error），下面是 10-09 收盘")
    expect(whyLine(resp({ mode: "close", why_not: "设置里关了盘中市场记录：页面用收盘数据" }))).toBe("设置里关了盘中市场记录：页面用收盘数据")
    expect(whyLine(resp({ stale: true, as_of: "2026-10-08" }))).toBe("日线只到 10-08（收盘后的更新没成功）：今天的涨跌照常；晋级率、连板、阶段、60 日线，行业的近 5 日、近 20 日和标签不知道")
    const evening = resp({ mode: "close", error: "TickFlow 500", session: { ...SESSION, state: "after", label: "已收盘", time: "19:00:00" } })
    expect(whyLine(evening)).toBeNull()                                  // the session is over: no live board to miss
  })
  it("says how many prices beyond today's limits were left out (10-13 09:25: twice the auction's price for 1,750 stocks)", () => {
    const beyond = "行情里 1,750 只股票的价格超出了今天的涨跌停，不可能是成交价，没有算进来"
    expect(whyLine(resp({ mode: "auction_result", board: board({ impossible: 1750 }) }))).toBe(beyond)
    expect(whyLine(resp({ stale: true, as_of: "2026-10-08", board: board({ impossible: 1750 }) })))
      .toBe(`${beyond}；日线只到 10-08（收盘后的更新没成功）：今天的涨跌照常；晋级率、连板、阶段、60 日线，行业的近 5 日、近 20 日和标签不知道`)
    expect(whyLine(resp({ board: board({ impossible: 0 }) }))).toBeNull()
  })
})

describe("the read and the phase", () => {
  it("lists what was weaker or stronger than the same minute yesterday", () => {
    expect(readText({ label: "偏弱", better: [], worse: ["封板率低", "晋级率低", "最高板炸板"] })).toBe("今天偏弱：封板率低、晋级率低、最高板炸板（和昨天同一时间比）")
    expect(readText({ label: "持平", better: [], worse: [] })).toBe("和昨天同一时间差不多")
    expect(readText(null)).toBeNull()
  })
  it("counts today in the phase only on a day it is showing", () => {
    expect(phaseDays(phase(), resp())).toBe(6)
    expect(phaseDays(phase(), resp({ mode: "close", session: { ...SESSION, state: "closed", label: "休市", day: "2026-10-11" } }))).toBe(5)
  })
  it("keeps the freshness words of the record", () => {
    const lunch = resp({ session: { ...SESSION, state: "lunch", label: "午休", time: "12:10:00" }, fetched_at: "2026-10-12T11:35:02+08:00" })
    expect(freshness(lunch, at("12:10:00"))).toEqual({ text: "行情到 11:35", late: false })
    expect(freshness(resp({ error: "TickFlow 500：Internal Server Error" }), NOW)?.late).toBe(true)
  })
})

describe("industries in words (redesign §3, §4, §6.3)", () => {
  it("shows 二级 names without Ⅱ; a stock's industry is 一级 · 二级", () => {
    expect(industryName("白酒Ⅱ")).toBe("白酒")
    expect(industryName("半导体")).toBe("半导体")
    expect(stockIndustry("食品饮料", "白酒Ⅱ")).toBe("食品饮料 · 白酒")
    expect(stockIndustry("輸送用機器", null)).toBe("輸送用機器")
    expect(stockIndustry(null, null)).toBeNull()
  })
  it("says the tag's two numbers, or that the last 5 bars were like the market", () => {
    expect(tagSentence({ tag: "转强", rel5: 0.033, prior15: 0.014 })).toBe("近 5 日比大盘 +3.3%，之前 15 日 +1.4%")
    expect(tagSentence({ tag: "一直弱", rel5: -0.052, prior15: -0.031 })).toBe("近 5 日比大盘 -5.2%，之前 15 日 -3.1%")
    expect(tagSentence({ tag: null, rel5: 0.004, prior15: 0.02 })).toBe("近 5 日和大盘差不多（+0.4%）")
    expect(tagSentence({ tag: null, rel5: null, prior15: null })).toBeNull()
  })
  it("shades by the largest move, four steps a side", () => {
    expect([0.0245, -0.0245, 0.0136, 0.0022, 0, -0.00004, null].map((v) => heatStep(v, 0.0245))).toEqual([4, -4, 3, 1, 0, 0, null])
    expect(heatStep(0.01, 0)).toBe(0)
  })
})

describe("the 成交额 line and the 情绪 note say whose numbers they are (redesign §2.2, §2.3, §5)", () => {
  const PRE = { ...SESSION, state: "pre" as const, label: "未开盘", time: "09:27:00" }
  it("the auction's result is the auction's, not a close against the day before", () => {
    const r = resp({ mode: "auction_result", session: PRE, amount_before: 1.92e12, board: board({ amount: 1.5e10, projected: null }) })
    expect(amountLine(r, yuan)).toBe("集合竞价成交 · 前一天全天 1.92 万亿")
    expect(moodNote(r)).toBe("集合竞价的结果（9:30 开盘后换成盘中的数）")
  })
  it("the session's, the day's over and a close keep theirs", () => {
    expect(amountLine(resp({ session: { ...SESSION, time: "10:12:00" } }), yuan)).toBe("前一天这时 — · 全天 1.21 万亿")   // before 10:30: no projection
    expect(amountLine(resp({ session: { ...SESSION, time: "13:53:30" } }), yuan)).toBe("全天约 1.18 万亿 · 前一天 1.21 万亿")
    const after = resp({ session: { ...SESSION, state: "after", label: "已收盘", time: "19:00:00" }, amount_before: 1.92e12, board: board({ amount: 1.55e12 }) })
    expect(amountLine(after, yuan)).toBe("前一天 1.92 万亿 · 少 19%")
    expect(moodNote(resp({ mode: "close", session: PRE }))).toBe("10-09 收盘的数")
    expect(moodNote(resp())).toBe("今天偏弱：封板率低、晋级率低、最高板炸板（和昨天同一时间比）")
  })
})
