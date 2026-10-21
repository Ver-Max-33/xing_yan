// Painting the structures and the rule (spec §8.2): draw-only indicators on the price pane. Each chart
// gives its own view through extendData, so two charts on one page never share state.
import { type Chart, type IndicatorTemplate, type KLineData, registerIndicator } from "klinecharts"
import type { Structures } from "@/lib/rules"
import type { Chips } from "./cyq"
import { lowerBound, tsOf } from "./data"
import {
  boxCandles, boxesOnDay, boxText, type CompareView, edgeTo, EMPTY_COMPARE, EMPTY_RULE, EMPTY_STRUCT, fbId, goldName, type LevelTag, type Live,
  pressureText, type RuleView, SCALE_STYLE, stackTags, type StructView, visibleBoxes, visibleSlants,
} from "./structures"
import { fs, type Palette } from "./theme"

interface Ctx2 { ctx: CanvasRenderingContext2D; chart: Chart; xAxis: { convertToPixel: (v: number) => number }; yAxis: { convertToPixel: (v: number) => number } }

function geometry({ chart, xAxis, yAxis }: Ctx2) {
  const list = chart.getDataList()
  const half = Math.max(1, chart.getBarSpace().bar / 2)
  const first = list.length ? list[0].timestamp : 0
  /** bar index of a date: the first bar on or after it; null when it is before the loaded bars, or none are loaded
   *  (a reload: it was −1, and a painter reading list[−1] threw) */
  const index = (d: string, clampEarly = false): number | null => {
    if (!list.length) return null
    const ts = tsOf(d)
    if (ts < first) return clampEarly ? 0 : null
    return Math.min(lowerBound(list as KLineData[], ts), list.length - 1)
  }
  const x = (i: number) => xAxis.convertToPixel(i)
  const y = (v: number) => yAxis.convertToPixel(v)
  return { list, half, index, x, y }
}

/** The price pane's canvas is klinecharts', shared with its own drawing, which leaves a dashed line's dash set (the price
 *  line): every painter starts solid and gives the context back as it found it — or the boxes came out dashed once the
 *  MA lines, which set it solid again, were switched off (and stayed so: switched on, they draw after the boxes). */
export function fresh<T>(ctx: CanvasRenderingContext2D, paint: () => T): T {
  ctx.save()
  ctx.setLineDash([])
  try {
    return paint()
  } finally {
    ctx.restore()
  }
}

export function alpha(color: string, a: number): string {
  if (color.startsWith("#") && color.length === 7) return `${color}${Math.round(a * 255).toString(16).padStart(2, "0")}`
  return color
}

/** A tag's font and box height at the 字号 setting. */
const tagFont = (p: Palette) => `${fs(p, 12)}px ${p.fontSans}`
const tagHeight = (p: Palette) => fs(p, 12) + 5

function tag(ctx: CanvasRenderingContext2D, p: Palette, text: string, x: number, y: number, color: string, below = false) {
  ctx.save()
  ctx.font = tagFont(p)
  const w = ctx.measureText(text).width + 8, h = tagHeight(p)
  const top = below ? y + 2 : y - h - 2
  ctx.fillStyle = color
  ctx.fillRect(x, top, w, h)
  ctx.fillStyle = p.bg
  ctx.textBaseline = "middle"
  ctx.fillText(text, x + 4, top + h / 2)
  ctx.restore()
}

/** What is drawn in full: the view's strong set (当前形态, a focus, the judgement), or everything when it has none. */
const strongOf = (v: StructView) => (v.strong ? new Set(v.strong) : null)
/** A structure outside it: thin, grey, bare — where it was, nothing more (旧结构); none with that layer off. */
const faint = (p: Palette) => alpha(p.muted, 0.5)

function drawBoxes(c: Ctx2, v: StructView, p: Palette, bounding: { left: number; width: number }) {
  const { ctx } = c
  const g = geometry(c)
  const [from, to] = [c.chart.getVisibleRange().from, c.chart.getVisibleRange().to]
  const last = g.list.length - 1
  const strong = strongOf(v)
  const standing = new Set(boxesOnDay(v.structures.boxes).map((b) => b.id))   // the boxes of the day shown: always labelled
  for (const b of visibleBoxes(v.structures.boxes, v.layers, v.highlight, v.structures.gold)) {
    const hot = v.highlight.includes(b.id)
    const full = !strong || strong.has(b.id) || hot
    if (!full && !v.layers.history) continue
    const st = SCALE_STYLE[b.scale]
    const color = !full ? faint(p) : b.status === "proto" ? p.muted : p[st.color]
    let labelAt: [number, number, number] | null = null   // x, bottom and top of the first piece on screen
    for (const s of b.segments) {
      const i0 = g.index(s.from, true)
      const i1 = s.to === null ? last : g.index(s.to)
      if (i0 === null || i1 === null || i1 < from - 1 || i0 > to + 1) continue
      const [x0, x1, yt, yb] = [g.x(i0) - g.half, g.x(i1) + g.half, g.y(s.top), g.y(s.bottom)]
      ctx.save()
      ctx.strokeStyle = color
      ctx.lineWidth = full ? st.width + (hot ? 0.5 : 0) : 1
      if (b.status === "proto") ctx.setLineDash([4, 3])
      if (full && b.status === "established" && (st.fill > 0 || hot)) {
        ctx.fillStyle = alpha(color, hot ? 0.14 : st.fill)
        ctx.fillRect(x0, yt, x1 - x0, yb - yt)
      }
      ctx.strokeRect(x0, yt, x1 - x0, yb - yt)
      ctx.restore()
      labelAt = labelAt ?? [x0, yb, yt]
    }
    if (!full) continue   // its touches, marks and tags are drawn with it when it is in full (picked in the list)
    // 当前形态 reads by its edges and its breaks; how it was drawn (each touch, each left-out extreme) shows when it is picked
    const how = !v.now || hot
    for (const t of how ? b.touches : []) {
      const i = g.index(t.date)
      if (i === null || i < from - 1 || i > to + 1) continue
      ctx.save()
      ctx.strokeStyle = color
      ctx.lineWidth = 1.2
      ctx.beginPath(); ctx.arc(g.x(i), g.y(t.price), 4, 0, Math.PI * 2); ctx.stroke()
      ctx.restore()
    }
    for (const e of b.events) {
      const i = g.index(e.date)
      if (i === null || i < from - 1 || i > to + 1) continue
      const bar = g.list[i]
      ctx.save()
      if ((e.event === "spike_up" || e.event === "spike_down") && e.value !== null && how) { // a left-out extreme: ×
        const [xx, yy] = [g.x(i), g.y(e.value)]
        ctx.strokeStyle = p.muted
        ctx.beginPath(); ctx.moveTo(xx - 3, yy - 3); ctx.lineTo(xx + 3, yy + 3); ctx.moveTo(xx + 3, yy - 3); ctx.lineTo(xx - 3, yy + 3); ctx.stroke()
      } else if (e.event === "break_up" || e.event === "break_down") {
        const up = e.event === "break_up"
        const xx = g.x(i)
        const yy = up ? g.y(bar.high) - 8 : g.y(bar.low) + 8
        ctx.fillStyle = up ? p.up : p.down
        ctx.beginPath(); ctx.moveTo(xx, yy + (up ? -6 : 6)); ctx.lineTo(xx - 4, yy); ctx.lineTo(xx + 4, yy); ctx.closePath(); ctx.fill()
      }
      ctx.restore()
    }
    if (b.excluded_before && how) {
      const i = g.index(b.excluded_before)
      if (i !== null && i >= from - 1 && i <= to + 1) {
        const yy = g.y(g.list[i].high) - 6
        ctx.save(); ctx.strokeStyle = p.muted; ctx.setLineDash([2, 2])
        ctx.beginPath(); ctx.arc(g.x(i), yy, 4, 0, Math.PI * 2); ctx.stroke(); ctx.restore()
      }
    }
    // solid: its text is the page colour, 5:1 on blue (a 70% blue was 3:1)
    if (labelAt && (strong ? b.scale !== "short" : hot || b.scale === "long" || standing.has(b.id))) {
      const text = boxText(b, boxCandles(b, null, v.barLen))   // inside the pane: the box standing now ends at its right edge
      const x = Math.max(bounding.left + 4, Math.min(labelAt[0], bounding.left + bounding.width - textWidth(ctx, p, text) - 4))
      // under the box — and clear of its bottom's price at the right edge, centred on that same line
      tag(ctx, p, text, x, labelAt[1] + (v.levels?.length ? tagHeight(p) / 2 : 0), color, true)
    }
    const gold = v.layers.gold ? goldName(v.structures.gold, b.id, strong ?? undefined, v.structures.boxes) : null
    if (gold && labelAt) tag(ctx, p, gold, labelAt[0], labelAt[2], p.warning)   // over the same piece (an ex-date cuts a box in pieces)
  }
}

function drawSlant(c: Ctx2, v: StructView, p: Palette) {
  const { ctx } = c
  const g = geometry(c)
  const slant = v.structures.slant as (Structures["slant"][number] & { until?: string | null })[]
  const strong = strongOf(v)
  for (const s of visibleSlants(slant, v.layers, v.structures.gold, v.highlight)) {
    const full = !strong || strong.has(s.id) || v.highlight.includes(s.id)
    if (!full && !v.layers.history) continue
    let first: [number, number] | null = null
    for (const edge of [s.top, s.bottom]) {
      // a point before the loaded bars is left out, not put on the first bar: that would bend the line
      const pts = edge.map((q) => [g.index(q.date), q.price] as const).filter((q): q is [number, number] => q[0] !== null)
      if (pts.length < 2) continue
      first ??= pts[0]
      ctx.save()
      ctx.strokeStyle = !full ? faint(p) : s.status === "proto" ? p.muted : p.boxShort
      ctx.lineWidth = full ? 1.5 : 1
      if (s.status === "proto") ctx.setLineDash([4, 3])
      ctx.beginPath()
      // still running on the replay day: the edge goes to that day, no further and no shorter
      edgeTo(pts, s.until ? g.index(s.until) : null).forEach(([i, price], k) => {
        if (k === 0) ctx.moveTo(g.x(i), g.y(price))
        else ctx.lineTo(g.x(i), g.y(price))
      })
      ctx.stroke()
      ctx.restore()
    }
    // a 变异金品 is tagged as a flat one is, at its top's start
    const gold = full && v.layers.gold ? goldName(v.structures.gold, s.id, strong ?? undefined, v.structures.boxes) : null
    if (gold && first) tag(ctx, p, gold, g.x(first[0]), g.y(first[1]), p.warning)
  }
}

function drawMarks(c: Ctx2, v: StructView, p: Palette) {
  const { ctx } = c
  const g = geometry(c)
  if (v.layers.pivots) {
    for (const q of v.structures.pivots) {
      const i = g.index(q.date)
      if (i === null) continue
      ctx.save(); ctx.fillStyle = q.kind === "high" ? p.down : p.up
      ctx.beginPath(); ctx.arc(g.x(i), g.y(q.price) + (q.kind === "high" ? -5 : 5), 2.5, 0, Math.PI * 2); ctx.fill(); ctx.restore()
    }
  }
  if (v.layers.swings) {
    for (const s of v.structures.swings) {
      const [i0, i1] = [g.index(s.start), g.index(s.end)]   // one that starts before the loaded bars waits for them
      if (i0 === null || i1 === null) continue
      ctx.save(); ctx.strokeStyle = alpha(p.muted, 0.8); ctx.setLineDash([3, 3])
      ctx.beginPath(); ctx.moveTo(g.x(i0), g.y(s.start_price)); ctx.lineTo(g.x(i1), g.y(s.end_price)); ctx.stroke(); ctx.restore()
    }
  }
}

/** 假底: a dashed line from C (or the 金品's start) on past the break until it is lost, a new box forms or
 *  its horizon; after the break the old box's top and bottom go on beside it, faint and dotted — falling back under the old
 *  top, losing the 假底 (heading for the old bottom), and losing the old bottom are three different things. */
function drawFalseBottoms(c: Ctx2, v: StructView, p: Palette) {
  if (!v.layers.falseBottoms) return
  const { ctx } = c
  const g = geometry(c)
  const last = g.list.length - 1
  const strong = strongOf(v)
  for (const f of v.structures.false_bottoms) {
    if (strong && !strong.has(fbId(f))) continue   // an older box's 假底 is drawn with it (picked in the list)
    if (v.now && f.end !== null) continue           // 当前形态's lost one: in the panel, drawn when picked
    const [i0, ib] = [g.index(f.date, true), g.index(f.known_at, true)]   // flat lines: what was before the loaded bars starts at the first
    const i1 = f.end ? g.index(f.end) : last
    if (i0 === null || ib === null || i1 === null) continue
    const [x0, xb, x1, y] = [g.x(i0), g.x(ib), g.x(i1), g.y(f.price)]
    ctx.save()
    ctx.strokeStyle = alpha(p.muted, 0.8); ctx.lineWidth = 1; ctx.setLineDash([1, 3])
    for (const yy of [g.y(f.box_top), g.y(f.box_bottom)]) { ctx.beginPath(); ctx.moveTo(xb, yy); ctx.lineTo(x1, yy); ctx.stroke() }
    ctx.strokeStyle = p.falseBottom; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4])
    ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke()
    ctx.restore()
    if (v.now) continue   // its price is at the right edge, not on the latest bars
    tag(ctx, p, `假底 ${f.price.toFixed(v.digits)}`, x0, y, p.falseBottom)
    if (f.end_reason === "失守") tag(ctx, p, "假底失守", x1, y, p.falseBottom, true)
  }
}

function drawPressure(c: Ctx2, v: StructView, p: Palette, bounding: { left: number; width: number; top: number; height: number }) {
  const pr = v.pressure
  if (!v.layers.pressure || !pr) return
  const { ctx } = c
  const g = geometry(c)
  const right = bounding.left + bounding.width
  const label = pressureText(pr, v.digits)
  const color = label.short ? p.warning : p.muted
  if (pr.bottom != null && pr.top != null) {
    // from the old box's right end on to now (the box itself is drawn, faint, where it was): the zone ahead
    const i0 = pr.end ?? pr.start ? g.index((pr.end ?? pr.start)!, true) : 0
    const [yt, yb] = [g.y(pr.top), g.y(pr.bottom)]
    const x0 = i0 === null ? bounding.left : g.x(i0) + g.half
    ctx.save()
    ctx.fillStyle = alpha(p.warning, 0.06)
    ctx.fillRect(x0, yt, right - x0, yb - yt)
    ctx.restore()
    if (v.levels) return   // its words go with 当前形态's prices at the right edge
    const y = Math.max(bounding.top + 2 * tagHeight(p) + 6, Math.min(yb, bounding.top + bounding.height - 4))
    tag(ctx, p, `${label.text}${yb < bounding.top ? " ↑" : ""}`, Math.max(bounding.left + 4, right - 260), y, color)
  } else if (label.text) {
    ctx.save(); ctx.font = tagFont(p)
    const w = ctx.measureText(label.text).width + 8
    ctx.restore()
    tag(ctx, p, label.text, Math.max(bounding.left + 4, right - w - 4), bounding.top + tagHeight(p) + 6, color)   // the top-right corner
  }
}

const levelColor = (t: LevelTag, p: Palette) => (t.scale ? p[SCALE_STYLE[t.scale].color]   // a box's in its colour
  : { box: p.structure, falseBottom: p.falseBottom, pressure: p.muted, short: p.warning }[t.tone])

/** The 价位梯 line the pointer rests on, drawn as itself: from its structure's start to the right edge, in its colour
 *  (heavier for 突破线 and 失效线, dashed as the panel's sample); its price goes with the others' (drawLevels). */
function drawMark(c: Ctx2, v: StructView, p: Palette, bounding: { left: number; width: number; top: number; height: number }) {
  const m = v.mark
  if (!m) return
  const { ctx } = c
  const g = geometry(c)
  const y = g.y(m.price)
  if (y < bounding.top || y > bounding.top + bounding.height) return   // off the pane: its tag at the edge, arrowed
  const i0 = m.from ? g.index(m.from, true) : null
  const x0 = i0 === null ? bounding.left : Math.max(bounding.left, g.x(i0) - g.half)
  ctx.save()
  ctx.strokeStyle = levelColor(m, p)
  ctx.lineWidth = m.heavy ? 2 : 1.5
  ctx.setLineDash(m.dash ? [5, 3] : [])
  ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(bounding.left + bounding.width, y); ctx.stroke()
  ctx.restore()
}

const NO_LEGEND = new Set(["WB_STRUCT", "WB_RULE", "WB_COMPARE", "WB_CHIPS"])
/** Where the chart's legend at the top left ends: its title, the bar's figures (two lines in a narrow pane), a line for
 *  each indicator over the candles (theme: 13 / 12 px text, klinecharts' 4 px above and below each). */
function legendBottom(c: Ctx2, p: Palette, bounding: { top: number; width: number }): number {
  const rows = c.chart.getIndicators({ paneId: "candle_pane" }).filter((i) => i.visible && !NO_LEGEND.has(i.name)).length
  const figures = bounding.width < 760 * fs(p, 12) / 12 ? 2 : 1
  return bounding.top + 6 + (fs(p, 13) + 8) + (figures + rows) * (fs(p, 12) + 8) + 4
}

/** 日线价位 on a minute chart (design 2026-10-12-structures-every-period): the daily 当前形态's lines across the whole pane,
 *  dashed, each named at the left edge (日线突破线 12.51 +1.7%) — the right edge is the minute chart's own; a line off the
 *  pane: its name at that edge, arrowed. */
function drawDayLines(c: Ctx2, v: StructView, p: Palette, bounding: { left: number; width: number; top: number; height: number }) {
  if (!v.dayLines?.length || !v.layers.dayLines) return
  const { ctx } = c
  const g = geometry(c)
  const [bottom, right] = [bounding.top + bounding.height, bounding.left + bounding.width]
  const top = Math.min(legendBottom(c, p, bounding), bottom - tagHeight(p))   // the names stay clear of the chart's legend
  const h = tagHeight(p)
  const ys = v.dayLines.map((m) => g.y(m.price))
  const at = stackTags(ys.map((y) => Math.min(Math.max(y - h - 2, top), bottom - h)), h + 2, top, bottom, [])
  v.dayLines.forEach((m, k) => {
    const y = ys[k], color = levelColor(m, p), inside = y >= top && y <= bottom
    if (inside) {
      ctx.save(); ctx.strokeStyle = color; ctx.globalAlpha = 0.85; ctx.lineWidth = m.heavy ? 1.5 : 1; ctx.setLineDash([6, 4])
      ctx.beginPath(); ctx.moveTo(bounding.left, y); ctx.lineTo(right, y); ctx.stroke(); ctx.restore()
    }
    tag(ctx, p, `${m.text}${y < top ? " ↑" : y > bottom ? " ↓" : ""}`, bounding.left + 6, at[k] + h / 2 + 2, color)
  })
}

/** 当前形态's prices down the right edge (box edges, 假底, left pressure) with how far each is from the close: each at its price
 *  (a tick there), moved to the nearest free place when another is in the way, clear of 昨收 / 涨停's labels; a price off
 *  the pane at its edge, arrowed. */
function drawLevels(c: Ctx2, v: StructView, p: Palette, bounding: { left: number; width: number; top: number; height: number }) {
  if (!v.levels?.length) return
  const { ctx } = c
  const g = geometry(c)
  const right = bounding.left + bounding.width
  const [top, bottom] = [bounding.top, bounding.top + bounding.height]
  const h = tagHeight(p)
  const last = g.list.at(-1) as (KLineData & { pre_close?: number; up_limit?: number }) | undefined
  const taken = [last?.pre_close, last?.up_limit].filter((x): x is number => typeof x === "number").map((x) => g.y(x) - h / 2)
  const close = (last?.close ?? 0)
  const order = v.levels.map((_, i) => i).sort((a, b) => Math.abs(v.levels![a].price - close) - Math.abs(v.levels![b].price - close))
  const at = stackTags(order.map((i) => g.y(v.levels![i].price)), h + 2, top, bottom, taken)
  order.forEach((i, k) => {
    const t = v.levels![i], y = g.y(t.price), color = levelColor(t, p)
    const text = `${t.text}${y < top ? " ↑" : y > bottom ? " ↓" : ""}`
    if (y >= top && y <= bottom) {   // a tick at the price itself
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.moveTo(right - 3, y); ctx.lineTo(right, y); ctx.stroke(); ctx.restore()
    }
    tag(ctx, p, text, right - 4 - textWidth(ctx, p, text), at[k] + h / 2 + 2, color)
  })
}

function drawLabels(c: Ctx2, v: StructView, p: Palette) {
  if (!v.layers.labels) return
  const { ctx } = c
  const g = geometry(c)
  for (const l of v.labels) {
    const [i0, i1] = [g.index(l.start, true), g.index(l.end)]
    if (i0 === null || i1 === null) continue
    const [x0, x1, yt, yb] = [g.x(i0) - g.half, g.x(i1) + g.half, g.y(l.top), g.y(l.bottom)]
    ctx.save(); ctx.strokeStyle = p.annotate; ctx.setLineDash([5, 3]); ctx.lineWidth = 1.2
    ctx.strokeRect(x0, yt, x1 - x0, yb - yt); ctx.restore()
    const note = v.labelNotes?.[l.id]
    tag(ctx, p, `${l.is_box ? "你的样例" : "你标的“不是箱体”"}${note ? ` · ${note}` : ""}`, x0, yt, p.annotate)
    for (const t of l.touches) {
      const i = g.index(t.date)
      if (i === null) continue
      ctx.save(); ctx.strokeStyle = p.annotate
      ctx.beginPath(); ctx.arc(g.x(i), g.y(t.price), 4, 0, Math.PI * 2); ctx.stroke(); ctx.restore()
    }
  }
}

export const STRUCT_TEMPLATE: IndicatorTemplate<unknown, number, Live<StructView>> = {
  name: "WB_STRUCT", shortName: "结构", series: "price", precision: 2, figures: [],
  shouldUpdate: () => ({ calc: false, draw: true }), // a new view only repaints
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: "", calcParamsText: "", features: [], legends: [] }),
  draw: ({ ctx, chart, indicator, xAxis, yAxis, bounding }) => {
    const v = indicator.extendData?.view() ?? EMPTY_STRUCT
    if (!v.palette) return false
    const [c, p] = [{ ctx, chart, xAxis, yAxis }, v.palette]
    return fresh(ctx, () => {
      drawPressure(c, v, p, bounding)
      drawBoxes(c, v, p, bounding)
      drawSlant(c, v, p)
      drawMarks(c, v, p)
      drawFalseBottoms(c, v, p)
      drawLabels(c, v, p)
      drawDayLines(c, v, p, bounding)
      drawMark(c, v, p, bounding)
      drawLevels(c, v, p, bounding)
      return false
    })
  },
}

export const RULE_TEMPLATE: IndicatorTemplate<unknown, number, Live<RuleView>> = {
  name: "WB_RULE", shortName: "规则", series: "price", precision: 2, figures: [],
  shouldUpdate: () => ({ calc: false, draw: true }), // a new view only repaints
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: "", calcParamsText: "", features: [], legends: [] }),
  draw: ({ ctx, chart, indicator, xAxis, yAxis, bounding }) => {
    const v = indicator.extendData?.view() ?? EMPTY_RULE
    const p = v.palette
    if (!p) return false
    return fresh(ctx, () => paintRule(geometry({ ctx, chart, xAxis, yAxis }), ctx, v, p, bounding))
  },
}

function paintRule(g: ReturnType<typeof geometry>, ctx: CanvasRenderingContext2D, v: RuleView, p: Palette,
                   bounding: { top: number; height: number }): false {
    for (const b of v.bands) { // the stages of the binding behind the explanation
      if (!b.start || !b.end) continue
      const [i0, i1] = [g.index(b.start, true), g.index(b.end)]
      if (i0 === null || i1 === null) continue
      const [x0, x1] = [g.x(i0) - g.half, g.x(i1) + g.half]
      ctx.save(); ctx.fillStyle = alpha(p.annotate, 0.08); ctx.fillRect(x0, bounding.top, x1 - x0, bounding.height); ctx.restore()
      if (b.stage) tag(ctx, p, b.label ?? b.stage, x0, bounding.top + 20, alpha(p.annotate, 0.85)) // by name; a nameless band only marks the bars
    }
    for (const s of v.signals) {
      const i = g.index(s.signal_date)
      if (i === null) continue
      const bar = g.list[i]
      const [xx, yy] = [g.x(i), g.y(bar.low) + 10]
      ctx.save(); ctx.fillStyle = p.up
      ctx.beginPath(); ctx.moveTo(xx, yy); ctx.lineTo(xx - 5, yy + 8); ctx.lineTo(xx + 5, yy + 8); ctx.closePath(); ctx.fill(); ctx.restore()
    }
    for (const d of v.danger) { // 危险提醒: an amber triangle pointing down over the day's high (design 2026-10-15 §3)
      const i = g.index(d.date)
      if (i === null) continue
      const [xx, yy] = [g.x(i), g.y(g.list[i].high) - 10]
      ctx.save(); ctx.fillStyle = p.warning
      ctx.beginPath(); ctx.moveTo(xx, yy); ctx.lineTo(xx - 5, yy - 8); ctx.lineTo(xx + 5, yy - 8); ctx.closePath(); ctx.fill(); ctx.restore()
    }
    const last = g.list.length - 1
    const [top, bottom, h] = [bounding.top, bounding.top + bounding.height, tagHeight(p)]
    // 价格行为分析's plan (design 2026-10-20 §6.2): from its first open to its sale, dashed until bought
    const lines = last < 0 ? [] : v.plan.flatMap((l) => {
      const [i0, i1] = [g.index(l.from, true), l.to ? g.index(l.to) : last]
      return i0 === null || i1 === null ? []   // an open plan runs past the last bar
        : [{ l, x0: g.x(i0) - g.half, x1: g.x(i1) + g.half * (l.to ? 1 : 5), y: g.y(l.price) }]
    })
    const at = stackTags(lines.map((d) => d.y), h + 2, top, bottom)   // the lines end together: their tags, close prices kept apart
    lines.forEach(({ l, x0, x1, y }, k) => {
      const color = l.kind === "stop" ? p.down : l.kind === "tp1" || l.kind === "tp2" ? p.up : p.annotate   // entry, support, resistance
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.2
      if (!l.filled) ctx.setLineDash([5, 4])
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.restore()
      tag(ctx, p, `${PLAN_TAG[l.kind]}${y < top ? " ↑" : y > bottom ? " ↓" : ""}`, x1 + 4, at[k] + h / 2 + 2, color)
    })
    return false
}
const PLAN_TAG = { entry: "买入", stop: "止损", tp1: "止盈1", tp2: "止盈2", support: "支撑", resistance: "阻力" } as const

const COMPARE: IndicatorTemplate<unknown, number, Live<CompareView>> = {
  name: "WB_COMPARE", shortName: "对比", series: "price", precision: 2, figures: [],
  shouldUpdate: () => ({ calc: false, draw: true }),
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: "", calcParamsText: "", features: [], legends: [] }),
  draw: ({ ctx, chart, indicator, xAxis, yAxis, bounding }) => {
    const v = indicator.extendData?.view() ?? EMPTY_COMPARE
    const p = v.palette
    const list = chart.getDataList()
    if (!p || !v.name || list.length === 0) return false
    return fresh(ctx, () => paintCompare(ctx, chart, v, p, xAxis, yAxis, bounding))
  },
}

function paintCompare(ctx: CanvasRenderingContext2D, chart: Chart, v: CompareView, p: Palette, xAxis: Ctx2["xAxis"], yAxis: Ctx2["yAxis"],
                      bounding: { left: number }): false {
    const list = chart.getDataList()
    const { from, to } = chart.getVisibleRange()
    let base: number | null = null
    let scale = 1
    ctx.save()
    ctx.strokeStyle = p.ma4
    ctx.lineWidth = 1.5
    ctx.beginPath()
    let started = false
    let last: { x: number; y: number; pct: number } | null = null
    for (let i = Math.max(0, from); i < Math.min(list.length, to); i++) {
      const other = v.closes[list[i].timestamp]
      if (other === undefined) continue
      if (base === null) { base = other; scale = list[i].close / other }
      const [x, y] = [xAxis.convertToPixel(i), yAxis.convertToPixel(other * scale)]
      if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
      last = { x, y, pct: other / base - 1 }
    }
    ctx.stroke()
    ctx.restore()
    if (last) { // at the line's end, clear of the legend rows top-left
      const text = `对比 ${v.name} ${last.pct >= 0 ? "+" : ""}${(last.pct * 100).toFixed(1)}%（从左边第一根起）`
      ctx.save(); ctx.font = tagFont(p); const w = ctx.measureText(text).width + 8; ctx.restore()
      tag(ctx, p, text, Math.max(bounding.left, last.x - w), last.y - 6, p.ma4)
    }
    return false
}

/** 筹码分布 (spec §8.6): the day's chips as bars from the price pane's right edge, 获利 (below the close) red and 套牢 blue
 *  as 东方财富 draws them, the 平均成本 dashed; its numbers at the bottom right. `note` says why there is none. */
export interface ChipView { chips: Chips | null; date: string | null; partial: boolean; note: string | null; digits: number; palette: Palette | null }
export const EMPTY_CHIPS: ChipView = { chips: null, date: null, partial: false, note: null, digits: 2, palette: null }

const pct = (x: number) => `${(x * 100).toFixed(2)}%`

function paintChips(ctx: CanvasRenderingContext2D, v: ChipView, p: Palette, yAxis: Ctx2["yAxis"],
                    bounding: { left: number; top: number; width: number; height: number }): false {
  const right = bounding.left + bounding.width
  const bottom = bounding.top + bounding.height
  const c = v.chips
  if (!c) {
    if (v.note) tag(ctx, p, v.note, right - 8 - textWidth(ctx, p, v.note), bottom - 6, p.muted)
    return false
  }
  const width = Math.min(240, bounding.width * 0.25)
  const max = Math.max(...c.weights)
  const y = (price: number) => yAxis.convertToPixel(price)
  const h = Math.max(1, Math.abs(y(c.low) - y(c.low + c.step)))
  ctx.save()
  ctx.globalAlpha = 0.6   // the candles under it stay readable
  c.weights.forEach((w, j) => {
    const len = max > 0 ? (w / max) * width : 0
    if (len < 0.5) return
    const price = c.low + c.step * j
    ctx.fillStyle = price < c.close ? p.up : p.boxLong
    ctx.fillRect(right - len, y(price) - h / 2, len, h)
  })
  ctx.restore()
  ctx.strokeStyle = p.warning; ctx.lineWidth = 1.5; ctx.setLineDash([6, 2])
  ctx.beginPath(); ctx.moveTo(right - width, y(c.avgCost)); ctx.lineTo(right, y(c.avgCost)); ctx.stroke()
  ctx.setLineDash([])
  const d = v.digits
  const lines = [
    `筹码 ${v.date ?? ""}${v.partial ? `（只载入了 ${c.bars} 根）` : ""}`,
    `获利比例 ${pct(c.profit)} · 平均成本 ${c.avgCost.toFixed(d)}`,
    `90% ${c.p90.low.toFixed(d)}–${c.p90.high.toFixed(d)} 集中度 ${pct(c.p90.concentration)}`,
    `70% ${c.p70.low.toFixed(d)}–${c.p70.high.toFixed(d)} 集中度 ${pct(c.p70.concentration)}`,
    ...(c.missing ? [`其中 ${c.missing} 天没有换手率（按 0 算）`] : []),
  ]
  ctx.save()
  ctx.font = tagFont(p)
  const lh = tagHeight(p)
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 10
  const top = bottom - lines.length * lh - 8
  ctx.fillStyle = alpha(p.bg, 0.85); ctx.fillRect(right - w - 4, top, w, lines.length * lh + 4)
  ctx.textBaseline = "middle"
  lines.forEach((l, i) => {
    ctx.fillStyle = i === 1 ? p.fg : p.muted
    ctx.fillText(l, right - w + 1, top + 2 + lh * (i + 0.5))
  })
  ctx.restore()
  return false
}

function textWidth(ctx: CanvasRenderingContext2D, p: Palette, text: string): number {
  ctx.save(); ctx.font = tagFont(p)
  const w = ctx.measureText(text).width + 8
  ctx.restore()
  return w
}

export const CHIPS_TEMPLATE: IndicatorTemplate<unknown, number, Live<ChipView>> = {
  name: "WB_CHIPS", shortName: "筹码", series: "price", precision: 2, figures: [],
  shouldUpdate: () => ({ calc: false, draw: true }),
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: "", calcParamsText: "", features: [], legends: [] }),
  draw: ({ ctx, indicator, yAxis, bounding }) => {
    const v = indicator.extendData?.view() ?? EMPTY_CHIPS
    const p = v.palette
    if (!p || (!v.chips && !v.note)) return false
    return fresh(ctx, () => paintChips(ctx, v, p, yAxis, bounding))
  },
}

let registered = false
export function registerStructureIndicators() {
  if (registered) return
  registered = true
  registerIndicator(STRUCT_TEMPLATE)
  registerIndicator(RULE_TEMPLATE)
  registerIndicator(COMPARE)
  registerIndicator(CHIPS_TEMPLATE)
}
