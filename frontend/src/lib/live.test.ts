import { describe, expect, it } from "vitest"
import {
  awake, factsOf, goldLabels, lateInDay, lateText, liveAt, liveDay, type LiveMarket, priceAge, type LiveRow, merge, type MicroBox, type MicroGold, type MicroWave, minuteOf, nearestLevels,
  latentOf, placeOf, resonance, rowsOf, type Session, sessionText, statusOf, ticks, vwapNote, waveText,
  type AlertKind,
} from "./live"
import { type Box, NO_STRUCTURES } from "./rules"

const row = (r: Partial<LiveRow>): LiveRow => ({
  listing_id: "JP:7203", name: "トヨタ自動車", rule: null, rule_id: null, watched: false, status: null, stage: null, top: null, line: null, last: 2861,
  prev: 2845.5, chg: 0.0054, vol_ratio: null, to_top: null, live: true, ex_rights: false, alert: null, vwap: null, vwap_gap: null, ...r,
})
const market = (m: Partial<LiveMarket>): LiveMarket => ({
  type: "quotes", market: "JP", at: "2026-10-02T10:42:18+09:00", indexes: [], error: null, quote_at: null,
  source: { name: "yfinance_rt", delay: 20, approx_volume: true, approx_minutes: false },
  session: { state: "trading", label: "前场", day: "2026-10-02", elapsed: 102, total: 330 }, rows: [], ...m,
})

describe("the 盯盘 list", () => {
  it("takes each event as its market's state now", () => {
    const jp = market({}), cn = market({ market: "CN" })
    const later = market({ at: "2026-10-02T10:44:18+09:00" })
    expect(merge([cn, jp], [later])).toEqual([cn, later])
    expect(merge([], [jp, cn]).map((m) => m.market)).toEqual(["JP", "CN"])
  })
  it("lists both markets, the ones nearest their trigger first: furthest over the box top, then the closest under it", () => {
    const m = [
      market({ rows: [row({ listing_id: "JP:6861", rule: "上横上", to_top: 0.012 }), row({ listing_id: "JP:7203", watched: true }),
        row({ listing_id: "JP:6920", rule: "上横上", watched: true, to_top: -0.004 }), row({ listing_id: "JP:4063", rule: "上横上", to_top: -0.015 })] }),
      market({ market: "CN", rows: [row({ listing_id: "CN:300750", rule: "上横上", to_top: 0.003, live: false })] }),
    ]
    expect(rowsOf(m).map((r) => [r.listing_id, r.market])).toEqual([["JP:6861", "JP"], ["CN:300750", "CN"], ["JP:6920", "JP"], ["JP:4063", "JP"], ["JP:7203", "JP"]])
  })
  it("says a stock over its box top waits for the close, one within 1% is near, and otherwise what the scan said", () => {
    const r = (x: Partial<LiveRow>) => statusOf({ ...row(x), market: "JP" })
    expect(r({ rule: "上横上", to_top: 0.012 })).toEqual({ text: "越过上沿 · 待收盘确认", tone: "warn" })
    expect(r({ rule: "上横上", to_top: -0.004 })).toEqual({ text: "接近上沿", tone: "fg" })
    expect(r({ rule: "上横上", stage: "box", to_top: -0.04 })).toEqual({ text: "上横上 · 到 box", tone: "muted" })
    expect(r({ rule: "上横上", stage: "box", to_top: 0.02, live: false })).toEqual({ text: "上横上 · 到 box", tone: "muted" })
    expect(r({ watched: true })).toEqual({ text: "自选", tone: "muted" })
  })
  it("says first what the day's alerts and the ex-rights check found (spec §7.2)", () => {
    const r = (x: Partial<LiveRow>) => statusOf({ ...row(x), market: "JP" })
    const alert = (kind: "rule" | "box" | "line", status: "pending" | "confirmed" | "revoked" | "none", text = "") =>
      ({ kind, status, text, at: "2026-10-02T10:31:00" })
    expect(r({ ex_rights: true, rule: "上横上", to_top: 0.02 })).toEqual({ text: "疑似除权，今日不判断", tone: "muted" })
    expect(r({ rule: "上横上", alert: alert("rule", "pending") })).toEqual({ text: "盘中满足 · 待收盘确认", tone: "warn" })
    expect(r({ rule: "上横上", alert: alert("rule", "confirmed") })).toEqual({ text: "收盘确认", tone: "fg" })
    expect(r({ rule: "上横上", alert: alert("rule", "revoked") })).toEqual({ text: "收盘撤销", tone: "muted" })
    expect(r({ rule: "上横上", alert: alert("box", "none") })).toEqual({ text: "10:31 越过上沿", tone: "warn" })
    expect(r({ watched: true, alert: alert("line", "none", "涨破你画的线 2,860") })).toEqual({ text: "涨破你画的线 2,860", tone: "fg" })
  })
  it("says a line's alerts by their time and layer (design 2026-10-10), and a stock you watch by its mark", () => {
    const r = (x: Partial<LiveRow>) => statusOf({ ...row(x), market: "CN" })
    const alert = (kind: AlertKind) => ({ kind, status: "none" as const, text: "", at: "2026-10-12T10:31:00" })
    expect(r({ watched: true, status: "watching", alert: alert("auction") })).toEqual({ text: "10:31 开盘到线", tone: "warn" })
    expect(r({ watched: true, status: "watching", alert: alert("near") })).toEqual({ text: "10:31 逼近线 · 注意看", tone: "warn" })
    expect(r({ watched: true, status: "watching", alert: alert("touch") })).toEqual({ text: "10:31 已到线", tone: "warn" })
    expect(r({ watched: true, status: "holding", alert: alert("hold") })).toEqual({ text: "10:31 收盘前仍在失效线下", tone: "warn" })
    expect(r({ watched: true, status: "holding" })).toEqual({ text: "持有中", tone: "muted" })
    expect(r({ watched: true, status: "research" })).toEqual({ text: "待研究", tone: "muted" })
  })
  it("says the moment's facts the alert saw, as Feishu hears them", () => {
    expect(factsOf({ side: "up", move2: 0.012, vol3: 3.46, vol_ratio: 2.1, vwap_gap: 0.018, to_limit: 0.062, tags: ["大盘弱"],
      history: "同类提醒约六成当天碰线" })).toEqual(["2 分钟 +1.2%", "近 3 分钟量 3.5 倍", "量比 2.1", "均价线上 1.8 点", "离涨停 6.2 点", "大盘弱", "同类提醒约六成当天碰线"])
    expect(factsOf({ side: "down", vwap_gap: -0.004, to_limit: 0.071 })).toEqual(["均价线下 0.4 点", "离跌停 7.1 点"])
    expect(factsOf(null)).toEqual([])
  })
})

describe("the 分时图's time axis", () => {
  it("marks each open and close, whole hours clear of them, and joins the lunch's two ends", () => {
    expect(ticks([["09:00", "11:30"], ["12:30", "15:30"]])).toEqual([
      { i: 0, label: "9:00" }, { i: 60, label: "10:00" }, { i: 150, label: "11:30/12:30" }, { i: 240, label: "14:00" },
      { i: 330, label: "15:30" }])
    expect(ticks([["09:30", "11:30"], ["13:00", "15:00"]])).toEqual([
      { i: 0, label: "9:30" }, { i: 120, label: "11:30/13:00" }, { i: 180, label: "14:00" }, { i: 240, label: "15:00" }])
  })
})

describe("the 分时图's clock", () => {
  it("places a time on the trading day's minutes, lunch left out", () => {
    const jp: [string, string][] = [["09:00", "11:30"], ["12:30", "15:30"]]
    expect(minuteOf("2026-10-02T09:00:00", jp)).toBe(0)
    expect(minuteOf("2026-10-02T11:00:00", jp)).toBe(120)
    expect(minuteOf("2026-10-02T12:30:00", jp)).toBe(150)               // lunch takes no room
    expect(minuteOf("2026-10-02T15:30:00", jp)).toBe(330)
  })
})

describe("the K-line's forming bar (spec §8.5)", () => {
  it("follows a market's minutes while its day is open and prices come, never on 行情中断 or after the day", () => {
    const jp = market({})
    expect(liveAt([jp], "JP")).toBe(jp.at)
    expect(liveAt([jp], "CN")).toBeNull()
    expect(liveAt([market({ error: "立花证券今天还没登录" })], "JP")).toBeNull()
    expect(liveAt([market({ session: { ...jp.session, state: "closed" } })], "JP")).toBeNull()
    expect(liveAt([market({ source: null })], "JP")).toBeNull()        // demo data: never polled
  })
  it("the structures count today's bar while the chart shows it — through a 行情中断 too (review 10-13)", () => {
    const down = market({ error: "行情源超时", source: null })   // as /api/live says it while the feed is down
    expect(liveDay([down], "JP")).toBe(down.at)
    expect(liveDay([market({ session: { ...market({}).session, state: "closed" } })], "JP")).toBeNull()
    expect(liveDay([market({ source: null })], "JP")).toBeNull()
  })
})

describe("the sessions", () => {
  it("keep the computer awake while a market trades or is at lunch, not before the open or after the close (spec §3.3)", () => {
    const at = (state: "pre" | "trading" | "lunch" | "after" | "closed") => market({ session: { state, label: "", day: "", elapsed: 0, total: 330 } })
    expect(awake([at("trading"), at("closed")])).toBe(true)
    expect(awake([at("lunch")])).toBe(true)
    expect(awake([at("pre"), at("after"), at("closed")])).toBe(false)
  })
  it("say 行情中断 when the source fails, not the session as if prices were coming (spec §15.1)", () => {
    expect(sessionText(market({}))).toEqual({ text: "东京 前场", broken: false })
    expect(sessionText(market({ error: "立花证券今天还没登录" }))).toEqual({ text: "东京 行情中断", broken: true })
    expect(sessionText(market({ market: "CN", session: { state: "closed", label: "休市", day: "", elapsed: 0, total: 240 } })))
      .toEqual({ text: "沪深 休市", broken: false })
  })
})

describe("三箱共振", () => {
  const box = (b: Partial<Box>): Box => ({
    id: "b", scale: "long", status: "established", start: "2026-05-11", known_at: "2026-06-01", end: null, ended_at: null, end_reason: null,
    width: 0.2, length: null, shape: "平箱", context: "上横", position: "中继", excluded_before: null, superseded_at: null,
    segments: [], touches: [], events: [], ...b,
  })
  const brk = { box_id: "m", at: "2026-10-02T10:31:00", event: "break_up", value: 3.1, ref_ratio: 2.4, partial: false, known_at: "2026-10-02T10:31:00" }
  it("names the big box (宏观), its 金品 (中观) and today's small box breaking up (微观); all three resonate", () => {
    const s = { ...NO_STRUCTURES, boxes: [box({ id: "L" }), box({ id: "S", scale: "short" })], gold: [{ small_box_id: "S", big_box_id: "L", kind: "上品", variant: false, known_at: "" }] }
    const r = resonance(s, [brk], 5, "2026-10-02")
    expect(r).toEqual({ macro: "宏观 · 大箱（自 05-11）", meso: "中观 · 金品（上品）", micro: "微观 · 5 分钟小箱上破 · 量 2.4 倍", label: "三箱共振" })
    // two small boxes confirmed before the big one share its known_at: 中观 is the one the K-line names (not superseded)
    const tie = { ...s, gold: [{ small_box_id: "S1", big_box_id: "L", kind: "下品", variant: false, known_at: "2026-06-01", superseded_at: "2026-06-01" },
      { small_box_id: "S2", big_box_id: "L", kind: "上品", variant: false, known_at: "2026-06-01", superseded_at: null }] }
    expect(resonance(tie, [brk], 5, "2026-10-02").meso).toBe("中观 · 金品（上品）")
  })
  it("lets a short flat box stand for two (不硬凑大箱), and says nothing for a break on another day", () => {
    const flat = { ...NO_STRUCTURES, boxes: [box({ id: "S", scale: "short", shape: "平箱" })] }
    expect(resonance(flat, [brk], 1, "2026-10-02").label).toBe("两箱共振")
    expect(resonance(flat, [{ ...brk, at: "2026-10-01T14:00:00" }], 1, "2026-10-02")).toEqual({ macro: null, meso: null, micro: null, label: null })
    const curved = { ...NO_STRUCTURES, boxes: [box({ id: "S", scale: "short", shape: "曲箱" })] }
    expect(resonance(curved, [brk], 1, "2026-10-02").label).toBeNull()
  })
  it("finds the latest box by its time, not by where its scale puts it in the list (boxes come short, mid, long)", () => {
    // an old mid box ended in March; the latest one is this month's short flat box: 两箱共振
    const s = { ...NO_STRUCTURES, boxes: [box({ id: "S", scale: "short", shape: "平箱", start: "2026-09-14" }),
      box({ id: "M", scale: "mid", shape: "曲箱", start: "2026-01-05", end: "2026-03-20" })] }
    expect(resonance(s, [brk], 5, "2026-10-02").label).toBe("两箱共振")
  })
  it("names the big box's latest 金品, and a break on the bar still forming as a process value (量≈)", () => {
    const s = { ...NO_STRUCTURES, boxes: [box({ id: "L" })], gold: [
      { small_box_id: "S1", big_box_id: "L", kind: "下品", variant: false, known_at: "2026-06-20" },
      { small_box_id: "S2", big_box_id: "L", kind: "上品", variant: false, known_at: "2026-09-01" }] }
    const r = resonance(s, [{ ...brk, partial: true }], 5, "2026-10-02")
    expect([r.meso, r.micro]).toEqual(["中观 · 金品（上品）", "微观 · 5 分钟小箱上破 · 量≈ 2.4 倍"])
  })
  it("takes the day's break on the most volume (当天放量的小箱体), whatever order the breaks come in", () => {
    const flat = { ...NO_STRUCTURES, boxes: [box({ id: "S", scale: "short", shape: "平箱" })] }
    const later = { ...brk, box_id: "n", at: "2026-10-02T10:32:00", ref_ratio: 0.2 }
    expect(resonance(flat, [later, brk], 1, "2026-10-02").micro).toBe("微观 · 1 分钟小箱上破 · 量 2.4 倍")
    expect(resonance(flat, [brk, later], 1, "2026-10-02").micro).toBe("微观 · 1 分钟小箱上破 · 量 2.4 倍")
  })
})

describe("the 分时图's readings", () => {
  const jp: [string, string][] = [["09:00", "11:30"], ["12:30", "15:30"]]
  it("places a time across several days side by side, each day on its own clock", () => {
    const days = ["2026-09-30", "2026-10-01", "2026-10-02"]
    expect(placeOf("2026-10-01T09:00:00", days, jp, 330)).toBe(330)
    expect(placeOf("2026-10-02T12:30:00", days, jp, 330)).toBe(660 + 150)
    expect(placeOf("2026-09-25T10:00:00", days, jp, 330)).toBe(0)                          // before the first day shown
    expect(placeOf("2026-10-03T10:00:00", days, jp, 330)).toBe(990)                        // after the last
  })
  it("finds the nearest box top above the price and the nearest bottom below it, to the left (阻力位、支撑位)", () => {
    const box = (b: Partial<MicroBox>): MicroBox => ({ box_id: "b", scale: "short", status: "established", start: "2026-10-01T09:30:00",
      known_at: "2026-10-01T10:00:00", end: null, ended_at: null, end_reason: null, top: 0, bottom: 0, shape: null, ...b })
    const boxes = [box({ box_id: "a", top: 105, bottom: 101 }), box({ box_id: "b", top: 110, bottom: 107 }),
      box({ box_id: "c", top: 103, bottom: 99, status: "proto" })]
    expect(nearestLevels(boxes, 104)).toEqual({ above: 105, below: 101 })
    expect(nearestLevels(boxes, 106)).toEqual({ above: 107, below: 105 })                  // a top below is support now (顶底互换)
    expect(nearestLevels([], 100)).toEqual({ above: null, below: null })
  })
  it("says where the price stands against the day's average, and warns 5 points above it", () => {
    expect(vwapNote(0.012)).toEqual({ text: "白线在均价线上方 1.2 个点", warn: false })
    expect(vwapNote(-0.008)).toEqual({ text: "白线在均价线下方 0.8 个点", warn: false })
    expect(vwapNote(0.057)).toEqual({ text: "白线在均价线上方 5.7 个点：远离均价线（经验说法，没有经过检验）", warn: true })
    // it is about the close (尾盘): the next day only in the last 30 minutes or after the close
    expect(vwapNote(0.057, true)?.text).toBe("白线在均价线上方 5.7 个点：远离均价线，尾盘还这样，次日容易回落（经验说法，没有经过检验）")
    expect(vwapNote(null)).toBeNull()
    const s = (state: Session["state"], elapsed: number): Session => ({ state, label: "", day: "2026-10-02", elapsed, total: 330 })
    expect([lateInDay(s("trading", 299)), lateInDay(s("trading", 300)), lateInDay(s("after", 330)), lateInDay(s("lunch", 150))])
      .toEqual([false, true, true, false])
  })
  it("labels a wave with its volume against the wave before, and what that means", () => {
    const w = (v: Partial<MicroWave>): MicroWave => ({ start: "", end: "", dir: "up", start_price: 1, end_price: 2, volume: 1, peak: 1,
      total_ratio: 1.4, peak_ratio: 1.5, new_extreme: true, flag: null, open: false, ...v })
    expect(waveText(w({}))).toBe("量 1.5×")
    expect(waveText(w({ peak_ratio: 0.6, flag: "新高量不足" }))).toBe("量 0.6× · 新高量不足")
    expect(waveText(w({ peak_ratio: null, total_ratio: null, new_extreme: null }))).toBe("")      // the first wave: nothing before
    expect(waveText(w({ open: true, peak_ratio: 0.7, flag: "新高量不足" }))).toBe("量≈ 0.7×（还在走）")
  })
})

describe("分时金品", () => {
  const g = (o: Partial<MicroGold>): MicroGold => ({ kind: "下品", variant: false, big_box_id: "big", small_box_id: "s1",
    known_at: "2026-10-02T10:00:00", superseded_at: null, inner_distance: 0.004, above_top: null, ...o })
  it("names each bigger box's last small box, never one a later small box replaced", () => {
    const labels = goldLabels([g({ small_box_id: "s0", superseded_at: "2026-10-02T10:00:00" }), g({}),
      g({ big_box_id: "bigger", kind: "上品" }), g({ small_box_id: "s2", kind: "天品", variant: true })])
    expect([...labels]).toEqual([["s1", "下品/上品"], ["s2", "变异天品"]])
    expect(goldLabels(undefined).size).toBe(0)
  })
})

describe("a live price not updated for 3 minutes is said (the 10-12 rule)", () => {
  const S = (state: Session["state"]): Session => ({ state, label: "上午盘", day: "2026-10-13", elapsed: 72, total: 240 })
  const now = (t: string, z = "+08:00") => Date.parse(`2026-10-13T${t}${z}`)
  it("ages a price by the exchange's clock, with or without an offset", () => {
    expect(priceAge("2026-10-13T10:42:30+08:00", "CN", S("trading"), now("10:43:00"))).toEqual({ seconds: 30, late: false })
    expect(priceAge("2026-10-13T10:38:00", "CN", S("trading"), now("10:42:30"))).toEqual({ seconds: 270, late: true })
    expect(priceAge("2026-10-13T10:38:00", "JP", S("trading"), now("10:42:30", "+09:00"))).toEqual({ seconds: 270, late: true })
  })
  it("says nothing outside the trading minutes, nor in the first 3 minutes of each half", () => {
    for (const st of ["pre", "lunch", "after", "closed"] as const) expect(priceAge("2026-10-13T11:30:00", "CN", S(st), now("12:10:00"))).toBeNull()
    expect(priceAge("2026-10-13T11:30:00", "CN", S("trading"), now("13:01:30"))).toBeNull()   // the afternoon's first fetch not in yet
    expect(priceAge("2026-10-13T11:30:00", "CN", S("trading"), now("13:04:00"))?.late).toBe(true)
    expect(priceAge(null, "CN", S("trading"), now("10:00:00"))).toBeNull()
  })
  it("words it by the minute", () => {
    expect(lateText({ seconds: 270, late: true })).toBe("行情 4 分钟没更新")
  })
})

describe("the 潜伏 tab (盯盘 follows yours only, 10-12)", () => {
  it("lists the others by the whole-market quote, the nearest their box top first — either side — never with an alert", () => {
    const row = (listing_id: string, to_top: number | null) => ({ listing_id, name: null, rule: "上品", rule_id: "x", stage: null, top: 10, line: 10.15,
      last: 9.9, chg: 0.01, to_top, at: "2026-10-13T10:42:20" })
    const got = latentOf([{ market: "CN", session: { state: "trading", label: "上午盘", day: "2026-10-13", elapsed: 72, total: 240 },
      rows: [row("CN:000002", -0.03), row("CN:000004", 0.01), row("CN:000006", null), row("CN:000007", 1.32), row("CN:000008", -0.005)] }, undefined])
    // one far past its top (a +132% run the scan still calls 潜伏) is no breakout to watch for: last of those with a top
    expect(got.map((r) => r.listing_id)).toEqual(["CN:000008", "CN:000004", "CN:000002", "CN:000007", "CN:000006"])
    expect(got.every((r) => r.alert === null && !r.watched && r.market === "CN" && r.live)).toBe(true)
  })
})
