import type { KLineData } from "klinecharts"
import { describe, expect, it, vi } from "vitest"
import type { Box } from "@/lib/rules"
import { RULE_TEMPLATE, STRUCT_TEMPLATE } from "./paint"
import { EMPTY_RULE, EMPTY_STRUCT, type PlanLine, type StructView } from "./structures"
import type { Palette } from "./theme"

vi.mock("klinecharts", () => ({ registerIndicator: () => {} }))   // the library needs a window; the painter does not

/** A 2D context that keeps only what the test reads: the dash in force at each stroke, through save / restore. */
function fakeCtx(dash: number[]) {
  const stack: number[][] = []
  const strokes: number[][] = []
  let cur = dash
  const noop = () => {}
  const ctx = {
    save: () => { stack.push(cur) }, restore: () => { cur = stack.pop() ?? cur },
    setLineDash: (d: number[]) => { cur = d }, getLineDash: () => cur,
    strokeRect: () => { strokes.push(cur) }, stroke: () => { strokes.push(cur) },
    fillRect: noop, beginPath: noop, arc: noop, moveTo: noop, lineTo: noop, closePath: noop, fill: noop, fillText: noop,
    measureText: () => ({ width: 10 }),
  }
  return { ctx: ctx as unknown as CanvasRenderingContext2D, strokes, now: () => cur }
}

const P = new Proxy({ scale: 1, fontSans: "sans", fontMono: "mono" } as Partial<Palette>, {
  get: (t, k: string) => (k in t ? t[k as keyof Palette] : "#123456"),
}) as Palette

const bars: KLineData[] = Array.from({ length: 30 }, (_, i) => ({
  timestamp: Date.UTC(2026, 0, 1 + i), open: 10, high: 11, low: 9, close: 10, volume: 1,
}))
const day = (i: number) => new Date(bars[i].timestamp).toISOString().slice(0, 10)

const box: Box = {
  id: "b", scale: "mid", status: "established", start: day(2), known_at: day(10), end: day(20), ended_at: day(25), end_reason: "上破离箱",
  width: 0.1, length: 19, shape: null, context: null, position: null, excluded_before: null, superseded_at: null,
  segments: [{ from: day(2), to: day(20), top: 10.5, bottom: 9.5 }], touches: [{ date: day(5), side: "top", price: 10.5, known_at: day(10) }], events: [],
}

describe("the structure painter", () => {
  it("draws an established box solid whatever dash the chart left on the context", () => {
    // klinecharts sets a dashed line's dash (the price line) and leaves it: the box after it must still be solid
    const f = fakeCtx([4, 4])
    const view: StructView = { ...EMPTY_STRUCT, palette: P, structures: { ...EMPTY_STRUCT.structures, boxes: [box] } }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({
      ctx: f.ctx, chart, indicator: { extendData: { view: () => view } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 300, height: 200 },
    } as never)
    expect(f.strokes.length).toBeGreaterThan(0)
    expect(f.strokes.every((d) => d.length === 0)).toBe(true)
    expect(f.now()).toEqual([4, 4])   // and the chart's own state is given back as it was
  })
})

describe("box labels", () => {
  it("label the boxes standing on the day shown with their candle length — a 中箱 too — and leave an ended 中箱 bare", () => {
    const f = fakeCtx([])
    const texts: string[] = []
    ;(f.ctx as unknown as { fillText: (t: string) => void }).fillText = (t) => { texts.push(t) }
    const standing: Box = { ...box, id: "x:big:2.22", start: day(22), end: null, ended_at: null, end_reason: null, length: null,
      segments: [{ from: day(22), to: null, top: 10.5, bottom: 9.5 }] }
    const view: StructView = { ...EMPTY_STRUCT, palette: P, structures: { ...EMPTY_STRUCT.structures, boxes: [box, standing] },
      barLen: () => 0.016 }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({
      ctx: f.ctx, chart, indicator: { extendData: { view: () => view } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 300, height: 200 },
    } as never)
    expect(texts).toEqual(["中箱 · 箱宽 10.0% · K 线均长 1.6%"])
  })
  it("keep a label inside the pane: the box standing at the right edge has its label pulled left, not cut off", () => {
    const f = fakeCtx([])
    const at: number[] = []
    ;(f.ctx as unknown as { fillText: (t: string, x: number) => void }).fillText = (_t, x) => { at.push(x) }
    const standing: Box = { ...box, id: "x:big:2.22", start: day(22), end: null, ended_at: null, end_reason: null, length: null,
      segments: [{ from: day(22), to: null, top: 10.5, bottom: 9.5 }] }
    const view: StructView = { ...EMPTY_STRUCT, palette: P, structures: { ...EMPTY_STRUCT.structures, boxes: [standing] } }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({
      ctx: f.ctx, chart, indicator: { extendData: { view: () => view } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 200, height: 200 },   // the box starts at x 217: past the pane
    } as never)
    expect(at).toHaveLength(1)
    expect(at[0] + 10).toBeLessThanOrEqual(200)   // measureText says 10 wide
  })
})

describe("slanted lines", () => {
  // a slant edge or a swing that starts before the loaded bars: its earlier point is not on bar 0 — drawn there, the line's
  // slope was wrong until the older bars were loaded
  it("start at their first point inside the loaded bars, not at the left edge", () => {
    const moves: [number, number][] = []
    const noop = () => {}
    const ctx = {
      save: noop, restore: noop, setLineDash: noop, beginPath: noop, stroke: noop, strokeRect: noop, fillRect: noop, arc: noop,
      fill: noop, fillText: noop, closePath: noop, measureText: () => ({ width: 10 }),
      moveTo: (x: number, y: number) => { moves.push([x, y]) }, lineTo: noop,
    }
    const before = "2025-12-20"
    const view: StructView = {
      ...EMPTY_STRUCT, palette: P, layers: { ...EMPTY_STRUCT.layers, slant: true, swings: true },
      structures: {
        ...EMPTY_STRUCT.structures,
        slant: [{ id: "s", status: "established", start: before, known_at: day(12), end: null, ended_at: null, end_reason: null, width: 0.05,
          top: [{ date: before, price: 9 }, { date: day(10), price: 10 }, { date: day(20), price: 11 }],
          bottom: [{ date: day(10), price: 9.5 }, { date: day(20), price: 10.5 }] }],
        swings: [{ start: before, end: day(5), direction: "up", change: 0.2, start_price: 8, end_price: 9.6, known_at: day(8) }],
      },
    }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({ ctx, chart, indicator: { extendData: { view: () => view } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 300, height: 200 } } as never)
    expect(moves).not.toContainEqual([0, 90])    // the slant top's earlier point, put on bar 0
    expect(moves).not.toContainEqual([0, 80])    // the swing's start, put on bar 0
    expect(moves).toContainEqual([100, 100])     // the top from its first loaded point
  })
})

describe("the 假底 painter", () => {
  it("draws a 假底 known before the loaded bars that still runs inside them", () => {
    const lines: number[] = []
    let y = 0
    const noop = () => {}
    const ctx = {
      save: noop, restore: noop, setLineDash: noop, beginPath: noop, strokeRect: noop, fillRect: noop, arc: noop,
      fill: noop, fillText: noop, closePath: noop, measureText: () => ({ width: 10 }),
      moveTo: (_x: number, yy: number) => { y = yy }, lineTo: noop, stroke: () => { lines.push(y) },
    }
    const view: StructView = {
      ...EMPTY_STRUCT, palette: P,
      structures: { ...EMPTY_STRUCT.structures, false_bottoms: [{ box_id: "b", source: "C 点", date: "2025-12-01", price: 9.2,
        known_at: "2025-12-15", end: day(20), end_reason: "到期", box_top: 10, box_bottom: 8 }] },
    }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({ ctx, chart, indicator: { extendData: { view: () => view } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 300, height: 200 } } as never)
    expect(lines).toEqual([100, 80, 92])   // the old top and bottom, faint, and the 假底
  })
})

describe("价格行为's plan lines", () => {
  function tags(plan: PlanLine[]) {   // each tag's text and the middle of its box
    const seen: { text: string; y: number }[] = []
    const noop = () => {}
    const ctx = {
      save: noop, restore: noop, setLineDash: noop, beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop, fillRect: noop,
      fillText: (text: string, _x: number, y: number) => { seen.push({ text, y }) }, measureText: () => ({ width: 10 }),
    }
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    RULE_TEMPLATE.draw!({ ctx, chart, indicator: { extendData: { view: () => ({ ...EMPTY_RULE, palette: P, plan }) } },
      xAxis: { convertToPixel: (v: number) => v * 10 }, yAxis: { convertToPixel: (v: number) => 1000 - v * 100 },   // up the page
      bounding: { left: 0, top: 0, width: 400, height: 200 } } as never)
    return seen
  }
  const line = (kind: PlanLine["kind"], price: number): PlanLine => ({ kind, price, from: day(10), to: null, filled: false })

  it("keep their tags apart when the prices are close, in the prices' order (10-21)", () => {
    const [s, r] = tags([line("support", 9.18), line("resistance", 9.21)])   // 3 px apart
    expect([s.text, r.text]).toEqual(["支撑", "阻力"])
    expect(s.y - r.y).toBeGreaterThanOrEqual(17)                                // a tag's height at 字号 1: 阻力 above, clear of 支撑
  })
  it("say a price off the pane at its edge, arrowed", () => {
    expect(tags([line("entry", 9), line("tp2", 12)]).map((t) => t.text)).toEqual(["买入", "止盈2 ↑"])   // 12 is above the pane
  })
})

describe("the 筹码分布 painter", () => {
  it("draws 获利 below the close red, 套牢 above it blue, the 平均成本 dashed, and says the numbers", async () => {
    const { chips } = await import("./cyq")
    const { EM_688682 } = await import("./cyq.fixture")
    const { CHIPS_TEMPLATE, EMPTY_CHIPS } = await import("./paint")
    const rows = EM_688682.split(";").map((r) => r.split(",").map(Number))
    const c = chips(rows.map(([, o, cl, h, l, t]) => ({ open: o, close: cl, high: h, low: l, turnover_rate: t })), rows.length - 1)!
    const fills: { style: string; y: number; w: number }[] = []
    const texts: string[] = []
    const dashes: number[][] = []
    let dash: number[] = [], style = ""
    const ctx = {
      save: () => {}, restore: () => {}, setLineDash: (d: number[]) => { dash = d },
      set fillStyle(v: string) { style = v }, get fillStyle() { return style }, strokeStyle: "", lineWidth: 1, globalAlpha: 1, font: "", textBaseline: "",
      fillRect: (_x: number, y: number, w: number) => { fills.push({ style, y, w }) }, fillText: (t: string) => { texts.push(t) },
      beginPath: () => {}, moveTo: () => {}, lineTo: () => {}, stroke: () => { dashes.push(dash) }, measureText: (t: string) => ({ width: t.length * 6 }),
    }
    const own = { scale: 1, fontSans: "sans", fontMono: "mono", up: "#e00000", boxLong: "#0000e0", warning: "#f0a000" } as Partial<Palette>
    const pal = new Proxy(own, { get: (t, k: string) => (k in t ? t[k as keyof Palette] : "#123456") }) as Palette
    const y = (price: number) => 1000 - price * 10   // a price axis: up the page
    CHIPS_TEMPLATE.draw!({ ctx, indicator: { extendData: { view: () => ({ ...EMPTY_CHIPS, chips: c, date: "2026-09-30", palette: pal }) } },
      yAxis: { convertToPixel: y }, bounding: { left: 0, top: 0, width: 800, height: 600 } } as never)
    const bars = fills.filter((f) => f.style === pal.up || f.style === pal.boxLong)
    expect(bars.length).toBeGreaterThan(50)
    for (const f of bars) {   // the y of a step's middle: below the close's y means a higher price
      expect(f.style).toBe(f.y + 0.01 < y(c.close) ? pal.boxLong : pal.up)
    }
    expect(Math.max(...bars.map((f) => f.w))).toBeCloseTo(200, 0)   // the longest step spans the panel (a quarter of 800)
    expect(dashes).toContainEqual([6, 2])
    expect(texts).toContain("获利比例 39.53% · 平均成本 34.72")
  })
})

describe("当前形态 first, the rest faint", () => {
  // the colours apart, to tell what is drawn in full from what is drawn faint
  const C = new Proxy({ scale: 1, fontSans: "sans", fontMono: "mono", muted: "#999999", boxMid: "#7c3aed", boxLong: "#2563eb", falseBottom: "#a21caf" } as Partial<Palette>, {
    get: (t, k: string) => (k in t ? t[k as keyof Palette] : "#123456"),
  }) as Palette
  const standing: Box = { ...box, id: "x:big:2.22", start: day(22), end: null, ended_at: null, end_reason: null, length: null,
    segments: [{ from: day(22), to: null, top: 10.5, bottom: 9.5 }], touches: [{ date: day(24), side: "top", price: 10.5, known_at: day(24) }] }
  function paint(view: StructView) {
    const seen = { rects: [] as string[], texts: [] as string[], arcs: [] as string[], textAt: [] as number[], tagColor: [] as string[] }
    let [stroke, fill, behind] = ["", "", ""]   // behind: the last rect filled, a tag's own colour under its text
    const ctx = {
      save: () => {}, restore: () => {}, setLineDash: () => {}, getLineDash: () => [],
      set strokeStyle(c: string) { stroke = c }, get strokeStyle() { return stroke },
      set fillStyle(c: string) { fill = c }, get fillStyle() { return fill },
      strokeRect: () => { seen.rects.push(stroke) }, stroke: () => {}, arc: () => { seen.arcs.push(stroke) },
      fillRect: () => { behind = fill }, beginPath: () => {}, moveTo: () => {}, lineTo: () => {}, closePath: () => {}, fill: () => {},
      fillText: (t: string, x: number) => { seen.texts.push(t); seen.textAt.push(x); seen.tagColor.push(behind) }, measureText: () => ({ width: 10 }),
    } as unknown as CanvasRenderingContext2D
    const chart = { getDataList: () => bars, getBarSpace: () => ({ bar: 6 }), getVisibleRange: () => ({ from: 0, to: bars.length }) }
    const axis = { convertToPixel: (v: number) => v * 10 }
    STRUCT_TEMPLATE.draw!({ ctx, chart, indicator: { extendData: { view: () => ({ ...view, palette: C }) } }, xAxis: axis, yAxis: axis,
      bounding: { left: 0, top: 0, width: 400, height: 200 } } as never)
    return seen
  }
  const view: StructView = { ...EMPTY_STRUCT, structures: { ...EMPTY_STRUCT.structures, boxes: [box, standing],
    false_bottoms: [{ box_id: "b", source: "C 点", date: day(15), price: 9.8, known_at: day(20), end: null, end_reason: null, box_top: 10.5, box_bottom: 9.5 }] },
    strong: ["x:big:2.22"] }
  it("draws 当前形态 in its colour with its label — its touches left to the panel's count — and an older box faint, bare, its 假底 left out", () => {
    const seen = paint({ ...view, now: true })
    expect(seen.rects).toEqual(["#99999980", "#7c3aed"])            // the older box first, faint grey; the standing one in its colour
    expect(seen.arcs).toEqual([])
    expect(seen.texts).toEqual(["中箱 · 箱宽 10.0%"])               // only its label; no 假底 of the older box
  })
  it("draws a structure picked in the list with everything: its touches too", () => {
    expect(paint({ ...view, now: false }).arcs).toEqual(["#7c3aed"])
  })
  it("draws 当前形态's 假底 still holding — its price at the right edge, not on the bars — and not one already lost", () => {
    const fb = (end: string | null) => ({ box_id: "x:big:2.22", source: "C 点" as const, date: day(24), price: 9.8, known_at: day(25), end,
      end_reason: end ? ("失守" as const) : null, box_top: 10.5, box_bottom: 9.5 })
    const v = (end: string | null, now: boolean): StructView => ({ ...view, now,
      structures: { ...view.structures, boxes: [standing], false_bottoms: [fb(end)] }, strong: ["x:big:2.22", `fb:x:big:2.22:${day(24)}`] })
    expect(paint(v(null, true)).texts).toEqual(["中箱 · 箱宽 10.0%"])                      // the line, no tag on the bars
    expect(paint(v(day(28), true)).texts).toEqual(["中箱 · 箱宽 10.0%"])                   // lost: in the panel's list
    expect(paint(v(day(28), false)).texts).toEqual(expect.arrayContaining(["假底 9.80", "假底失守"]))   // picked: all of it
  })
  it("leaves the older box out with 旧结构 off", () => {
    expect(paint({ ...view, layers: { ...view.layers, history: false } }).rects).toEqual(["#7c3aed"])
  })
  it("reads 当前形态's prices at the right edge, inside the pane, each box's in that box's colour", () => {
    const seen = paint({ ...view, levels: [{ price: 10.5, text: "中箱顶 10.50 · +5.0%", tone: "box", scale: "mid" },
      { price: 9.5, text: "大箱底 9.50 · −5.0%", tone: "box", scale: "long" }] })
    const [i, j] = [seen.texts.indexOf("中箱顶 10.50 · +5.0%"), seen.texts.indexOf("大箱底 9.50 · −5.0%")]
    expect(i).toBeGreaterThanOrEqual(0)
    expect(seen.textAt[i] + 10).toBeLessThanOrEqual(400)            // measureText says 10 wide: it ends inside the pane
    expect([seen.tagColor[i], seen.tagColor[j]]).toEqual(["#7c3aed", "#2563eb"])   // the 中箱's purple, the 大箱's blue
  })
})
