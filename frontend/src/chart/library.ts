// 指标库 on the chart (design 2026-10-10 §5): each indicator a draw-only KLineChart indicator WB_LIB_<id>. Its values
// come from the backend for the chart's own bars (/api/indicator-series: the same series, basis and forming bar as
// /api/bars) and are laid on the bars by time. Lines, dots and sticks are figures (they give the legend its values);
// STICKLINE, icons, text, bands, zones, levels and a sub pane's reference lines are painted.
import { type IndicatorTemplate, type KLineData, registerIndicator } from "klinecharts"
import type { Basis, Period } from "@/lib/api"
import { type DrawItem, indicatorEndpoints, type IndicatorSeries } from "@/lib/indicators"
import { alpha, fresh } from "./paint"
import { fs, type Palette } from "./theme"

export interface LibExt { id: string; version: number; listingId: string; period: Period; basis: Basis; live: boolean }
export interface LibMeta { id: string; version: number; name: string; pane: "main" | "sub"; items: DrawItem[]; levels: number[] }

export const libName = (id: string) => `WB_LIB_${id}`
const FIGURED = new Set<DrawItem["kind"]>(["line", "dots", "stick", "colorstick", "volstick", "linestick"])

let palette: Palette
export const setLibraryPalette = (p: Palette) => { palette = p }

// ---- the values -----------------------------------------------------------------------------------------------
const results = new Map<string, IndicatorSeries>()             // by indicator name: the series last laid on the bars
const requests = new Map<string, Promise<IndicatorSeries>>()   // by what was asked: one request per bars' state

/** The indicator's series for these bars: asked again when the bars end otherwise (a load, the bar forming). */
export function fetchSeries(ext: LibExt, last: KLineData | undefined): Promise<IndicatorSeries> {
  const key = [ext.id, ext.version, ext.listingId, ext.period, ext.basis, ext.live, last?.timestamp, last?.close].join("|")
  let p = requests.get(key)
  if (!p) {
    p = indicatorEndpoints.series(ext.listingId, ext.id, ext.period, ext.basis, ext.live)
      .catch((e) => ({ id: ext.id, version: ext.version, name: "", error: String(e) }))
    requests.set(key, p)
    while (requests.size > 64) requests.delete(requests.keys().next().value!)
  }
  return p
}

/** The indicator's state on the chart, for the legend and for checks (null: not asked yet). */
export const libraryResult = (id: string) => results.get(libName(id)) ?? null

type Row = Record<string, number | boolean | null | undefined>

function colorOf(c: string | null, k: number): string {
  if (c === "up") return palette.up
  if (c === "down") return palette.down
  return c ?? [palette.ma1, palette.ma2, palette.ma3, palette.ma4, palette.muted][k % 5]
}

// ---- the template ----------------------------------------------------------------------------------------------
/** The KLineChart indicator for one library indicator (its figures follow what it draws). */
export function registerLibrary(meta: LibMeta): string {
  const name = libName(meta.id)
  const items = meta.items
  const figures: NonNullable<IndicatorTemplate["figures"]> = []
  let k = 0
  for (const it of items) {
    if (!FIGURED.has(it.kind)) continue
    const col = it.cols.value, n = k++
    const title = `${it.name} `
    const color = () => (it.hidden ? "transparent" : colorOf(it.color, n))
    if (it.kind === "line" || it.kind === "linestick") {
      figures.push({ key: col, title, type: "line",
        styles: () => ({ color: color(), size: it.width, style: it.dash ? "dashed" : "solid", dashedValue: [4, 3] }) })
    }
    if (it.kind === "dots") {
      figures.push({ key: col, title, type: "circle", attrs: ({ coordinate }) => ({ x: coordinate.current.x, y: (coordinate.current as Record<string, number>)[col], r: 2 + (it.width - 1) / 2 }),
        styles: () => ({ color: color(), style: "fill" }) })
    }
    if (it.kind === "stick" || it.kind === "linestick" || it.kind === "colorstick" || it.kind === "volstick") {
      const key = it.kind === "linestick" ? `${col}__stick` : col
      figures.push({ key, title: it.kind === "linestick" ? "" : title, type: "bar", baseValue: 0,
        styles: ({ data }) => {
          const r = data.current as Row | null
          const v = (r?.[key] as number) ?? 0
          const c = it.hidden ? "transparent" : it.kind === "colorstick" ? (v >= 0 ? palette.up : palette.down)
            : it.kind === "volstick" ? ((r?.__close as number) >= (r?.__open as number) ? palette.up : palette.down) : color()
          return { color: c, borderColor: c, style: "fill" }
        } })
    }
  }
  if (meta.pane === "sub") {   // what is only painted still spans the pane's scale (STICKLINE's prices, a band's lines …)
    for (const it of items) {
      if (FIGURED.has(it.kind)) continue
      for (const [part, col] of Object.entries(it.cols)) {
        if (part !== "cond") figures.push({ key: col, title: "", type: "line", styles: () => ({ color: "transparent" }) })
      }
    }
  }
  const sticks = items.filter((x) => x.kind === "linestick").map((x) => x.cols.value)   // a line with its sticks: two figures
  const template: IndicatorTemplate<Row, number, LibExt> = {
    name, shortName: meta.name, series: meta.pane === "main" ? "price" : "normal", precision: 2, figures,
    calc: async (list, indicator) => {
      const ext = indicator.extendData
      if (!ext?.listingId) return list.map(() => ({}))
      const s = await fetchSeries(ext, list.at(-1))
      results.set(name, s)
      if (s.error || !s.t || !s.values) return list.map(() => ({}))
      const at = new Map(s.t.map((t, i) => [t, i]))
      const cols = Object.entries(s.values)
      return list.map((d) => {
        const i = at.get(d.timestamp)
        const row: Row = { __open: d.open, __close: d.close }
        if (i === undefined) return row
        for (const [col, vals] of cols) row[col] = vals[i] ?? null
        for (const col of sticks) row[`${col}__stick`] = row[col]
        return row
      })
    },
    draw: ({ ctx, chart, indicator, bounding, xAxis, yAxis }) => {
      const s = results.get(name)
      if (!s || s.error || !s.t) return false
      return fresh(ctx, () => {
        paint(ctx, chart.getDataList(), indicator.result as Row[], s, meta, bounding, chart.getVisibleRange(), chart.getBarSpace().bar,
          (i) => xAxis.convertToPixel(i), (v) => yAxis.convertToPixel(v))
        return false
      })
    },
  }
  registerIndicator(template)
  return name
}

// ---- painting -------------------------------------------------------------------------------------------------
type Bounding = { left: number; top: number; width: number; height: number }

function paint(ctx: CanvasRenderingContext2D, data: KLineData[], rows: Row[], s: IndicatorSeries, meta: LibMeta, b: Bounding,
               range: { from: number; to: number }, bar: number, x: (i: number) => number, y: (v: number) => number) {
  const from = Math.max(0, range.from), to = Math.min(data.length, range.to)
  const index = new Map(data.map((d, i) => [d.timestamp, i]))
  const right = b.left + b.width
  ctx.beginPath()
  ctx.rect(0, 0, right, b.height)   // the pane's own canvas: y from its top (bounding.top is where the pane sits in the chart)
  ctx.clip()
  for (const v of meta.levels) {   // a sub pane's reference lines
    const yy = Math.round(y(v)) + 0.5
    ctx.strokeStyle = alpha(palette.muted, 0.6)
    ctx.setLineDash([3, 3])
    ctx.beginPath(); ctx.moveTo(b.left, yy); ctx.lineTo(right, yy); ctx.stroke()
    ctx.setLineDash([])
  }
  meta.items.forEach((it, k) => {
    const color = colorOf(it.color, k)
    if (it.kind === "zone" || it.kind === "level") {
      for (const z of it.kind === "zone" ? (s.zones ?? []).filter((z) => z.item === k) : (s.levels ?? []).filter((z) => z.item === k)) {
        const i0 = index.get(z.s), i1 = z.e === null ? null : index.get(z.e)
        if (i0 === undefined || i0 >= to || (i1 !== null && i1 !== undefined && i1 < from)) continue
        const x0 = x(i0) - bar / 2, x1 = i1 === null || i1 === undefined ? right : x(i1) + bar / 2
        if ("top" in z) {
          const [yt, yb] = [y(z.top), y(z.bottom)]
          ctx.fillStyle = alpha(color, z.e === null ? 0.16 : 0.08)
          ctx.fillRect(x0, Math.min(yt, yb), x1 - x0, Math.max(1, Math.abs(yb - yt)))
          ctx.strokeStyle = alpha(color, z.e === null ? 0.6 : 0.3)
          ctx.lineWidth = 1
          ctx.strokeRect(Math.round(x0) + 0.5, Math.round(Math.min(yt, yb)) + 0.5, x1 - x0, Math.abs(yb - yt))
        } else {
          const yy = Math.round(y(z.price)) + 0.5
          ctx.strokeStyle = alpha(color, z.e === null ? 0.9 : 0.45)
          ctx.lineWidth = it.width
          ctx.setLineDash(it.dash ? [4, 3] : [])
          ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke()
          ctx.setLineDash([])
        }
      }
      return
    }
    if (it.kind === "band") {
      const [c1, c2] = [colorOf(it.color, k), colorOf(it.color2, k + 1)]
      for (let i = Math.max(1, from); i < to; i++) {
        const [a0, b0, a1, b1] = [rows[i - 1]?.[it.cols.v1], rows[i - 1]?.[it.cols.v2], rows[i]?.[it.cols.v1], rows[i]?.[it.cols.v2]]
        if (![a0, b0, a1, b1].every((v) => typeof v === "number")) continue
        ctx.fillStyle = alpha((a1 as number) >= (b1 as number) ? c1 : c2, 0.14)
        ctx.beginPath()
        ctx.moveTo(x(i - 1), y(a0 as number)); ctx.lineTo(x(i), y(a1 as number)); ctx.lineTo(x(i), y(b1 as number)); ctx.lineTo(x(i - 1), y(b0 as number))
        ctx.closePath(); ctx.fill()
      }
      return
    }
    if (FIGURED.has(it.kind)) return
    for (let i = from; i < to; i++) {
      const r = rows[i]
      if (!r || r[it.cols.cond] !== true) continue
      const xx = x(i)
      if (it.kind === "stickline") {
        const [p1, p2] = [r[it.cols.p1], r[it.cols.p2]]
        if (typeof p1 !== "number" || typeof p2 !== "number") continue
        const w = Math.max(1, (bar * Math.min(it.width, 10)) / 10)
        const [ya, yb] = [y(p1), y(p2)]
        if (it.empty) {
          ctx.strokeStyle = color; ctx.lineWidth = 1
          ctx.strokeRect(Math.round(xx - w / 2) + 0.5, Math.round(Math.min(ya, yb)) + 0.5, Math.round(w), Math.max(1, Math.abs(yb - ya)))
        } else {
          ctx.fillStyle = color
          ctx.fillRect(xx - w / 2, Math.min(ya, yb), w, Math.max(1, Math.abs(yb - ya)))
        }
        continue
      }
      const price = r[it.cols.price]
      if (typeof price !== "number") continue
      const d = data[i]
      const below = price <= d.low + 1e-9 ? 1 : price >= d.high - 1e-9 ? -1 : 0   // under the bar, over it, or at the price
      if (it.kind === "icon") icon(ctx, it.icon ?? 3, it.color === null ? null : color, xx, y(price), below, bar)
      else {
        const text = it.kind === "text" ? it.text ?? "" : String(r[it.cols.number] ?? "")
        ctx.fillStyle = color
        ctx.font = `${fs(palette, 11)}px ${palette.fontSans}`
        ctx.textAlign = "center"
        ctx.textBaseline = below > 0 ? "top" : below < 0 ? "bottom" : "middle"
        ctx.fillText(text, xx, y(price) + below * 14)
      }
    }
  })
}

/** 通达信-style icons: 1 up arrow (涨 colour, under the bar), 2 down arrow (跌 colour, over it), 3 a dot, 4 a ring,
 *  5 a cross, 6 a diamond; any other number a dot. */
function icon(ctx: CanvasRenderingContext2D, n: number, color: string | null, x: number, y: number, below: number, bar: number) {
  const r = Math.max(3, Math.min(6, bar / 2))
  const c = color ?? (n === 2 || n === 6 ? palette.down : n === 1 ? palette.up : palette.ma1)
  ctx.fillStyle = c
  ctx.strokeStyle = c
  ctx.lineWidth = 1.5
  if (n === 1 || n === 2) {
    const up = n === 1
    const tip = up ? y + (below >= 0 ? 4 : -2 * r - 4) : y - (below <= 0 ? 4 : -2 * r - 4)
    const base = up ? tip + 2 * r : tip - 2 * r
    ctx.beginPath(); ctx.moveTo(x, tip); ctx.lineTo(x - r, base); ctx.lineTo(x + r, base); ctx.closePath(); ctx.fill()
    return
  }
  const cy = y + below * (r + 4)
  ctx.beginPath()
  if (n === 4) { ctx.arc(x, cy, r, 0, Math.PI * 2); ctx.stroke(); return }
  if (n === 5) { ctx.moveTo(x - r, cy - r); ctx.lineTo(x + r, cy + r); ctx.moveTo(x + r, cy - r); ctx.lineTo(x - r, cy + r); ctx.stroke(); return }
  if (n === 6) { ctx.moveTo(x, cy - r); ctx.lineTo(x + r, cy); ctx.lineTo(x, cy + r); ctx.lineTo(x - r, cy); ctx.closePath(); ctx.fill(); return }
  ctx.arc(x, cy, r * 0.8, 0, Math.PI * 2); ctx.fill()
}
