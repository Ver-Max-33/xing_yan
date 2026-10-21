// Draw-only indicators: every value is computed by the backend (spec §8.1 "所见即所算") and
// arrives on each bar; `calc` only picks fields, `draw` only paints. 看盘 preset: spec §8.6.
import { type Chart, type IndicatorTemplate, type KLineData, registerIndicator, registerLocale } from "klinecharts"
import { endpoints, type Period } from "@/lib/api"
import { bucketOf, type WbBar } from "./data"
import { fresh } from "./paint"
import { fs, type Palette } from "./theme"

// Module state read by the style callbacks (templates are global in KLineChart).
let palette: Palette
let course = true
let crossBy: "zero" | "ma60" = "zero"
export const setIndicatorPalette = (p: Palette) => { palette = p }
export const setCourseMode = (on: boolean) => { course = on }
export const setCrossMode = (mode: "zero" | "ma60") => { crossBy = mode }

type Row = Record<string, number | boolean | string | null | undefined>
const pick = (keys: string[]) => (list: KLineData[]): Row[] =>
  list.map((d) => Object.fromEntries(keys.map((k) => [k, (d as WbBar)[k as keyof WbBar] ?? null])) as Row)
const line = (key: string, title: string, color: () => string) => ({ key, title, type: "line", styles: () => ({ color: color() }) })

function visible(chart: Chart): [number, number] {
  const r = chart.getVisibleRange()
  return [Math.max(0, r.from), Math.min(chart.getDataList().length, r.to)]
}

const MA: IndicatorTemplate = {
  name: "WB_MA", shortName: "MA", series: "price", precision: 2, shouldOhlc: false,
  figures: [
    line("ma5", "MA5 ", () => palette.ma1), line("ma10", "MA10 ", () => palette.ma2),
    line("ma20", "MA20 ", () => palette.ma3),
    {
      key: "ma60", title: "MA60 ", type: "line",
      // 看盘 preset: the 60-day line ("生命线") changes colour with its direction.
      styles: ({ data }) => {
        const up = (data.current as Row | null)?.ma60_up
        return { color: course && up !== null && up !== undefined ? (up ? palette.ma3 : palette.down) : palette.ma4, size: course ? 1.5 : 1 }
      },
    },
  ],
  calc: pick(["ma5", "ma10", "ma20", "ma60", "ma60_up"]),
}

const BOLL: IndicatorTemplate = {
  name: "WB_BOLL", shortName: "BOLL", series: "price", precision: 2,
  figures: [line("boll_up", "UP ", () => palette.ma2), line("boll_mid", "MID ", () => palette.ma1), line("boll_low", "LOW ", () => palette.ma3)],
  calc: pick(["boll_up", "boll_mid", "boll_low"]),
}

const VOL: IndicatorTemplate = {
  name: "WB_VOL", shortName: "倍量", series: "volume", precision: 0, shouldFormatBigNumber: true,
  figures: [
    {
      key: "volume", title: "量 ", type: "bar", baseValue: 0,
      // 倍量 colouring (spec §8.6): limit-up on >= 2x volume / down day on <= half volume / 地量. Today's bar still
      // forming (§8.5) has only the volume so far: outlined dashed, never classed as a whole day's.
      styles: ({ data }) => {
        const d = data.current as Row | null
        if (d?.temp) {
          const c = (d.close as number) >= (d.open as number) ? palette.up : palette.down
          return { color: c, borderColor: c, style: "stroke", borderStyle: "dashed", borderDashedValue: [3, 2] }
        }
        const color = d?.vol_class === "limit_double" ? palette.ma1
          : d?.vol_class === "down_half" ? palette.ma2
          : d?.vol_class === "ground" ? palette.ma4
          : (d?.close as number) >= (d?.open as number) ? palette.up : palette.down
        return { color, borderColor: color, style: "fill" }
      },
    },
    line("vol_ma5", "MA5 ", () => palette.ma1), line("vol_ma10", "MA10 ", () => palette.ma2),
  ],
  calc: pick(["volume", "vol_ma5", "vol_ma10", "vol_class", "open", "close", "temp"]),
}

const MACD: IndicatorTemplate = {
  name: "WB_MACD", shortName: "MACD", series: "normal", precision: 3, calcParams: [12, 26, 9],
  figures: [
    line("dif", "DIF ", () => palette.ma1), line("dea", "DEA ", () => palette.ma2),
    {
      key: "macd", title: "MACD ", type: "bar", baseValue: 0,
      styles: ({ data }) => {
        const v = (data.current as Row | null)?.macd as number
        const c = v >= 0 ? palette.up : palette.down
        return { color: c, borderColor: c, style: "fill" }
      },
    },
  ],
  calc: pick(["dif", "dea", "macd", "macd_cross", "macd_cross_ma60"]),
  // Golden crosses: below the zero axis (水下) and above it (水上) get different marks (spec §8.6);
  // the switch classes them by price against MA60 instead (a cross before MA60 exists: a neutral mark).
  draw: ({ ctx, chart, indicator, xAxis, yAxis }) => {
    const [from, to] = visible(chart)
    const res = indicator.result as Row[]
    for (let i = from; i < to; i++) {
      const r = res[i]
      if (!r?.macd_cross || r.dif === null) continue
      const x = xAxis.convertToPixel(i)
      const y = yAxis.convertToPixel(r.dif as number)
      const cls = crossBy === "zero" ? r.macd_cross : r.macd_cross_ma60
      ctx.fillStyle = cls === "below" ? palette.ma1 : cls === "above" ? palette.up : palette.muted
      ctx.fillRect(x - 3, y - 3, 6, 6)
    }
    return false
  },
}

const KDJ: IndicatorTemplate = {
  name: "WB_KDJ", shortName: "KDJ", series: "normal", precision: 2, calcParams: [9, 3, 3],
  figures: [line("k", "K ", () => palette.ma1), line("d", "D ", () => palette.ma2), line("j", "J ", () => palette.ma3)],
  calc: pick(["k", "d", "j"]),
}

function dashed(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, color: string) {
  ctx.save()
  ctx.strokeStyle = color
  ctx.lineWidth = 1
  ctx.setLineDash([3, 3])
  ctx.beginPath()
  ctx.moveTo(x1, y1)
  ctx.lineTo(x2, y2)
  ctx.stroke()
  ctx.restore()
}

function label(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color: string, align: CanvasTextAlign = "right", backed = false) {
  ctx.save()
  const size = fs(palette, 12)
  ctx.font = `${size}px ${palette.fontMono}`
  if (backed) { // over the candles it is drawn on: the page's background behind it, so it stays readable
    const w = ctx.measureText(text).width + 6
    ctx.globalAlpha = 0.85
    ctx.fillStyle = palette.bg
    ctx.fillRect(align === "right" ? x - w + 3 : x - 3, y - size - 2, w, size + 3)
    ctx.globalAlpha = 1
  }
  ctx.fillStyle = color
  ctx.textAlign = align
  ctx.textBaseline = "bottom"
  ctx.fillText(text, x, y)
  ctx.restore()
}

const fmt = (v: number, p = 2) => v.toLocaleString("en-US", { minimumFractionDigits: p, maximumFractionDigits: p })

// The 看盘 preset's marks on the price pane.
const COURSE: IndicatorTemplate = {
  name: "WB_COURSE", shortName: "看盘", series: "price", precision: 2,
  figures: [],
  calc: pick(["limit_up", "limit_down", "high", "low", "pre_close", "up_limit", "amp60", "amp120"]),
  // three- and six-month amplitude (近三月 / 六月振幅) in its legend row, of the bar under the pointer
  createTooltipDataSource: ({ indicator, crosshair }) => {
    const res = indicator.result as Row[]
    const r = res[crosshair.dataIndex ?? res.length - 1] ?? res.at(-1)
    const amp = (a: unknown) => (typeof a === "number" ? `${(a * 100).toFixed(1)}%` : "—")
    return { name: "看盘", calcParamsText: "", features: [], legends: course && r ? [
      { title: "3月振幅 ", value: { text: amp(r.amp60), color: palette.muted } },
      { title: "6月振幅 ", value: { text: amp(r.amp120), color: palette.muted } }] : [] }
  },
  draw: ({ ctx, chart, indicator, bounding, xAxis, yAxis }) => {
    const res = indicator.result as Row[]
    const n = res.length
    if (!course || n === 0) return false
    const [from, to] = visible(chart)
    const top = bounding.top
    const bottom = bounding.top + bounding.height
    const prec = indicator.precision
    // every 40 bars (two months) a vertical dashed line, counted back from the latest bar
    for (let i = from; i < to; i++) {
      if ((n - 1 - i) % 40 === 0 && i !== n - 1) dashed(ctx, xAxis.convertToPixel(i), top, xAxis.convertToPixel(i), bottom, palette.border)
    }
    // limit-up / limit-down closes
    for (let i = from; i < to; i++) {
      const r = res[i]
      if (r.limit_up) {
        const x = xAxis.convertToPixel(i)
        const y = yAxis.convertToPixel(r.high as number) - 5
        ctx.fillStyle = palette.up
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 3, y - 5); ctx.lineTo(x + 3, y - 5); ctx.closePath(); ctx.fill()
      }
      if (r.limit_down) {
        const x = xAxis.convertToPixel(i)
        const y = yAxis.convertToPixel(r.low as number) + 5
        ctx.fillStyle = palette.down
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 3, y + 5); ctx.lineTo(x + 3, y + 5); ctx.closePath(); ctx.fill()
      }
    }
    // yesterday's close and today's limit-up price: is there 10% room before the left pressure?
    const last = res[n - 1]
    const right = bounding.left + bounding.width
    for (const [v, name, color] of [[last.pre_close, "昨收", palette.muted], [last.up_limit, "涨停", palette.up]] as const) {
      if (typeof v !== "number") continue
      const y = yAxis.convertToPixel(v)
      if (y < top || y > bottom) continue
      dashed(ctx, bounding.left, y, right, y, color)
      // the top-right corner holds the pressure note: a label that would run into it goes under its line
      const h = fs(palette, 12) + 4, corner = top + 2 * (fs(palette, 12) + 5) + 6   // the pressure note's corner
      label(ctx, `${name} ${fmt(v, prec)}`, right - 4, y - h - 2 < corner ? y + h : y - 2, color, "right", true)
    }
    return false
  },
}

// 年内向左: new half-year (120 bars) and one-year (250 bars) highs, and how far apart they are.
const YEAR_LEFT: IndicatorTemplate = {
  name: "WB_YEARLEFT", shortName: "年内向左", series: "normal", precision: 0, minValue: 0, maxValue: 1,
  figures: [],
  // draw-only: the 笑脸 (half-year breakout), 箭头 (one-year breakout) and their gap come from the backend
  calc: (list) => list.map((d) => {
    const b = d as WbBar
    return { h120: b.year_half === 0, h250: b.year_year === 0, gap: b.year_year === 0 ? b.year_gap : null }
  }),
  draw: ({ ctx, chart, indicator, bounding, xAxis }) => {
    const [from, to] = visible(chart)
    const res = indicator.result as Row[]
    const mid = bounding.top + bounding.height / 2
    for (let i = from; i < to; i++) {
      const r = res[i]
      const x = xAxis.convertToPixel(i)
      if (r?.h120) {
        ctx.fillStyle = palette.warning
        ctx.beginPath(); ctx.arc(x, mid + 6, 3, 0, Math.PI * 2); ctx.fill()
      }
      if (r?.h250) {
        ctx.fillStyle = palette.up
        ctx.beginPath(); ctx.moveTo(x, mid - 10); ctx.lineTo(x - 4, mid - 3); ctx.lineTo(x + 4, mid - 3); ctx.closePath(); ctx.fill()
        if (typeof r.gap === "number") label(ctx, `${r.gap}`, x, mid - 11, palette.muted, "center")
      }
    }
    return false
  },
}

// 大盘副图: the board index of the stock, aligned to the stock's bars by date.
const indexCache = new Map<string, Promise<Map<number, Row>>>()
function indexRows(indexId: string, period: Period): Promise<Map<number, Row>> {
  const key = `${indexId}|${period}`
  if (!indexCache.has(key)) {
    indexCache.set(key, endpoints.indexBars({ index_id: indexId, period, limit: 5000 })
      .then((r) => new Map(r.rows.map((b) => [bucketOf(b.t, period), { o: b.open, h: b.high, l: b.low, c: b.close }])))
      .catch(() => new Map()))
  }
  return indexCache.get(key)!
}
export const clearIndexCache = () => indexCache.clear()

const INDEX: IndicatorTemplate<Row, number, { indexId: string; period: Period }> = {
  name: "WB_INDEX", shortName: "大盘", series: "normal", precision: 2,
  figures: [
    { key: "c", title: "收 ", type: "line", styles: () => ({ color: "transparent" }) },
    { key: "h", title: "", type: "line", styles: () => ({ color: "transparent" }) },
    { key: "l", title: "", type: "line", styles: () => ({ color: "transparent" }) },
  ],
  calc: async (list, indicator) => {
    const ext = indicator.extendData
    if (!ext?.indexId) return list.map(() => ({}))
    const rows = await indexRows(ext.indexId, ext.period)
    return list.map((d) => rows.get(bucketOf(d.timestamp, ext.period)) ?? { o: null, h: null, l: null, c: null })
  },
  draw: ({ ctx, chart, indicator, xAxis, yAxis }) => {
    const [from, to] = visible(chart)
    const res = indicator.result as Row[]
    const w = Math.max(1, chart.getBarSpace().bar * 0.7)
    return fresh(ctx, () => {   // solid wicks, whatever dash the chart left
      for (let i = from; i < to; i++) {
        const r = res[i]
        if (typeof r?.c !== "number") continue
        const x = xAxis.convertToPixel(i)
        const [o, h, l, c] = [r.o, r.h, r.l, r.c].map((v) => yAxis.convertToPixel(v as number))
        const color = (r.c as number) >= (r.o as number) ? palette.up : palette.down
        ctx.strokeStyle = color
        ctx.fillStyle = color
        ctx.beginPath(); ctx.moveTo(x, h); ctx.lineTo(x, l); ctx.stroke()
        ctx.fillRect(x - w / 2, Math.min(o, c), w, Math.max(1, Math.abs(c - o)))
      }
      return false
    })
  },
}

let registered = false
export function registerAll() {
  if (registered) return
  registered = true
  registerLocale("wb", {
    time: "时间 ", open: "开 ", high: "高 ", low: "低 ", close: "收 ", volume: "量 ", change: "涨幅 ",
    turnover: "额 ", second: "秒", minute: "分", hour: "时", day: "日", week: "周", month: "月", year: "年",
  })
  for (const t of [MA, BOLL, VOL, MACD, KDJ, COURSE, YEAR_LEFT]) registerIndicator(t)
  registerIndicator(INDEX)
}
