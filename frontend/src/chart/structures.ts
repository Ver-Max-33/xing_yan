// Structures and the rule on the chart (spec §8.2): boxes (each as drawn on the day, three widths for the three
// scales, touches circled, left-out extremes, spikes and breaks marked), slant
// boxes, pivots, swings, 金品, 假底, the left pressure band, your sample boxes, and the rule's signals
// and stage bands. Everything is computed by the backend; these indicators only paint the visible
// range. The replay draws asOf(t): only what was known on day t (spec §5.8 rule 5).
import { type Band, type Box, boxesOf, type FalseBottom, type Gold, type Label, type LabelResult, type Pressure, type PricePoint,
  type Signal, type Structures } from "@/lib/rules"
import type { Palette } from "./theme"

export interface Layers {
  boxShort: boolean; boxMid: boolean; boxLong: boolean; protos: boolean; slant: boolean; pivots: boolean
  swings: boolean; gold: boolean; falseBottoms: boolean; pressure: boolean; labels: boolean; signals: boolean
  history: boolean   // 旧结构: the structures besides 当前形态 (or your focus), drawn faint; off, not at all
  dayLines: boolean  // 日线价位: on a minute chart, the daily 当前形态's lines (盯盘 alerts on them)
  danger: boolean    // 危险提醒's days (design 2026-10-15)
}
export const DEFAULT_LAYERS: Layers = {
  boxShort: false, boxMid: true, boxLong: true, protos: false, slant: false, pivots: false, swings: false,
  gold: true, falseBottoms: true, pressure: true, labels: true, signals: true, history: true, dayLines: true, danger: true,
}
export const LAYER_LABELS: [keyof Layers, string][] = [
  ["boxLong", "大箱"], ["boxMid", "中箱"], ["boxShort", "小箱"], ["slant", "斜箱"], ["history", "旧结构"],
  ["gold", "金品"], ["falseBottoms", "假底"], ["pressure", "左侧压力"], ["pivots", "高低点"], ["swings", "波段"],
  ["labels", "你的样例"], ["signals", "规则信号"], ["danger", "危险提醒"], ["dayLines", "日线价位"],
]
/** What each layer is, in words anyone can follow (the chip's tooltip). */
export const LAYER_HINTS: Partial<Record<keyof Layers, string>> = {
  boxLong: "横盘 60 根以上的区间；上下沿画在多数 K 线成交过的价位，只有一两根冲到的价位不算",
  boxMid: "横盘 15–60 根的区间，画法同大箱；大箱里面更窄的一段横盘也画成中箱（先画大箱，再在大箱里分中箱、小箱）",
  boxShort: "几根挤在一起的 K 线（3–20 根），在大箱里比大箱窄",
  history: "当前形态以外的箱体，淡淡地画出来，看得出以前在哪里横过盘；关掉就只画当前形态。右边列表里点一个，它照样完整画出",
  dayLines: "分钟线上画日线当前形态的突破线、上沿、下沿和失效线（虚线，名字在左边）：选股、盯盘提醒、交易计划都按日线的线",
  slant: "两条近似平行的斜线围成的通道",
  gold: "大箱末端（或刚突破后）的那个小箱，按位置分三种：突破后在原箱顶上方＝天品，贴着箱顶＝上品，离箱顶还有一段＝下品；小箱是斜的叫变异",
  falseBottoms: "向上突破时，最后一次回落的低点（有金品时是它的底）；之后收盘跌破它，说明这次突破可能失败",
  pressure: "左边离现价最近、还在上方的旧箱子：涨到那里容易受阻",
  pivots: "已经确认的转折高点和低点",
  swings: "相邻两个高低点之间的一段涨或跌",
  labels: "你在箱体标注页画的样例",
  signals: "规则在这只股票上出信号的日子",
  danger: "十年里之后多半跑输的情况出现的那天（收在涨停、高位放量、弱势急拉、散户接盘），说明在右栏",
}
export const SCALE_NAME: Record<Box["scale"], string> = { short: "小箱", mid: "中箱", long: "大箱" }
/** The three levels apart at a glance (big box blue, small boxes warm): colour, weight, fill. */
export const SCALE_STYLE: Record<Box["scale"], { color: "boxLong" | "boxMid" | "boxShort"; width: number; fill: number }> = {
  long: { color: "boxLong", width: 2, fill: 0.06 }, mid: { color: "boxMid", width: 1.5, fill: 0.04 }, short: { color: "boxShort", width: 1.25, fill: 0 },
}
/** The layer chips' line sample. */
export const LAYER_SWATCH: Partial<Record<keyof Layers, { color: string; dash: boolean }>> = {
  boxLong: { color: "--box-long", dash: false }, boxMid: { color: "--box-mid", dash: false }, boxShort: { color: "--box-short", dash: false },
  slant: { color: "--box-short", dash: false }, falseBottoms: { color: "--false-bottom", dash: true },
}
export const GOLD_NAME: Record<string, string> = { sky: "天品", upper: "上品", lower: "下品" }

// ---- what is known on a day -------------------------------------------------------------------------
const knownBy = (t: string) => (d: string | null | undefined) => !!d && d <= t

/** The structures as they were known on day `t` (null: the latest bar, everything). */
export function asOf(s: Structures, t: string | null, dates: string[] = []): Structures {
  // a box is redrawn as it grows: each version stands from its known_at until the next one (superseded_at)
  if (t === null) {
    return s.gold.some((g) => g.superseded_at) || s.boxes.some((b) => b.superseded_at)
      ? { ...s, boxes: s.boxes.filter((b) => !b.superseded_at), gold: s.gold.filter((g) => !g.superseded_at) } : s
  }
  const known = knownBy(t)
  const boxes = s.boxes
    .filter((b) => known(b.known_at) && !known(b.superseded_at))
    .map((b) => {
      const ended = known(b.ended_at)
      // not known to have ended: it runs to t (its last started piece; pieces cut at an ex-date keep their ends)
      const started = b.segments.filter((g) => g.from <= t)
      const segments = ended ? started : started.map((g, k) => (k === started.length - 1 ? { ...g, to: t } : g))
      return {
        ...b, segments, end: ended ? b.end : null, ended_at: ended ? b.ended_at : null,
        end_reason: ended ? b.end_reason : null, length: ended ? b.length : null,
        touches: b.touches.filter((x) => known(x.known_at)), events: b.events.filter((x) => known(x.known_at)),
      }
    })
  const slant = s.slant.filter((x) => known(x.known_at)).map((x) => {
    const ended = known(x.ended_at)
    // the edge stops on t, at its own line's price there (by bar with the stock's bar days, else by calendar): never the
    // point after t — in a replay the chart has no bar for it, it would sit on t with a later price (review 10-13)
    const cut = (pts: typeof x.top) => {
      const i = pts.findIndex((p) => p.date > t)
      if (ended || i < 0) return pts
      if (i === 0) return pts.slice(0, 1)
      return [...pts.slice(0, i), { date: t, price: lineAt(pts.slice(i - 1, i + 1), t, dates) ?? pts[i - 1].price }]
    }
    return { ...x, end: ended ? x.end : null, ended_at: ended ? x.ended_at : null, top: cut(x.top), bottom: cut(x.bottom), until: ended ? null : t }
  })
  return {
    boxes, slant,
    pivots: s.pivots.filter((p) => known(p.known_at)), swings: s.swings.filter((p) => known(p.known_at)),
    gold: s.gold.filter((g) => known(g.known_at) && !known(g.superseded_at)), false_bottoms: s.false_bottoms.filter((f) => known(f.known_at)).map((f) => (known(f.end) ? f : { ...f, end: null, end_reason: null })),
  }
}

/** A slanted edge's points (bar index, price) drawn up to bar `until` (the replay day; null: as they are):
 *  the last piece is cut there, or — when the points stop before it because the box has not been seen to
 *  end yet — the edge carries on along the same line to that day, as a flat box runs to t. */
export function edgeTo(pts: [number, number][], until: number | null): [number, number][] {
  if (until === null || pts.length < 2) return pts
  const [pi, pp] = pts[pts.length - 2]
  const [i, p] = pts[pts.length - 1]
  if (i === pi || until === i) return pts
  const at = (x: number): [number, number] => [x, pp + ((p - pp) * (x - pi)) / (i - pi)]
  return until < i ? [...pts.slice(0, -1), at(Math.max(until, pi))] : [...pts, at(until)]
}

/** The boxes the layers ask for, and always the ones behind the judgement on screen (`highlight`). */
export function visibleBoxes(boxes: Box[], layers: Layers, highlight: string[] = [], gold: Gold[] = []): Box[] {
  const scale = { short: layers.boxShort, mid: layers.boxMid, long: layers.boxLong }
  // a 金品 is its small box: the 金品 layer draws it whatever the small boxes' switch says (spec §8.2)
  const golden = new Set(layers.gold ? gold.map((g) => g.small_box_id) : [])
  return boxes.filter((b) => (scale[b.scale] && (b.status === "established" || layers.protos)) || highlight.includes(b.id) || golden.has(b.id))
}

/** A small box's 金品 tag: every kind it is (天品 of the box it broke out of, 上品 of the one it tops …), 变异 when slanted;
 *  with `of` (the structures drawn in full) only what it is to the big box drawn with it — not 上品 · 下品 of two; with two
 *  of them drawn (a 中箱 inside a 大箱), each by its box's name (`boxes`): 中箱上品 · 大箱下品. */
export function goldName(gold: Gold[], boxId: string, of?: Set<string>, boxes: Box[] = []): string | null {
  const mine = gold.filter((g) => g.small_box_id === boxId)
  const near = of ? mine.filter((g) => of.has(g.big_box_id)) : []
  const shown = near.length ? near : mine
  const scale = (id: string) => boxes.find((b) => b.id === id)?.scale
  const named = near.length > 1 && near.every((g) => scale(g.big_box_id))
  const names = [...new Set(shown.map((g) => `${named ? SCALE_NAME[scale(g.big_box_id)!] : ""}${g.variant ? "变异" : ""}${GOLD_NAME[g.kind] ?? g.kind}`))]
  return names.length ? names.join(" · ") : null
}

// ---- what comes first (当前形态, a focus, the judgement) ----------------------------------------------
/** A 假底's id (it has none of its own): its box and its day. */
export const fbId = (f: Pick<FalseBottom, "box_id" | "date">) => `fb:${f.box_id}:${f.date}`

/** A structure's days by its id (end null: running): a box, a slant box (a 变异金品's small box is one) or a 假底. */
export function spanOf(s: Structures, id: string): { start: string; end: string | null } | null {
  const x = s.boxes.find((b) => b.id === id) ?? s.slant.find((b) => b.id === id)
  if (x) return { start: x.start, end: x.end }
  const f = s.false_bottoms.find((f) => fbId(f) === id)
  return f ? { start: f.date, end: f.end } : null
}

/** With what belongs to them: a 假底 its box, a box its 金品's small boxes and its 假底, a 金品's small box its big boxes. */
function withRelated(s: Structures, ids: string[]): Set<string> {
  const fbs = new Map(s.false_bottoms.map((f) => [fbId(f), f]))
  const base = ids.flatMap(boxesOf).flatMap((id) => (fbs.has(id) ? [id, fbs.get(id)!.box_id] : [id]))
  const out = new Set(base)
  for (const id of base) {
    for (const g of s.gold) {
      if (g.big_box_id === id) out.add(g.small_box_id)
      if (g.small_box_id === id) out.add(g.big_box_id)
    }
    for (const f of s.false_bottoms) if (f.box_id === id) out.add(fbId(f))
  }
  return out
}

/** The structures drawn in full, the rest faint: the one you focused, else the ones behind the judgement on screen
 *  (`highlight`), else 当前形态 — the big box standing (none standing: the one last drawn, just broken out of) and the 中箱
 *  inside it, with their 金品 and 假底, and any 假底 still running. Each comes with what belongs to it. A focus the day shown
 *  does not have yet (a replay before it) leaves 当前形态. */
export function strongSet(s: Structures, focus: string | null, highlight: string[]): Set<string> {
  return emphasis(s, focus, highlight).strong
}

/** strongSet, and whether it is 当前形态 (`now`: no focus the day has, no judgement) — then its prices go to the right edge. */
export function emphasis(s: Structures, focus: string | null, highlight: string[]): { strong: Set<string>; now: boolean } {
  const known = new Set([...s.boxes.map((b) => b.id), ...s.slant.map((x) => x.id), ...s.false_bottoms.map(fbId)])
  if (focus && boxesOf(focus).every((id) => known.has(id))) return { strong: withRelated(s, [focus]), now: false }
  if (highlight.length) return { strong: withRelated(s, highlight), now: false }
  const now = [...boxesOnDay(s.boxes).map((b) => b.id), ...s.false_bottoms.filter((f) => f.end === null).map(fbId)]
  return { strong: withRelated(s, now), now: true }
}

/** A price read at the right edge: its text, and its tone (a box edge — `scale`: its box's, for its colour —, a 假底, the
 *  left pressure, short of the 10% minimum). */
export interface LevelTag { price: number; text: string; tone: "box" | "falseBottom" | "pressure" | "short"; scale?: Box["scale"] }

const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`

/** 当前形态's prices, highest first: each box's top and bottom (原 for one that ended: where it may hold now), the 假底 and the
 *  left pressure, with how far each is from `close` (the day shown) — short, for the room right of the last bar (the side
 *  panel has them in words). Two edges within 0.3% of each other read as one: the bigger box's, its name and price. */
export function levelTags(s: Structures, strong: Set<string>, close: number, digits: number, pressure: Pressure | null): LevelTag[] {
  const edges: (LevelTag & { rank: number })[] = []
  for (const b of s.boxes) {
    const seg = b.segments.at(-1)
    if (!strong.has(b.id) || b.scale === "short" || b.status !== "established" || !seg) continue
    const name = `${b.ended_at ? "原" : ""}${SCALE_NAME[b.scale]}`, rank = b.scale === "long" ? 2 : 1
    edges.push({ price: seg.top, text: `${name}顶`, tone: "box", scale: b.scale, rank },
      { price: seg.bottom, text: `${name}底`, tone: "box", scale: b.scale, rank })
  }
  edges.sort((a, b) => b.price - a.price)
  const merged: typeof edges = []
  for (const e of edges) {
    const last = merged.at(-1)
    if (!last || Math.abs(last.price / e.price - 1) >= 0.003) merged.push({ ...e })
    else if (e.rank > last.rank) Object.assign(last, e)
  }
  const away = (x: number) => signed(x / close - 1)
  const out: LevelTag[] = merged.map((e) => ({ price: e.price, text: `${e.text} ${num(e.price, digits)} ${away(e.price)}`, tone: e.tone,
    scale: e.scale }))
  for (const f of s.false_bottoms) {   // its price here, not at the line's start on the latest bars
    if (strong.has(fbId(f)) && f.end === null) out.push({ price: f.price, text: `假底 ${num(f.price, digits)} ${away(f.price)}`, tone: "falseBottom" })
  }
  if (pressure && pressure.bottom != null && pressure.space != null) {
    const p = pressureText(pressure, digits)
    // the price already inside the old box (left downward, its top still above): the trapped supply runs to its top
    const top = pressure.top != null && pressure.bottom <= close && pressure.top > close ? pressure.top : null
    out.push(top !== null ? { price: top, text: `左侧压力上沿 ${num(top, digits)} ${away(top)}`, tone: "short" }
      : { price: pressure.bottom, text: `左侧压力 ${num(pressure.bottom, digits)} ${signed(pressure.space)}`, tone: p.short ? "short" : "pressure" })
  }
  return out.sort((a, b) => b.price - a.price)
}

// ---- 当前形态 as a plan: the side panel's 价位梯 -----------------------------------------------------------------
/** A price on the 价位梯: a line a plan is made of — 突破线 (where the scan's rules call it a breakout), 左侧压力 (how much
 *  room), the box's edges (盯盘 alerts at the top), 现价, the 失效线 that acts — with the structure it belongs to (the pointer
 *  resting on it shows it on the chart, a click picks it) and the chart's colour for it (a CSS variable). */
export interface Rung {
  kind: "break" | "pressure" | "top" | "bottom" | "price" | "fail"
  price: number
  label: string    // 突破线, 左侧压力, 上沿 (大箱上沿 with two boxes, 原上沿 for one left), 现价, 失效线, 下沿
  note: string     // what it is, short: 上品小箱底, 假底, 中箱底 × 0.985, 空间不到 10%
  hint: string     // in full, on hover
  id: string | null; from: string | null; to: string | null   // the structure (a click picks it) and its days
  color: string | null; dash: boolean
  warn: boolean    // 左侧压力 under the 10% minimum
}

const BREAK = 0.015    // a top is broken at × (1 + 1.5%), as the box primitive breaks it (潜伏 and 盯盘 read it so)
/** The band a structure set was found with: a day's 1.5%, another period's its own (design 2026-10-12-structures-every-period). */
const bandOf = (s: Structures) => s.tol ?? BREAK
const times = (k: number) => k.toFixed(3).replace(/0+$/, "")   // 1.035, 0.985
const FAIL_BARS = 60   // a 金品, or a box left upward, gives a 失效线 this many bars (观察页「持有中」's suggestions: the same)
const SCALE_VAR: Record<Box["scale"], string> = { long: "--box-long", mid: "--box-mid", short: "--box-short" }
const RANK: Record<Box["scale"], number> = { long: 2, mid: 1, short: 0 }

/** A slanted edge's price on day `d` along its line, between the points around it — by bar, as it is drawn (`dates`: the
 *  bars' days; a day not among them, by calendar). Null off the line's days. */
export function lineAt(pts: PricePoint[], d: string, dates: string[]): number | null {
  const i = pts.findIndex((p) => p.date >= d)
  if (i < 0 || (i === 0 && pts[0].date > d)) return null
  if (pts[i].date === d) return pts[i].price
  const [a, b] = [pts[i - 1], pts[i]]
  const [ia, ib, id] = [dates.indexOf(a.date), dates.indexOf(b.date), dates.indexOf(d)]
  const f = ia >= 0 && ib > ia && id >= 0 ? (id - ia) / (ib - ia) : (Date.parse(d) - Date.parse(a.date)) / (Date.parse(b.date) - Date.parse(a.date))
  return a.price + (b.price - a.price) * f
}

/** The 失效线 that acts on a stock (观察页「持有中」's suggestions, the same lines): of a 金品's small-box bottom (known within
 *  60 bars; a 变异金品's slant bottom as it stood when the box was drawn), a 假底 still running and a 中箱 / 大箱's bottom ×
 *  0.985 (standing, or left upward within 60 bars), the highest under the last close that no close since has crossed —
 *  usually the 金品, then the 假底, then the box. `bars`: the settled bars up to the day shown. */
export function failLine(s: Structures, bars: { date: string; close: number }[]): Rung | null {
  const last = bars.at(-1)
  if (!last) return null
  const dates = bars.map((b) => b.date)
  const recent = dates[Math.max(0, dates.length - FAIL_BARS)]
  const all = [...s.boxes.map((b) => b.id), ...s.slant.map((x) => x.id)]
  const cands: (Rung & { since: string; order: number })[] = []
  const base = { kind: "fail" as const, label: "失效线", warn: false }
  for (const g of s.gold.filter((x) => x.known_at >= recent)) {
    const flat = s.boxes.find((b) => b.id === g.small_box_id), slant = s.slant.find((x) => x.id === g.small_box_id)
    const price = flat ? flat.segments.at(-1)?.bottom : slant ? lineAt(slant.bottom, g.known_at, dates) : null
    const name = `${g.variant ? "变异" : ""}${GOLD_NAME[g.kind] ?? g.kind}小箱底`
    const span = spanOf(s, g.small_box_id)
    if (price != null && all.includes(g.small_box_id)) {
      cands.push({ ...base, price, note: name, hint: `${name}：收盘跌破它，这个金品就不成立了`, id: g.small_box_id, from: span?.start ?? null,
        to: span?.end ?? null, color: "--box-short", dash: true, since: g.known_at, order: 0 })
    }
  }
  for (const f of s.false_bottoms.filter((x) => x.end === null)) {
    cands.push({ ...base, price: f.price, note: "假底", id: fbId(f), from: f.date, to: f.end, color: "--false-bottom", dash: true, since: f.known_at, order: 1,
      hint: `假底（${f.source === "金品" ? "金品的底" : "突破前最后一次回落的低点"}）：收盘跌破它，这次突破可能失败` })
  }
  for (const b of s.boxes) {
    const seg = b.segments.at(-1)
    const live = !b.ended_at || (b.end_reason === "上破离箱" && b.ended_at >= recent)
    if (!seg || b.status !== "established" || b.scale === "short" || !live) continue
    const k = times(1 - bandOf(s))
    cands.push({ ...base, price: seg.bottom * (1 - bandOf(s)), note: `${SCALE_NAME[b.scale]}底 × ${k}`, id: b.id, from: b.start, to: b.end,
      hint: `${SCALE_NAME[b.scale]}的下沿 × ${k}：收盘跌破就是破箱`, color: SCALE_VAR[b.scale], dash: true, since: b.known_at, order: 2 })
  }
  const held = cands.filter((c) => c.price < last.close && bars.every((x) => x.date <= c.since || x.close >= c.price))
  const top = held.sort((a, b) => b.price - a.price || a.order - b.order)[0]
  if (!top) return null
  const { since: _since, order: _order, ...rung } = top
  return rung
}

/** 当前形态's 价位梯, highest first: each box described (the big box standing, or the one just left, and the 中箱 inside it)
 *  with its top and bottom and, standing, its 突破线; the left pressure; 现价 (`price`: the last price on screen) and the
 *  失效线 that acts (failLine; `bars` the settled bars up to the day shown). Two boxes' lines within 0.3% are one, the
 *  bigger box's. */
export function ladder(s: Structures, price: number | null, pressure: Pressure | null, bars: { date: string; close: number }[],
                       digits = 2): Rung[] {
  const shown = boxesOnDay(s.boxes).sort((a, b) => RANK[b.scale] - RANK[a.scale])
  const two = shown.length > 1
  const out: Rung[] = []
  const add = (r: Rung) => { if (!out.some((x) => x.kind === r.kind && Math.abs(x.price / r.price - 1) < 0.003)) out.push(r) }
  for (const b of shown) {
    const seg = b.segments.at(-1)
    if (!seg) continue
    const name = two ? SCALE_NAME[b.scale] : "", was = b.ended_at ? "原" : ""
    const line = { id: b.id, from: b.start, to: b.end, color: SCALE_VAR[b.scale], dash: false, warn: false }
    if (!b.ended_at) {
      add({ ...line, kind: "break", price: seg.top * (1 + bandOf(s)), label: `${name}突破线`, note: "",
        hint: s.tol == null || s.period === "day" ? `${SCALE_NAME[b.scale]}上沿 × 1.015：收盘站上它，潜伏规则才算突破（盯盘在上沿就提醒）`
          : `${SCALE_NAME[b.scale]}上沿 × ${times(1 + s.tol)}：收盘站上它就是这个周期的破箱（选股、盯盘按日线的箱）` })
    }
    add({ ...line, kind: "top", price: seg.top, label: `${was}${name}上沿`, note: "", hint: `${SCALE_NAME[b.scale]}的上沿${was ? "（已离箱）"
      : "：观察中的股票，盯盘在这里提醒——离它 1% 以内、创当日新高、在均价线上方时（在箱顶设预警）"}` })
    add({ ...line, kind: "bottom", price: seg.bottom, label: `${was}${name}下沿`, note: "", hint: `${SCALE_NAME[b.scale]}的下沿${was ? "（已离箱）" : ""}` })
  }
  if (pressure && !pressure.none_above && pressure.bottom != null && pressure.space != null) {
    // 现价 already inside the old box (one left downward, its top still above): the trapped supply runs to its top
    const inside = price !== null && pressure.bottom <= price && pressure.top != null && pressure.top > price
    const short = pressure.space < 0.1
    out.push({ kind: "pressure", price: inside ? pressure.top! : pressure.bottom, label: "左侧压力",
      note: inside ? "在压力区里" : short ? "空间不到 10%" : "", id: pressure.box_id,
      from: pressure.start ?? null, to: pressure.end ?? null, color: "--warning", dash: false, warn: short,
      hint: `左边离现价最近、还在上方的旧箱子，下沿 ${num(pressure.bottom, digits)} 到上沿 ${pressure.top != null ? num(pressure.top, digits) : "—"} 是压力区${inside
        ? "：现价已经在里面，上沿以下都有套牢盘" : `；箱外空间 +${(pressure.space * 100).toFixed(1)}%（至少要 10%）`}` })
  }
  if (price !== null) out.push({ kind: "price", price, label: "现价", note: "", hint: "", id: null, from: null, to: null, color: null, dash: false, warn: false })
  const fail = failLine(s, bars)
  if (fail) out.push(fail)
  return out.sort((a, b) => b.price - a.price)
}

/** 日线价位 on a minute chart: the daily 价位梯's lines but 现价 — 突破线, the edges, 失效线 — named 日线…, against the
 *  minute chart's last close. */
export function dayMarks(rungs: Rung[], close: number, digits: number): Mark[] {
  return rungs.flatMap((r) => {
    const m = markOf(r, close, digits)
    return m ? [{ ...m, text: `日线${m.text}` }] : []
  })
}

/** 日线价位 on a minute chart (design 2026-10-12-structures-every-period): the daily 价位梯's lines but 现价, named 日线…,
 *  against the minute chart's last close (`close`); `bars`: the settled daily bars. */
export function dayLines(s: Structures, close: number, bars: { date: string; close: number }[], digits: number): Mark[] {
  return dayMarks(ladder(asOf(s, null), close, null, bars, digits), close, digits)   // each box as it stands now, not its old versions
}

/** 分时's daily box top: the daily 价位梯's top nearest above the last daily close (else the highest), as 盯盘's 分时 draws it. */
export function dayTop(s: Structures, bars: { date: string; close: number }[], digits: number): number | null {
  const last = bars.at(-1)?.close
  if (last == null) return null
  const tops = ladder(asOf(s, null), last, null, bars, digits).filter((r) => r.kind === "top").map((r) => r.price)
  return tops.filter((t) => t >= last).sort((a, b) => a - b)[0] ?? (tops.length ? Math.max(...tops) : null)
}

/** A 价位梯 line while the pointer rests on its row: drawn on the chart as itself — at its price, in its colour, from its
 *  structure's start — with its price at the right edge among 当前形态's (levelsWith). 现价 has none. */
export interface Mark extends LevelTag { from: string | null; dash: boolean; heavy: boolean }
const TONE: Record<string, Pick<LevelTag, "tone" | "scale">> = {
  "--box-long": { tone: "box", scale: "long" }, "--box-mid": { tone: "box", scale: "mid" }, "--box-short": { tone: "box", scale: "short" },
  "--false-bottom": { tone: "falseBottom" },
}

export function markOf(r: Rung, close: number, digits: number): Mark | null {
  if (r.kind === "price" || !r.color) return null
  return { price: r.price, text: `${r.label} ${num(r.price, digits)} ${signed(r.price / close - 1)}`,
    ...(TONE[r.color] ?? { tone: r.warn ? "short" : "pressure" }), from: r.from, dash: r.dash, heavy: r.kind === "break" || r.kind === "fail" }
}

/** 当前形态's prices with a line pointed at among them: the one already at its price (the box's top, for its 上沿) gives way. */
export function levelsWith(levels: LevelTag[], m: Mark): LevelTag[] {
  const { price, text, tone, scale } = m
  return [...levels.filter((t) => Math.abs(t.price / price - 1) >= 0.0005), { price, text, tone, scale }].sort((a, b) => b.price - a.price)
}

/** Where tags go down the right edge (their centres): each at its price's y when it is free; crowded, they keep the
 *  prices' order from the top down (review 10-13: moved to the nearest free place, 中箱顶 could land under 中箱底) — the
 *  first given (the nearest the close) stays at its price, the others are pushed away from it in turn, all moved back
 *  in when the pane's edge stops them; inside the pane ([top, bottom], half a tag in) and clear of `taken` (昨收 / 涨停's
 *  labels). */
export function stackTags(ys: number[], gap: number, top: number, bottom: number, taken: number[] = []): number[] {
  if (!ys.length) return []
  const lo = top + gap / 2, hi = bottom - gap / 2
  const clamp = (y: number) => Math.min(hi, Math.max(lo, y))
  const clear = (y: number, dir: number) => {   // stepped past 昨收 / 涨停's labels, in `dir`
    for (let moved = true; moved;) {
      moved = false
      for (const t of taken) if (Math.abs(t - y) < gap) { y = t + dir * gap; moved = true }
    }
    return y
  }
  const idx = ys.map((_, i) => i).sort((a, b) => ys[a] - ys[b] || a - b)   // top down
  const at = idx.map((i) => clamp(ys[i]))
  const k = idx.indexOf(0)
  const down = clear(at[k], 1), up = clear(at[k], -1)
  at[k] = Math.abs(down - at[k]) <= Math.abs(up - at[k]) && down <= hi ? down : up >= lo ? up : down
  for (let j = k + 1; j < at.length; j++) at[j] = clear(Math.max(at[j], at[j - 1] + gap), 1)
  for (let j = k - 1; j >= 0; j--) at[j] = clear(Math.min(at[j], at[j + 1] - gap), -1)
  if (at[at.length - 1] > hi) {   // past the bottom: back up, in order
    at[at.length - 1] = hi
    for (let j = at.length - 2; j >= 0; j--) at[j] = Math.min(at[j], at[j + 1] - gap)
  }
  if (at[0] < lo) {   // past the top: back down, in order
    at[0] = lo
    for (let j = 1; j < at.length; j++) at[j] = Math.max(at[j], at[j - 1] + gap)
  }
  const out = new Array<number>(ys.length)
  idx.forEach((i, j) => { out[i] = at[j] })
  return out
}

/** The slant boxes: all with the 斜箱 layer, and a 变异金品's (a slant small box) with the 金品 layer, as visibleBoxes. */
export function visibleSlants<T extends { id: string }>(slant: T[], layers: Layers, gold: Gold[] = [], highlight: string[] = []): T[] {
  const golden = new Set(layers.gold ? gold.map((g) => g.small_box_id) : [])
  return slant.filter((s) => layers.slant || golden.has(s.id) || highlight.includes(s.id))
}

const num = (v: number, digits: number) => v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })

/** A box in words: its size, its width, and (`len`) its candles' mean length inside. */
export function boxText(b: Box, len?: number | null): string {
  const parts = [b.length ? `${SCALE_NAME[b.scale]} ${b.length} 根` : SCALE_NAME[b.scale]]
  if (b.width !== null) parts.push(`箱宽 ${(b.width * 100).toFixed(1)}%`)
  if (len != null) parts.push(`K 线均长 ${(len * 100).toFixed(1)}%`)
  return parts.join(" · ")
}

/** The boxes to describe on a day (the structures as of it): the big box standing (or, none standing, the last one drawn)
 *  and the 中箱 standing inside it (the inner machine's: id …:inner:…). */
export function boxesOnDay(boxes: Box[]): Box[] {
  const big = boxes.filter((b) => b.status === "established" && b.scale !== "short")
  const outer = big.filter((b) => !b.id.includes(":inner:"))
  const standing = outer.filter((b) => !b.ended_at)
  const top = standing.at(-1) ?? outer.at(-1)
  const inner = top && !top.ended_at ? big.filter((b) => b.id.includes(":inner:") && !b.ended_at && b.start > top.start).at(-1) : undefined
  return [top, inner].filter((b): b is Box => !!b)
}

/** 箱内 K 线平均长度: the mean of (high − low) ÷ the close before over the bars from `from` to `to` (null: none loaded
 *  there, or only the first loaded bar, which has no close before it). The candles' length in a consolidation: the
 *  shorter, the calmer (short, even candles mean controlled trading). */
export type BarLen = (from: string, to: string, beforeTo?: boolean) => number | null   // barLength over the loaded bars

/** 箱内 K 线平均长度 of a box: over its bars to its end (an ended box's break bar is not its own), else to the day shown. */
export function boxCandles(b: Box, day: string | null, barLen?: BarLen): number | null {
  const ended = !!(b.ended_at && b.end)
  return barLen?.(b.start, ended ? b.end! : (day ?? "9999-12-31"), ended) ?? null
}

export function barLength(bars: { date: string; high: number; low: number; close: number }[], from: string, to: string,
                          beforeTo = false): number | null {   // beforeTo: an ended box's break bar is not its own
  let sum = 0, n = 0
  for (let i = 1; i < bars.length; i++) {
    const b = bars[i]
    if (b.date < from || b.date > to || (beforeTo && b.date === to) || !(bars[i - 1].close > 0)) continue
    sum += (b.high - b.low) / bars[i - 1].close
    n++
  }
  return n ? sum / n : null
}

/** A box not ended whose last break waits to be decided — R bars to come back (回箱) or to leave (离箱): "up" or "down",
 *  else null. The close is outside it then, so the panel must not say 仍在箱内 (review 10-13: 宁德时代's week 13% under). */
export function pendingBreak(b: Pick<Box, "ended_at" | "events">): "up" | "down" | null {
  if (b.ended_at) return null
  let p: "up" | "down" | null = null
  for (const e of [...b.events].sort((x, y) => x.date.localeCompare(y.date))) {
    if (e.event === "break_up") p = "up"
    else if (e.event === "break_down") p = "down"
    else if (e.event === "return" || e.event === "leave_up" || e.event === "leave_down") p = null
  }
  return p
}

/** When the box was drawn: the day it was first drawn, and its last redraw if it was redrawn since. */
export function boxSince(b: Pick<Box, "known_at" | "first_known">): string {
  const first = b.first_known ?? b.known_at
  return first === b.known_at ? `${first} 成立` : `${first} 成立 · ${b.known_at} 重画`
}

/** 手画对照 (spec §6.3, §5.10): one sample's agreement with the algorithm, short enough for its tag. */
export function labelNote(r: LabelResult): string {
  if (!r.is_box) return r.false_draw ? "算法在这里画了箱" : "算法没画（对）"
  if (!r.box_id) return "算法没画箱"
  return `重叠 ${r.overlap?.toFixed(2) ?? "—"} · ${r.consistent ? "一致" : "不一致"}`
}

/** The pressure label; `short` when the space is under the 10% minimum. */
export function pressureText(p: Pressure, digits: number): { text: string; short: boolean } {
  if (p.none_above) return { text: "上方没有左侧压力", short: false }
  if (p.bottom == null || p.space == null) return { text: "", short: false }
  return { text: `左侧压力 ${num(p.bottom, digits)} · 箱外空间 +${(p.space * 100).toFixed(1)}%`, short: p.space < 0.1 }
}

// ---- what the indicators are given ------------------------------------------------------------
export interface StructView {
  structures: Structures; layers: Layers; palette: Palette | null; digits: number
  highlight: string[]           // ids of the structures the selected rule binding uses
  focus?: string | null         // the structure you picked in the list (its box, 金品 or 假底 id)
  strong?: string[]             // drawn in full (strongSet); the rest faint. None given: all in full (the gallery's mini charts)
  now?: boolean                 // strong is 当前形态: its touches, marks and lost 假底 wait in the panel until one is picked
  levels?: LevelTag[]           // 当前形态's prices at the right edge (levelTags)
  rung?: Rung | null            // the 价位梯 row the pointer rests on: its line is drawn as itself (mark)
  mark?: Mark | null
  pressure: Pressure | null; labels: Label[]
  labelNotes?: Record<string, string> // label id → how the algorithm agrees with it (手画对照), shown on its tag
  barLen?: BarLen   // 箱内 K 线平均长度 over the loaded bars, on the boxes' tags
  dayLines?: Mark[] // 日线价位 on a minute chart: the daily 价位梯's lines (dayMarks)
  dates?: string[]   // the stock's bar days (the replay's stops): a slanted edge is cut on the replay day by bar (asOf)
}
/** 危险提醒 on the chart (design 2026-10-15 §3): the day it came and its short name. */
export interface DangerMark { date: string; label: string }
/** 价格行为分析's plan on the chart (design 2026-10-20 §6.2): a price from the first open the plan may use to its sale
 *  (null: still open), dashed until it is bought; with 不买, the nearest support and resistance it found instead. */
export interface PlanLine {
  kind: "entry" | "stop" | "tp1" | "tp2" | "support" | "resistance"; price: number; from: string; to: string | null; filled: boolean
}
export interface RuleView { signals: Signal[]; bands: Band[]; danger: DangerMark[]; plan: PlanLine[]; palette: Palette | null }
export const EMPTY_STRUCT: StructView = { structures: { boxes: [], slant: [], pivots: [], swings: [], gold: [], false_bottoms: [] },
  layers: DEFAULT_LAYERS, palette: null, digits: 2, highlight: [], pressure: null, labels: [] }
export const EMPTY_RULE: RuleView = { signals: [], bands: [], danger: [], plan: [], palette: null }


/** 对比 (spec §8.3): another listing or index, drawn scaled to meet this one at the first visible bar. */
export interface CompareView { name: string; closes: Record<number, number>; palette: Palette | null }
export const EMPTY_COMPARE: CompareView = { name: "", closes: {}, palette: null }

/** What an indicator's extendData holds. KLineChart deep-merges a new extendData into the old one
 *  (arrays element by element, so a shorter list keeps the old tail); a function is replaced whole. */
export interface Live<T> { view: () => T }
