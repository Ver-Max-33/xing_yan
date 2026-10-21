import { describe, expect, it } from "vitest"
import type { Box, BoxEvent, FalseBottom, Gold, LabelResult, Pressure, Structures } from "@/lib/rules"
import { asOf, barLength, boxCandles, boxSince, boxesOnDay, boxText, dayLines, dayMarks, dayTop, DEFAULT_LAYERS, failLine, fbId, goldName, edgeTo, labelNote, ladder, LAYER_SWATCH, levelsWith, levelTags, lineAt, markOf, pendingBreak, pressureText, SCALE_STYLE, spanOf, stackTags, strongSet, visibleBoxes, visibleSlants } from "./structures"

const box = (o: Partial<Box>): Box => ({
  id: "b1", scale: "mid", status: "established", start: "2026-01-05", known_at: "2026-01-20", end: "2026-03-02",
  ended_at: "2026-03-09", end_reason: "上破离箱", width: 0.082, length: 41, shape: "flat", context: "上横", position: "中继",
  excluded_before: null, superseded_at: null,
  segments: [{ from: "2026-01-05", to: "2026-03-02", top: 12, bottom: 11 }],
  touches: [{ date: "2026-01-08", side: "top", price: 12, known_at: "2026-01-12" },
            { date: "2026-02-20", side: "bottom", price: 11, known_at: "2026-02-24" }],
  events: [{ date: "2026-03-02", event: "break_up", value: 2.4, known_at: "2026-03-02" }],
  ...o,
})

const S: Structures = {
  boxes: [box({}), box({ id: "p1", status: "proto", known_at: "2026-01-10", superseded_at: "2026-01-20", end: null, ended_at: null,
                         segments: [{ from: "2026-01-05", to: null, top: 12.1, bottom: 11.1 }], touches: [], events: [] })],
  slant: [], pivots: [{ date: "2026-01-08", kind: "high", price: 12, known_at: "2026-01-12" }],
  swings: [], gold: [], false_bottoms: [{ box_id: "b1", source: "C 点", date: "2026-02-20", price: 11, known_at: "2026-03-02", end: "2026-03-20",
                   end_reason: "失守", box_top: 12, box_bottom: 10 }],
}

describe("replay shows only what was known on the day", () => {
  it("everything at the latest bar, each box as it was last drawn", () => {
    const now = { ...S, boxes: [box({})] }
    expect(asOf(now, null)).toBe(now)
    expect(asOf(S, null).boxes.map((b) => b.id)).toEqual(["b1"])
  })
  it("a box redrawn as it grows shows the version of the day", () => {
    const v1 = box({ id: "b1.0", known_at: "2026-01-20", superseded_at: "2026-02-10", segments: [{ from: "2026-01-05", to: "2026-03-02", top: 11.8, bottom: 11 }] })
    const v2 = box({ id: "b1.1", known_at: "2026-02-10" })
    const T = { ...S, boxes: [v1, v2] }
    expect(asOf(T, "2026-02-01").boxes.map((b) => b.id)).toEqual(["b1.0"])
    expect(asOf(T, "2026-02-10").boxes.map((b) => b.id)).toEqual(["b1.1"])
    expect(asOf(T, null).boxes.map((b) => b.id)).toEqual(["b1.1"])
  })
  it("before a box is confirmed it is not there, its proto is", () => {
    const s = asOf(S, "2026-01-15")
    expect(s.boxes.map((b) => b.id)).toEqual(["p1"])
    expect(s.pivots).toHaveLength(1)
  })
  it("a proto goes once it is superseded", () => {
    expect(asOf(S, "2026-01-25").boxes.map((b) => b.id)).toEqual(["b1"])
  })
  it("a box whose end is not known yet runs to the day, without its end, length or reason", () => {
    const b = asOf(S, "2026-03-05").boxes[0]
    expect(b.segments).toEqual([{ from: "2026-01-05", to: "2026-03-05", top: 12, bottom: 11 }])
    expect([b.end, b.ended_at, b.end_reason, b.length]).toEqual([null, null, null, null])
    expect(b.events.map((e) => e.event)).toEqual(["break_up"])           // the break itself is known on its day
    expect(asOf(S, "2026-03-09").boxes[0].segments[0].to).toBe("2026-03-02")
  })
  it("touches and marks appear when they are confirmed", () => {
    const b = asOf(S, "2026-02-22").boxes[0]
    expect(b.touches.map((t) => t.date)).toEqual(["2026-01-08"])
    expect(asOf(S, "2026-02-22").false_bottoms).toHaveLength(0)
    expect(asOf(S, "2026-03-02").false_bottoms).toHaveLength(1)
  })
  it("a 假底 runs to the replay day until its end is known (失守 only from that day)", () => {
    expect(asOf(S, "2026-03-10").false_bottoms[0]).toMatchObject({ end: null, end_reason: null })
    expect(asOf(S, "2026-03-20").false_bottoms[0]).toMatchObject({ end: "2026-03-20", end_reason: "失守" })
  })
})

describe("what the chart draws", () => {
  it("each box level has its own colour and weight, the big box heaviest", () => {
    const colors = new Set(Object.values(SCALE_STYLE).map((s) => s.color))
    expect(colors.size).toBe(3)
    expect(SCALE_STYLE.long.width).toBeGreaterThan(SCALE_STYLE.mid.width)
    expect(SCALE_STYLE.mid.width).toBeGreaterThan(SCALE_STYLE.short.width)
    expect(LAYER_SWATCH.boxLong?.color).toBe("--box-long")
    expect(LAYER_SWATCH.falseBottoms).toEqual({ color: "--false-bottom", dash: true })
  })
  it("boxes by scale and status follow the layer switches", () => {
    expect(visibleBoxes(S.boxes, DEFAULT_LAYERS).map((b) => b.id)).toEqual(["b1"])        // protos off by default
    expect(visibleBoxes(S.boxes, { ...DEFAULT_LAYERS, protos: true, boxMid: false }).map((b) => b.id)).toEqual([])
  })
  it("draws a 金品's small box when the 金品 layer is on, though small boxes are off", () => {
    // spec §8.2: 金品标出类别，只标现在还是金品的那个小箱 — with 小箱 off (the default) the 金品 layer showed nothing
    const small = box({ id: "s1", scale: "short" }), other = box({ id: "s2", scale: "short" })
    const gold: Gold[] = [{ small_box_id: "s1", big_box_id: "b1", kind: "upper", variant: false, known_at: "2026-01-20", superseded_at: null }]
    expect(visibleBoxes([small, other], DEFAULT_LAYERS, [], gold).map((b) => b.id)).toEqual(["s1"])
    expect(visibleBoxes([small, other], { ...DEFAULT_LAYERS, gold: false }, [], gold).map((b) => b.id)).toEqual([])
  })
  it("names every 金品 a small box is: 天品 of the box it broke out of and 上品 of the one it sits at the top of", () => {
    const g = (kind: string, big: string, variant = false): Gold => ({ small_box_id: "s", big_box_id: big, kind, variant, known_at: "2026-09-28" })
    expect(goldName([g("天品", "b1"), g("上品", "b2"), g("天品", "b3")], "s")).toBe("天品 · 上品")
    expect(goldName([g("下品", "b1", true)], "s")).toBe("变异下品")
    expect(goldName([g("下品", "b1")], "other")).toBeNull()
  })

  it("averages the candles' length inside a box: (high − low) ÷ the close before, from its start to the day", () => {
    const bars = [
      { date: "2026-01-02", high: 10.2, low: 9.8, close: 10 },
      { date: "2026-01-05", high: 10.3, low: 9.9, close: 10 },    // 0.4 / 10
      { date: "2026-01-06", high: 10.2, low: 10.0, close: 10.1 },  // 0.2 / 10
      { date: "2026-01-07", high: 10.5, low: 9.6, close: 10.0 },   // 0.9 / 10.1 — after the day asked
    ]
    expect(barLength(bars, "2026-01-05", "2026-01-06")).toBeCloseTo(0.03, 10)
    expect(barLength(bars, "2026-01-05", "2026-01-07", true)).toBeCloseTo(0.03, 10)   // an ended box: its break bar left out
    expect(barLength(bars, "2026-01-02", "2026-01-02")).toBeNull()       // the first loaded bar has no close before it
    expect(barLength(bars, "2025-12-01", "2025-12-31")).toBeNull()       // no bars loaded there
  })

  it("reads a box's candle length over its own bars: to its end (the break bar left out), else to the day shown", () => {
    const seen: unknown[][] = []
    const barLen = (...a: unknown[]) => { seen.push(a); return 0.016 }
    const b = (end: string | null, ended: string | null) => box({ start: "2026-01-05", end, ended_at: ended })
    expect(boxCandles(b("2026-02-10", "2026-02-12"), null, barLen)).toBe(0.016)
    boxCandles(b(null, null), "2026-03-02", barLen)
    boxCandles(b(null, null), null, barLen)
    expect(seen).toEqual([["2026-01-05", "2026-02-10", true], ["2026-01-05", "2026-03-02", false], ["2026-01-05", "9999-12-31", false]])
    expect(boxCandles(b(null, null), null)).toBeNull()   // no bars to read it on
    expect(boxText(box({}), 0.0162)).toBe("中箱 41 根 · 箱宽 8.2% · K 线均长 1.6%")
  })

  it("describes the big box standing on the day and the 中箱 standing inside it", () => {
    const bx = (id: string, scale: Box["scale"], start: string, known: string, end: string | null, ended: string | null): Box => ({
      id, scale, status: "established", start, known_at: known, end, ended_at: ended, end_reason: ended ? "上破离箱" : null, width: 0.1,
      length: null, shape: null, context: null, position: null, excluded_before: null, superseded_at: null,
      segments: [{ from: start, to: end, top: 11, bottom: 10 }], touches: [], events: [] })
    const old = bx("x:big:1.3", "mid", "2025-01-02", "2025-02-01", "2025-03-01", "2025-03-08")
    const big = bx("x:big:2.9", "long", "2025-04-01", "2025-07-01", null, null)
    const innerOld = bx("x:inner:1.2", "mid", "2025-05-01", "2025-06-01", "2025-06-20", "2025-06-26")
    const inner = bx("x:inner:2.4", "mid", "2025-08-01", "2025-08-20", null, null)
    expect(boxesOnDay([old, innerOld, big, inner]).map((b) => b.id)).toEqual(["x:big:2.9", "x:inner:2.4"])
    expect(boxesOnDay([old, innerOld, big]).map((b) => b.id)).toEqual(["x:big:2.9"])     // no 中箱 standing inside now
    expect(boxesOnDay([old]).map((b) => b.id)).toEqual(["x:big:1.3"])                    // nothing standing: the last one
  })

  it("dates a box from the day it was first drawn, and its last redraw beside it", () => {
    expect(boxSince({ known_at: "2025-12-09", first_known: "2025-02-20" })).toBe("2025-02-20 成立 · 2025-12-09 重画")
    expect(boxSince({ known_at: "2025-02-20", first_known: "2025-02-20" })).toBe("2025-02-20 成立")
    expect(boxSince({ known_at: "2025-02-20" })).toBe("2025-02-20 成立")
  })
  it("draws a slanted 金品 (变异) when the 金品 layer is on, though slant boxes are off", () => {
    const sl = (id: string) => ({ id, status: "established", start: "2026-01-05", known_at: "2026-01-12", end: null, ended_at: null, end_reason: null,
      width: 0.05, top: [{ date: "2026-01-05", price: 10 }, { date: "2026-01-20", price: 11 }], bottom: [{ date: "2026-01-05", price: 9.5 }, { date: "2026-01-20", price: 10.4 }] })
    const gold: Gold[] = [{ small_box_id: "v1", big_box_id: "b1", kind: "sky", variant: true, known_at: "2026-01-12", superseded_at: null }]
    expect(visibleSlants([sl("v1"), sl("v2")], DEFAULT_LAYERS, gold).map((x) => x.id)).toEqual(["v1"])
    expect(visibleSlants([sl("v1"), sl("v2")], { ...DEFAULT_LAYERS, slant: true }, []).map((x) => x.id)).toEqual(["v1", "v2"])
  })
  it("always shows the boxes behind the judgement, whatever the layers say", () => {
    const off = { ...DEFAULT_LAYERS, boxShort: false, boxMid: false, boxLong: false }
    expect(visibleBoxes(S.boxes, off, ["b1"]).map((b) => b.id)).toEqual(["b1"])
  })
  it("labels read in the method's own words", () => {
    expect(boxText(box({}))).toBe("中箱 41 根 · 箱宽 8.2%")
    expect(boxText(box({ length: null, scale: "long", width: 0.3 }))).toBe("大箱 · 箱宽 30.0%")
    expect(pressureText({ date: "2026-03-01", box_id: "x", bottom: 2960, top: 3100, space: 0.081, none_above: false }, 1))
      .toEqual({ text: "左侧压力 2,960.0 · 箱外空间 +8.1%", short: true })
    expect(pressureText({ date: "2026-03-01", box_id: null, space: null, none_above: true }, 2))
      .toEqual({ text: "上方没有左侧压力", short: false })
  })
})

describe("edgeTo", () => {
  it("cuts the last piece of a slanted edge at the replay day", () => {
    expect(edgeTo([[0, 10], [10, 20]], 5)).toEqual([[0, 10], [5, 15]])
  })
  it("carries an edge whose end is not known yet on to the replay day, along the same line", () => {
    expect(edgeTo([[0, 10], [10, 20]], 14)).toEqual([[0, 10], [10, 20], [14, 24]])
  })
  it("leaves an edge alone without a replay day", () => {
    expect(edgeTo([[0, 10], [10, 20]], null)).toEqual([[0, 10], [10, 20]])
  })
})

describe("labelNote", () => {
  const r = (o: Partial<LabelResult>): LabelResult => ({ id: "l", listing_id: "CN:600000", category: "flat", is_box: true, grp: "tune", verdict: null,
    box_id: "b1", consistent: true, false_draw: null, overlap: 0.91, ...o })
  it("says how a sample box and the algorithm's agree, in the agreement measures (手画对照)", () => {
    expect(labelNote(r({}))).toBe("重叠 0.91 · 一致")
    expect(labelNote(r({ consistent: false, overlap: 0.42 }))).toBe("重叠 0.42 · 不一致")
    expect(labelNote(r({ box_id: null, consistent: false, overlap: undefined }))).toBe("算法没画箱")
  })
  it("and for a not-a-box sample, whether the algorithm drew one there", () => {
    expect(labelNote(r({ is_box: false, false_draw: true, consistent: false }))).toBe("算法在这里画了箱")
    expect(labelNote(r({ is_box: false, false_draw: false, box_id: null }))).toBe("算法没画（对）")
  })
})

describe("金品 is the big box's last small box", () => {
  const g = (o: Partial<Gold>): Gold => ({ small_box_id: "s1", big_box_id: "b1", kind: "下品", variant: false, known_at: "2026-02-01",
    superseded_at: null, ...o })
  const T: Structures = { ...S, gold: [g({ superseded_at: "2026-02-20" }), g({ small_box_id: "s2", kind: "上品", known_at: "2026-02-20" })] }
  it("shows the one no later small box replaced, and in a replay the one there was on the day", () => {
    expect(asOf(T, null).gold.map((x) => x.small_box_id)).toEqual(["s2"])
    expect(asOf(T, "2026-02-10").gold.map((x) => x.small_box_id)).toEqual(["s1"])
    expect(asOf(T, "2026-02-25").gold.map((x) => x.small_box_id)).toEqual(["s2"])
  })
})

describe("what the chart puts first (当前形态, a focus, the judgement) and what it keeps faint", () => {
  const bx = (id: string, scale: Box["scale"], start: string, end: string | null, top = 11, bottom = 10): Box => ({
    id, scale, status: "established", start, known_at: start, end, ended_at: end, end_reason: end ? "上破离箱" : null, width: 0.1,
    length: null, shape: null, context: null, position: null, excluded_before: null, superseded_at: null,
    segments: [{ from: start, to: end, top, bottom }], touches: [], events: [] })
  const old = bx("x:big:1.3", "mid", "2025-01-02", "2025-03-01", 9.6, 8.6)
  const big = bx("x:big:2.9", "long", "2025-04-01", null, 11.6, 10.1)
  const inner = bx("x:inner:2.4", "mid", "2025-08-01", null, 11.6, 10.6)
  const small = bx("x:small:7.1", "short", "2025-09-10", null, 11.6, 11.3)
  const oldSmall = bx("x:small:3.2", "short", "2025-02-10", "2025-02-25", 9.6, 9.3)
  const gold: Gold[] = [
    { small_box_id: "x:small:7.1", big_box_id: "x:big:2.9", kind: "upper", variant: false, known_at: "2025-09-15", superseded_at: null },
    { small_box_id: "x:small:7.1", big_box_id: "x:inner:2.4", kind: "lower", variant: false, known_at: "2025-09-15", superseded_at: null },
    { small_box_id: "x:small:3.2", big_box_id: "x:big:1.3", kind: "upper", variant: false, known_at: "2025-02-15", superseded_at: null }]
  const fb = (box_id: string, date: string, end: string | null) => ({ box_id, source: "C 点" as const, date, price: 9.3, known_at: date,
    end, end_reason: end ? ("新箱" as const) : null, box_top: 9.6, box_bottom: 8.6 })
  const T: Structures = { ...S, boxes: [old, oldSmall, big, inner, small], gold, false_bottoms: [fb("x:big:1.3", "2025-03-01", "2025-04-01")] }

  it("当前形态: the big box standing, the 中箱 inside it, their 金品's small box — not a box that ended before", () => {
    expect([...strongSet(T, null, [])].sort()).toEqual(["x:big:2.9", "x:inner:2.4", "x:small:7.1"])
  })
  it("nothing standing: the box last drawn, its 金品 and its 假底 (just broken out: what holds it now)", () => {
    const U: Structures = { ...T, boxes: [old, oldSmall] }
    expect([...strongSet(U, null, [])].sort()).toEqual(["fb:x:big:1.3:2025-03-01", "x:big:1.3", "x:small:3.2"])
  })
  it("a 假底 still running is part of it, whichever box it came from", () => {
    const U: Structures = { ...T, false_bottoms: [fb("x:big:1.3", "2025-03-01", null)] }
    expect(strongSet(U, null, []).has("fb:x:big:1.3:2025-03-01")).toBe(true)
  })
  it("a focus comes first, with what belongs to it: a box its 金品 and 假底, a 金品's small box its big boxes", () => {
    expect([...strongSet(T, "x:big:1.3", [])].sort()).toEqual(["fb:x:big:1.3:2025-03-01", "x:big:1.3", "x:small:3.2"])
    expect([...strongSet(T, "x:small:7.1", [])].sort()).toEqual(["x:big:2.9", "x:inner:2.4", "x:small:7.1"])
    expect([...strongSet(T, "fb:x:big:1.3:2025-03-01", [])].sort()).toEqual(["fb:x:big:1.3:2025-03-01", "x:big:1.3", "x:small:3.2"])
  })
  it("then the structures behind the judgement on screen (a 金品 stage's id is its big and small box)", () => {
    expect([...strongSet(T, null, ["x:big:1.3>x:small:3.2"])].sort()).toEqual(["fb:x:big:1.3:2025-03-01", "x:big:1.3", "x:small:3.2"])
    expect(strongSet(T, "x:big:2.9", ["x:big:1.3"]).has("x:big:1.3")).toBe(false)   // your focus over the judgement
  })
  it("a focus the day shown does not have yet (a replay before it) leaves 当前形态", () => {
    expect([...strongSet({ ...T, boxes: [old, oldSmall] }, "x:big:2.9", [])].sort())
      .toEqual(["fb:x:big:1.3:2025-03-01", "x:big:1.3", "x:small:3.2"])
  })
  it("a 金品 is named for the box it is drawn with: 上品 of the big box, not 上品 · 下品 of both; with both drawn, by each one's name", () => {
    expect(goldName(gold, "x:small:7.1")).toBe("上品 · 下品")
    expect(goldName(gold, "x:small:7.1", new Set(["x:big:2.9", "x:small:7.1"]))).toBe("上品")
    expect(goldName(gold, "x:small:7.1", new Set(["x:big:2.9", "x:inner:2.4"]), T.boxes)).toBe("大箱上品 · 中箱下品")
  })
})

describe("the prices of 当前形态 at the right edge", () => {
  const P = (o: Partial<Pressure> = {}): Pressure => ({ date: "2025-09-30", box_id: "y", bottom: 12.78, top: 13.4, space: 0.111, none_above: false, ...o })
  const big: Box = { ...box({ id: "x:big:2.9", scale: "long", end: null, ended_at: null, end_reason: null }),
    segments: [{ from: "2025-04-01", to: null, top: 11.13, bottom: 10.15 }] }
  const inner: Box = { ...box({ id: "x:inner:2.4", end: null, ended_at: null, end_reason: null }),
    segments: [{ from: "2025-08-01", to: null, top: 11.15, bottom: 10.6 }] }
  const T: Structures = { ...S, boxes: [big, inner], false_bottoms: [{ ...S.false_bottoms[0], box_id: "x:big:2.9", price: 10.49, end: null, end_reason: null }] }
  const strong = new Set(["x:big:2.9", "x:inner:2.4", "fb:x:big:2.9:2026-02-20"])
  it("each box's top and bottom, the 假底 and the left pressure, with how far each is from the close; two edges a hair apart read as one, the bigger box's", () => {
    const tags = levelTags(T, strong, 11.5, 2, P())
    expect(tags.map((t) => t.text)).toEqual(["左侧压力 12.78 +11.1%", "大箱顶 11.13 −3.2%", "中箱底 10.60 −7.8%", "假底 10.49 −8.8%", "大箱底 10.15 −11.7%"])
    expect(tags.filter((t) => t.tone === "box").map((t) => t.scale)).toEqual(["long", "mid", "long"])   // in its box's colour
    expect(tags.find((t) => t.text.startsWith("左侧压力"))?.tone).toBe("pressure")
    expect(levelTags(T, strong, 11.5, 2, P({ bottom: 12.2, space: 0.061 }))[0].tone).toBe("short")   // under the 10% minimum
    // the close inside the old box: its top, where the trapped supply ends, as the side panel's 价位梯 has it
    expect(levelTags(T, strong, 11.5, 2, P({ bottom: 11.2, top: 12.1, space: 0 }))[0]).toEqual({ price: 12.1, text: "左侧压力上沿 12.10 +5.2%", tone: "short" })
  })
  it("a box that ended (just broken out of) reads as 原箱顶 / 原箱底: where it may hold", () => {
    const ended: Box = { ...big, end: "2025-09-20", ended_at: "2025-09-22", segments: [{ from: "2025-04-01", to: "2025-09-20", top: 11.13, bottom: 10.15 }] }
    expect(levelTags({ ...T, boxes: [ended], false_bottoms: [] }, new Set(["x:big:2.9"]), 11.5, 2, null).map((t) => t.text))
      .toEqual(["原大箱顶 11.13 −3.2%", "原大箱底 10.15 −11.7%"])
  })
  it("tags go to their prices, the nearest first, and any that would overlap move to the nearest free place in the pane", () => {
    expect(stackTags([50, 55, 120], 16, 0, 200)).toEqual([50, 66, 120])
    expect(stackTags([195, 190], 16, 0, 200)).toEqual([192, 176])            // kept inside the pane
    expect(stackTags([100], 16, 0, 200, [104])).toEqual([88])                // 昨收 / 涨停's labels kept clear
    expect(stackTags([-30, 260], 16, 0, 200)).toEqual([8, 192])              // a price off the pane: at its edge
  })
  it("crowded, they keep the prices' order from the top down — the nearest the close stays (review 10-13, you chose)", () => {
    // the nearest first, then three around it: a higher price is a smaller y
    expect(stackTags([100, 90, 105, 95], 16, 0, 200)).toEqual([100, 68, 116, 84])
    const at = stackTags([180, 170, 185, 175, 190], 16, 0, 200)               // too many near the bottom: all moved up, in order
    const byWant = [170, 175, 180, 185, 190].map((w) => at[[180, 170, 185, 175, 190].indexOf(w)])
    expect(byWant).toEqual([...byWant].sort((a, b) => a - b))
    expect(Math.max(...at)).toBeLessThanOrEqual(192)
    expect(stackTags([100, 96, 104], 16, 0, 200, [120])).toEqual([100, 84, 136])   // clear of 昨收 / 涨停's labels, still in order
  })
})

describe("a structure's days, by its id (what a line in the side panel brings into view)", () => {
  it("a box, a slant small box (a 变异金品's) and a 假底 — an id it does not know, none", () => {
    const T: Structures = { ...S, slant: [{ id: "s1", status: "established", start: "2026-02-02", known_at: "2026-02-09", end: null, ended_at: null,
      end_reason: null, width: 0.05, top: [], bottom: [] }] }
    expect(spanOf(T, "b1")).toEqual({ start: "2026-01-05", end: "2026-03-02" })
    expect(spanOf(T, "s1")).toEqual({ start: "2026-02-02", end: null })
    expect(spanOf(T, fbId(S.false_bottoms[0]))).toEqual({ start: "2026-02-20", end: "2026-03-20" })
    expect(spanOf(T, "gone")).toBeNull()
  })
})

describe("当前形态 as a plan: the 价位梯 in the side panel", () => {
  // bars from 2026-07-01 on, one a weekday; closes `c` (a number, or one per bar from the end)
  const bars = (n: number, c: number | ((k: number) => number)) => {
    const out: { date: string; close: number }[] = []
    for (let d = new Date("2026-07-01T00:00:00Z"); out.length < n; d.setUTCDate(d.getUTCDate() + 1)) {
      if (d.getUTCDay() % 6 !== 0) out.push({ date: d.toISOString().slice(0, 10), close: typeof c === "number" ? c : c(n - 1 - out.length) })
    }
    return out
  }
  const mid = box({ id: "m1", start: "2026-07-01", known_at: "2026-08-03", end: null, ended_at: null, end_reason: null, length: null,
    segments: [{ from: "2026-07-01", to: null, top: 12.33, bottom: 11.43 }] })
  const fb = (o: Partial<FalseBottom> = {}): FalseBottom => ({ box_id: "m0", source: "C 点", date: "2026-08-20", price: 12.07, known_at: "2026-08-25",
    end: null, end_reason: null, box_top: 12, box_bottom: 11, ...o })
  const P: Pressure = { date: "2026-10-09", box_id: "old", bottom: 12.47, top: 13, space: 0.014, none_above: false }
  const T: Structures = { ...S, boxes: [mid], false_bottoms: [fb()] }

  it("突破线, 左侧压力, the edges, 现价 and the 失效线 that acts, highest first, each with the structure it is", () => {
    const r = ladder(T, 12.3, P, bars(70, 12.25))
    expect(r.map((x) => [x.label, +x.price.toFixed(2), x.id])).toEqual([
      ["突破线", 12.51, "m1"], ["左侧压力", 12.47, "old"], ["上沿", 12.33, "m1"], ["现价", 12.3, null], ["失效线", 12.07, "fb:m0:2026-08-20"],
      ["下沿", 11.43, "m1"]])
    expect(r[1]).toMatchObject({ warn: true, note: "空间不到 10%" })          // room under the 10% minimum
    expect(r[4]).toMatchObject({ note: "假底", color: "--false-bottom", dash: true })
    expect(ladder(T, 12.3, { ...P, space: 0.12 }, bars(70, 12.25))[1]).toMatchObject({ warn: false, note: "" })
    // inside the old box (left downward, its top still above): the trapped supply runs to its top
    expect(ladder(T, 12.3, { ...P, bottom: 12.1, top: 12.8, space: 0 }, bars(70, 12.25))[0])
      .toMatchObject({ label: "左侧压力", price: 12.8, note: "在压力区里", warn: true })
  })
  it("another period's structures carry its own band: 突破线 and 失效线 at it, said as such", () => {
    const week = { ...T, tol: 0.035, period: "week" as const }   // a week's band (design 2026-10-12-structures-every-period)
    const r = ladder(week, 12.3, null, bars(70, 12.25))
    expect(r.find((x) => x.kind === "break")).toMatchObject({ price: 12.33 * 1.035 })
    expect(r.find((x) => x.kind === "break")!.hint).toContain("上沿 × 1.035")
    const shallow = { ...week, false_bottoms: [] }   // no 假底: the box's bottom × (1 − the band) acts
    expect(ladder(shallow, 12.3, null, bars(70, 12.25)).find((x) => x.kind === "fail")).toMatchObject({ price: 11.43 * 0.965, note: "中箱底 × 0.965" })
  })
  it("on a minute chart the daily 价位梯's lines are drawn as 日线价位: named so, against the minute close, no 现价", () => {
    const r = ladder(T, 12.3, null, bars(70, 12.25))
    expect(dayMarks(r, 12.3, 2).map((m) => m.text)).toEqual(["日线突破线 12.51 +1.7%", "日线上沿 12.33 +0.2%", "日线失效线 12.07 −1.9%", "日线下沿 11.43 −7.1%"])
    expect(dayMarks(r, 12.3, 2)[0]).toMatchObject({ heavy: true, dash: false, tone: "box", scale: "mid" })
  })
  it("a 大箱 with the 中箱 inside: each line named by its box; two a hair apart read as one, the bigger box's", () => {
    const big = { ...mid, id: "x:big:1", scale: "long" as const, start: "2026-03-02", segments: [{ from: "2026-03-02", to: null, top: 12.33, bottom: 10.9 }] }
    const inner = { ...mid, id: "x:inner:1", start: "2026-08-03", segments: [{ from: "2026-08-03", to: null, top: 12.35, bottom: 11.6 }] }
    const r = ladder({ ...T, boxes: [big, inner], false_bottoms: [] }, 12.3, null, bars(70, 12.25))
    expect(r.map((x) => x.label)).toEqual(["大箱突破线", "大箱上沿", "现价", "中箱下沿", "失效线", "大箱下沿"])
    expect(r.find((x) => x.kind === "fail")).toMatchObject({ note: "中箱底 × 0.985", id: "x:inner:1" })   // 11.6 × 0.985 over 10.9 × 0.985
  })
  it("a line pointed at on the 价位梯 is drawn on the chart as itself: its price, its box's colour, from its structure's start", () => {
    const r = ladder(T, 12.46, P, bars(70, 12.4))
    const at = (label: string) => markOf(r.find((x) => x.label === label)!, 12.46, 2)
    expect(at("突破线")).toEqual({ price: 12.33 * 1.015, text: "突破线 12.51 +0.4%", tone: "box", scale: "mid", from: "2026-07-01", dash: false, heavy: true })
    expect(at("左侧压力")).toMatchObject({ text: "左侧压力 12.47 +0.1%", tone: "short", from: null })   // under the 10% minimum
    expect(at("失效线")).toMatchObject({ text: "失效线 12.07 −3.1%", tone: "falseBottom", dash: true, heavy: true, from: "2026-08-20" })
    expect(at("现价")).toBeNull()
    // at the right edge among 当前形态's prices; the one already at that price (the box's top) gives way to it
    const tags = levelTags(T, new Set(["m1"]), 12.46, 2, null)
    expect(levelsWith(tags, at("突破线")!).map((t) => t.text)).toEqual(["突破线 12.51 +0.4%", "中箱顶 12.33 −1.0%", "中箱底 11.43 −8.3%"])
    expect(levelsWith(tags, at("上沿")!).map((t) => t.text)).toEqual(["上沿 12.33 −1.0%", "中箱底 11.43 −8.3%"])
  })
  it("a box just left upward: 原上沿 / 原下沿 and no 突破线", () => {
    const left = { ...mid, end: "2026-09-28", ended_at: "2026-09-29", end_reason: "上破离箱" }
    expect(ladder({ ...T, boxes: [left], false_bottoms: [] }, 13.1, null, bars(70, 12.9)).map((x) => x.label)).toEqual(["现价", "原上沿", "原下沿", "失效线"])
  })
  it("the 失效线: a 金品's small-box bottom over a 假底 over the box's; one a close has crossed since, an old 金品, one over the close: not", () => {
    const small = box({ id: "s1", scale: "short", start: "2026-09-21", known_at: "2026-09-25", end: null, ended_at: null, end_reason: null,
      segments: [{ from: "2026-09-21", to: null, top: 12.4, bottom: 12.1 }] })
    const g: Gold = { small_box_id: "s1", big_box_id: "m1", kind: "上品", variant: false, known_at: "2026-09-25", superseded_at: null }
    const U = { ...T, boxes: [mid, small], gold: [g] }
    expect(failLine(U, bars(70, 12.25))).toMatchObject({ price: 12.1, note: "上品小箱底", id: "s1", color: "--box-short" })
    expect(failLine(U, bars(70, (k) => (k === 3 ? 12.08 : 12.25)))?.note).toBe("假底")                // closed under 12.1 three bars ago
    expect(failLine({ ...U, gold: [{ ...g, known_at: "2026-07-02" }] }, bars(70, 12.25))?.note).toBe("假底")   // known more than 60 bars ago
    expect(failLine({ ...U, false_bottoms: [fb({ price: 12.4 })] }, bars(70, 12.25))?.price).toBe(12.1)   // over the close: not a 失效线
    expect(failLine({ ...T, false_bottoms: [] }, bars(70, 12.25))).toMatchObject({ note: "中箱底 × 0.985", id: "m1" })
    expect(failLine(T, [])).toBeNull()
  })
  it("a 变异金品's bottom is its slanted line where it stood the day the box was drawn, by bar", () => {
    const pts = [{ date: "2026-09-01", price: 10 }, { date: "2026-09-11", price: 11 }]
    const days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
    expect(lineAt(pts, "2026-09-07", days)).toBeCloseTo(10.5)         // 4 bars of 8 (a weekend is no bar)
    expect(lineAt(pts, "2026-09-11", days)).toBe(11)
    expect(lineAt(pts, "2026-08-31", days)).toBeNull()                // before the line
    expect(lineAt(pts, "2026-09-14", days)).toBeNull()                // after it
  })
})

// ---- review 10-13 (category E) -------------------------------------------------------------------------------------------
describe("日线价位 and 分时's daily top read each box as it stands now", () => {
  // the API sends every version of a box: one superseded long ago, standing in its own time and later in the list, is
  // not 当前形态 (real data, CN:300439: a 2025 version's 大箱突破线 11.51 drawn on the 5-minute chart; the box was left at 9.55)
  const old = box({ id: "big:1", scale: "long", start: "2025-01-02", known_at: "2025-03-01", end: null, ended_at: null, end_reason: null,
    superseded_at: "2025-06-11", segments: [{ from: "2025-01-02", to: null, top: 11.34, bottom: 9.78 }], touches: [], events: [] })
  const now = box({ id: "big:2", start: "2026-08-10", known_at: "2026-08-20", end: "2026-09-21", ended_at: "2026-09-21",
    segments: [{ from: "2026-08-10", to: "2026-09-21", top: 9.55, bottom: 9.08 }], touches: [], events: [] })
  const T: Structures = { boxes: [now, old], slant: [], pivots: [], swings: [], gold: [], false_bottoms: [] }
  const bars = [{ date: "2026-10-09", close: 10.3 }, { date: "2026-10-12", close: 10.21 }]
  it("日线价位: the latest versions' lines only", () => {
    const texts = dayLines(T, 10.25, bars, 2).map((m) => m.text)
    expect(texts).toEqual(["日线原上沿 9.55 −6.8%", "日线原下沿 9.08 −11.4%"])
  })
  it("分时's daily top: the latest version's", () => {
    expect(dayTop(T, bars, 2)).toBe(9.55)
  })
})

describe("a slanted edge in the replay stops on the day, on its own line", () => {
  // CN:002016 replayed on 2026-08-14: slant 63's top ran 4.41 → 5.01 (its 09-21 point) and was drawn ending at 5.01 on
  // 08-14; the engine on the bars up to 08-14 ends it at 4.75
  const sl = { id: "s1", status: "established", start: "2026-01-05", known_at: "2026-01-06", end: "2026-01-09", ended_at: "2026-01-09",
    end_reason: "超长", width: 0.1, top: [{ date: "2026-01-05", price: 10 }, { date: "2026-01-09", price: 14 }],
    bottom: [{ date: "2026-01-05", price: 9 }, { date: "2026-01-09", price: 13 }] }
  const T: Structures = { boxes: [], slant: [sl], pivots: [], swings: [], gold: [], false_bottoms: [] }
  const dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
  it("no point after the replay day: the last is the line's price on that day, by bar", () => {
    const s = asOf(T, "2026-01-06", dates)
    expect(s.slant[0].top).toEqual([{ date: "2026-01-05", price: 10 }, { date: "2026-01-06", price: 11 }])
    expect(s.slant[0].bottom.at(-1)).toEqual({ date: "2026-01-06", price: 10 })
  })
  it("by calendar without the bar days — never the later point's price", () => {
    expect(asOf(T, "2026-01-06").slant[0].top.at(-1)).toEqual({ date: "2026-01-06", price: 11 })
  })
})

describe("pendingBreak (review 10-13)", () => {
  const ev = (date: string, event: string): BoxEvent => ({ date, event, value: null, known_at: date })
  const box = (events: BoxEvent[], ended_at: string | null = null): Pick<Box, "ended_at" | "events"> => ({ ended_at, events })
  it("says a break still waiting to come back or leave, not 仍在箱内", () => {
    // 宁德时代 weekly 10-12: closed 13% under the 中箱 bottom, R bars not yet up
    expect(pendingBreak(box([ev("2026-09-04", "touch_top"), ev("2026-09-25", "break_down")]))).toBe("down")
    expect(pendingBreak(box([ev("2026-10-12", "break_up")]))).toBe("up")
  })
  it("is nothing once the box came back, left or ended", () => {
    expect(pendingBreak(box([ev("2026-10-08", "break_down"), ev("2026-10-09", "return")]))).toBeNull()
    expect(pendingBreak(box([ev("2026-10-12", "break_up"), ev("2026-10-08", "break_down"), ev("2026-10-09", "return")]))).toBe("up")
    expect(pendingBreak(box([ev("2026-09-01", "break_up")], "2026-09-08"))).toBeNull()
  })
})
