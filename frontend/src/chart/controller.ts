// ChartAdapter (spec §8.1): owns one KLineChart instance. Data comes page by page from the
// backend, indicators are draw-only, drawings are persisted as (date, raw price) anchors.
import {
  type Chart, type DataLoaderGetBarsParams, dispose, init, type KLineData, type Overlay, type OverlayEvent, type SymbolInfo,
} from "klinecharts"
import { type Basis, type Drawing, type DrawingPoint, endpoints, isMinute, type Market, type Period } from "@/lib/api"
import { indexName } from "@/lib/format"
import { toast } from "sonner"
import { DAY_MS, isoDate, liveStep, minuteTicks, reanchor, type Scales, settled, toDisplayPoint, toKLine, upTo, type WbBar } from "./data"
import { clearIndexCache, registerAll, setCourseMode, setCrossMode, setIndicatorPalette } from "./indicators"
import { fetchSeries, type LibExt, type LibMeta, libName, registerLibrary, setLibraryPalette } from "./library"
import { registerOverlays, type Tool, TOOLS } from "./overlays"

const ALERTABLE = new Set<string>([TOOLS.hline, TOOLS.segment, TOOLS.ray]) // lines a price is watched against (spec §7.2)
import { CHIP_BARS, chips } from "./cyq"
import { type ChipView, EMPTY_CHIPS, registerStructureIndicators } from "./paint"
import { asOf, barLength, type CompareView, emphasis, EMPTY_COMPARE, EMPTY_RULE, EMPTY_STRUCT, type LevelTag, levelsWith, levelTags, type Live, markOf,
  type RuleView, type StructView } from "./structures"
import { chartStyles, fs, type Palette, readPalette } from "./theme"

/** A drawing as the backend saves it (what 撤销 puts back). */
type Saved = Pick<Drawing, "id" | "listing_id" | "kind" | "points" | "styles" | "alert">

export interface Panes {
  ma: boolean; boll: boolean; course: boolean; chips: boolean
  vol: boolean; macd: boolean; kdj: boolean; index: boolean; yearLeft: boolean
}
export const DEFAULT_PANES: Panes = { ma: true, boll: false, course: true, chips: false, vol: true, macd: true, kdj: false, index: false, yearLeft: false }

/** An indicator of the 指标库 asked for on the chart. */
export interface LibEntry { id: string; version: number; name: string }

/** `live`: 盘中当天那根 — end with today's bar still forming while its market's day is open (spec §8.5), drawn as any candle. */
export interface View { listingId: string; market: Market; period: Period; basis: Basis; name: string; indexId: string; live?: boolean }
export interface LoadInfo { adjusted: boolean; snapshotId: string | null; bars: number }

const sleep = (ms: number) => new Promise((res) => setTimeout(res, ms))
const INIT_BARS = 600
const PAGE_BARS = 600
const SUB_PANES: [keyof Panes, string, number][] = [
  ["vol", "WB_VOL", 90], ["macd", "WB_MACD", 90], ["kdj", "WB_KDJ", 80], ["index", "WB_INDEX", 90], ["yearLeft", "WB_YEARLEFT", 36],
]
/** Layers of a day's bars: the 看盘 marks (two months every 40 bars, 3/6-month amplitude, 昨收 / 涨停) and the 年线 pane
 *  would read weeks, months or minutes as days; the 大盘 pane has no minute bars (an index's weeks and months it has) */
const DAILY_ONLY = new Set<keyof Panes>(["course", "yearLeft"])
const NOT_MINUTE = new Set<keyof Panes>(["index"])
const PERIOD_LABEL: Record<Period, string> = { day: "日", week: "周", month: "月", m1: "1分", m5: "5分", m15: "15分", m30: "30分", m60: "60分" }
/** KLineChart's period of ours: m5 is { minute, 5 } (its x axis then reads the times) */
const chartPeriod = (p: Period) => (isMinute(p) ? { type: "minute" as const, span: Number(p.slice(1)) } : { type: p, span: 1 })
const BASIS_LABEL: Record<Basis, string> = { qfq: "前复权", hfq: "后复权", raw: "不复权" }

type WbSymbol = SymbolInfo & { basis: Basis; market: Market }

declare global {
  interface Window {
    wbDebug?: {
      view: () => View | null
      ready: () => string | null
      bars: () => WbBar[]
      indicator: (name: string) => unknown[] | null
      overlays: () => { id: string; name: string; points: unknown[]; paneId: string; alert: boolean }[]
      structures: () => StructView                    // what the structure layer draws now (after replay and layers)
      rule: () => RuleView                            // the rule's signals and stage bands on screen
      replay: () => string | null                     // the replay day, null for the latest bar
      inits: () => number                             // initial data loads so far (one per stock / period / basis change)
      painted: () => { struct: unknown; rule: unknown; compare: unknown } // what the chart's own indicators hold (not the controller's copy)
      pixelOf: (date: string) => { x: number; y: number } | null // a loaded bar's close in page coordinates
      visible: () => [string | null, string | null]   // the first and last day on screen
      room: () => number                              // px kept right of the last bar (当前形态's prices sit there)
    }
  }
}

export class ChartController {
  readonly chart: Chart
  private view: View | null = null
  private panes: Panes = { ...DEFAULT_PANES }
  private library: LibEntry[] = []                      // the 指标库 indicators asked for, in order
  private libMeta = new Map<string, LibMeta>()          // what each (id@version) draws and where, once known
  private seq = 0
  private drawingsVisible = true
  private readyFor: string | null = null // listing whose bars and saved drawings are on screen
  private scales: Scales = [] // daily scale change points of the loaded listing and basis
  private anchors = new Map<string, { stored: DrawingPoint[]; shown: { timestamp: number; value: number }[] }>()
  private replacing = false // removing overlays to redraw them: not the user deleting them
  private pending: string | null = null // a drawing started and not finished: the next clicks place it
  private older = false // the server has bars before the first one loaded
  private olderLoaded: (() => void) | null = null // revealDay waiting for an older page
  private alerts = new Map<string, boolean>() // a line's 价格提醒 (spec §7.2): drawn dashed in the warning colour
  private palette: Palette
  private structView: StructView = EMPTY_STRUCT   // everything known at the latest bar
  private ruleView: RuleView = EMPTY_RULE
  private compareView: Omit<CompareView, "palette"> | null = null
  private replay: string | null = null           // replay day: bars, structures and signals stop there
  private chipDay: string | null = null          // 筹码分布's day: the crosshair's (null: the last bar loaded)
  private keepBarSpace: number | null = null     // a replay step reloads the bars: the zoom you had stays
  private snapshotId: string | null = null
  private initLoads = 0
  private feed: ((bar: KLineData) => void) | null = null // KLineChart's way in for the latest bar
  onLoad?: (info: LoadInfo) => void
  onError?: (e: unknown) => void
  onLibraryError?: (name: string, message: string) => void   // an indicator of the library that cannot be drawn here
  onCrosshair?: (bar: WbBar | null) => void
  onBarClick?: (bar: WbBar) => void
  onRectDrawn?: (id: string, corners: { date: string; price: number }[]) => void // a 样例框 candidate
  onLineMenu?: (m: { id: string; alert: boolean; x: number; y: number }) => void // a right-click on a line: its menu
  onUserRemoved?: (gone: Saved[]) => void // drawings you removed (one, or all of a stock's): to offer 撤销
  onDrawingDone?: () => void // the drawing started is placed: the tool is left
  private removing: Saved[] | null = null // gathers a clear-all's drawings into one 撤销
  private deleting = new Map<string, Promise<unknown>>() // deletes on their way: a 撤销 waits for them

  private el: HTMLElement

  constructor(el: HTMLElement) {
    this.el = el
    registerAll()
    registerOverlays()
    registerStructureIndicators()
    const palette = readPalette()
    this.palette = palette
    setIndicatorPalette(palette)
    setLibraryPalette(palette)
    this.chart = init(el, { locale: "wb", timezone: "UTC", styles: chartStyles(palette) })!
    // KLineChart's own element: tabindex 1 (ahead of everything in the Tab order) and an inline outline: none — in the
    // order where it sits, named, and its focus seen (index.css [data-chart-root])
    const root = el.firstElementChild
    if (root instanceof HTMLElement) {
      root.tabIndex = 0
      root.style.outline = ""
      root.dataset.chartRoot = ""
      root.setAttribute("aria-label", "K 线图：← → 移动，滚轮缩放")
    }
    const formatDate = this.chart.getFormatter().formatDate // a day's bar and longer need no time; a minute bar keeps its own
    this.chart.setFormatter({ formatDate: (p) => formatDate(this.minute() ? p : { ...p, template: p.template.replace(" HH:mm", "") }) })
    this.chart.overrideXAxis({ createTicks: ({ defaultTicks }) => (this.minute() ? minuteTicks : upTo)(defaultTicks, this.bars().at(-1)?.timestamp) })
    this.chart.setDataLoader({
      getBars: (p) => this.getBars(p),
      subscribeBar: ({ callback }) => { this.feed = callback },
      unsubscribeBar: () => { this.feed = null },
    })
    // the chart moved off the latest bars (dragged, or a day brought into view) stays where it was put: no room kept then
    this.chart.subscribeAction("onScroll", () => { this.pinned = Math.abs(this.chart.getOffsetRightDistance() - this.room) <= 1 })
    this.chart.subscribeAction("onZoom", () => this.keepRoom())
    this.chart.subscribeAction("onCrosshairChange", (data) => {
      // the action passes the pointer position, not the bar: look the bar up from it
      const x = (data as { x?: number } | undefined)?.x
      const bar = x === undefined ? null : this.barAt(x)
      this.onCrosshair?.(bar)
      if (this.panes.chips && (bar?.date ?? null) !== this.chipDay) {
        this.chipDay = bar?.date ?? null
        this.redrawChips()
      }
    })
    // a click anywhere picks the bar under the pointer (hitting a thin candle exactly is fiddly) — but a
    // drag that pans the chart ends in a click too: only a click that stayed put picks a bar
    let down: { x: number; y: number } | null = null
    el.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY } }, true)
    el.addEventListener("click", (e) => {
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return
      const r = el.getBoundingClientRect()
      const bar = this.barAt(e.clientX - r.left)
      if (bar) this.onBarClick?.(bar)
    })
    this.applyPanes(this.panes)
    for (const name of ["WB_STRUCT", "WB_RULE", "WB_COMPARE", "WB_CHIPS"]) { // draw-only layers: no legend row of their own
      this.chart.createIndicator({ name, paneId: "candle_pane", styles: { tooltip: { showRule: "none" } } }, true)
    }
    this.redrawStructures()
    // Read-only window into what is actually drawn, for end-to-end checks that the chart shows
    // exactly the backend's values (spec §13 P1 acceptance).
    window.wbDebug = {
      view: () => this.view,
      ready: () => this.readyFor,
      bars: () => this.bars(),
      indicator: (name: string) => this.chart.getIndicators({ name })[0]?.result ?? null,
      overlays: () => this.chart.getOverlays({ groupId: "user" }).map((o) => ({ id: o.id, name: o.name, points: o.points, paneId: o.paneId,
        alert: this.alerts.get(o.id) ?? false })),
      structures: () => this.shownStructures(),
      rule: () => this.shownRule(),
      replay: () => this.replay,
      inits: () => this.initLoads,
      painted: () => ({
        struct: this.chart.getIndicators({ name: "WB_STRUCT" }).map((i) => (i.extendData as Live<StructView> | undefined)?.view()),
        rule: this.chart.getIndicators({ name: "WB_RULE" }).map((i) => (i.extendData as Live<RuleView> | undefined)?.view()),
        compare: this.chart.getIndicators({ name: "WB_COMPARE" }).map((i) => (i.extendData as Live<CompareView> | undefined)?.view()),
        chips: this.chart.getIndicators({ name: "WB_CHIPS" }).map((i) => (i.extendData as Live<ChipView> | undefined)?.view()),
      }),
      pixelOf: (date: string) => this.pixelOf(date),
      visible: () => {   // the first and last day on screen
        const r = this.chart.getVisibleRange(), b = this.bars()
        return [b[Math.max(0, r.from)]?.date ?? null, b[Math.min(b.length, r.to) - 1]?.date ?? null]
      },
      room: () => this.chart.getOffsetRightDistance(),
    }
  }

  // ---- view --------------------------------------------------------------------------------
  open(view: View) {
    const prev = this.view
    this.view = view
    const precision = view.market === "CN" ? 2 : 1
    for (const name of ["WB_MA", "WB_BOLL", "WB_COURSE"]) this.chart.overrideIndicator({ name, precision })
    this.chart.overrideIndicator({ name: "WB_INDEX", shortName: `大盘 ${indexName(view.indexId)}`, extendData: { indexId: view.indexId, period: view.period } })
    this.chart.setStyles({ candle: { tooltip: { title: { template: `${view.name} · ${PERIOD_LABEL[view.period]} · ${BASIS_LABEL[view.basis]}` } } } })
    const symbol: WbSymbol = { ticker: view.listingId, pricePrecision: precision, volumePrecision: 0, basis: view.basis, market: view.market }
    // each of setPeriod / setSymbol reloads the data: call only what changed (one load per change)
    const symbolChanged = !prev || prev.listingId !== view.listingId || prev.basis !== view.basis
    if (!prev || prev.period !== view.period) this.chart.setPeriod(chartPeriod(view.period))
    if (prev && prev.period !== view.period) this.applyPanes(this.panes)   // DAILY_ONLY, NOT_MINUTE
    if (symbolChanged) this.chart.setSymbol(symbol)
    else if (prev && !!prev.live !== !!view.live && prev.period === view.period && !this.replay) { // the forming bar on or off
      this.keepBarSpace = this.chart.getBarSpace().bar
      this.chart.resetData()
    }
    this.structView = { ...this.structView, digits: precision }
    this.redrawStructures()
  }

  private async getBars({ type, timestamp, symbol, period, callback }: DataLoaderGetBarsParams) {
    const s = symbol as WbSymbol
    const p = (period.type === "minute" ? `m${period.span}` : period.type) as Period
    const q = { listing_id: s.ticker, period: p, basis: s.basis }
    try {
      if (type === "init") {
        const my = ++this.seq
        this.initLoads++
        this.readyFor = null // not ready until these bars and the drawings are on screen (a tool picked meanwhile is reset by onLoad)
        // a minute chart comes whole (its last days, never an older page)
        const r = await endpoints.bars({ ...q, end: this.replay ?? undefined, limit: isMinute(p) ? 5000 : INIT_BARS,
          live: (this.view?.live && !this.replay) || undefined })
        if (my !== this.seq) return // a newer symbol/period/basis was requested meanwhile
        if (r.snapshot_id !== this.snapshotId) { // a new snapshot: the index pane's cached bars are stale
          if (this.snapshotId !== null) clearIndexCache()
          this.snapshotId = r.snapshot_id
        }
        const bars = toKLine(r.rows)
        this.scales = r.scales
        this.older = r.more
        callback(bars, { forward: r.more, backward: false })
        this.redrawStructures()   // 当前形态's prices read the bars' last close, and their room changes the fit
        if (this.keepBarSpace !== null) this.chart.setBarSpace(this.keepBarSpace)
        else this.fitBars(this.minute() ? Math.min(120, bars.length) : 120) // ~120 bars (six months) per screen
        this.pinned = true   // a load shows the latest bars
        this.keepRoom()
        this.keepBarSpace = null
        this.redrawChips()
        this.onLoad?.({ adjusted: r.adjusted, snapshotId: r.snapshot_id, bars: bars.length })
        void this.loadDrawings(s.ticker)
      } else if (type === "forward" && timestamp !== null) {
        const my = this.seq
        const r = await endpoints.bars({ ...q, end: isoDate(timestamp - DAY_MS), limit: PAGE_BARS })
        if (my !== this.seq) return // an older page of the previous stock or period: never splice it in
        this.older = r.more
        callback(toKLine(r.rows), { forward: r.more, backward: false })
        this.redrawChips()   // a window that reached the left edge now reaches further
        this.olderLoaded?.()
      } else {
        callback([], { forward: false, backward: false })
      }
    } catch (e) {
      callback([], false)
      this.onError?.(e)
    }
  }

  private fitBars(n: number) {
    const width = this.el.clientWidth - 70 - this.room   // the room kept right of the last bar
    if (width > 0) this.chart.setBarSpace(Math.max(2, Math.min(24, width / n)))
  }

  // ---- 指标库 (design 2026-10-10) ------------------------------------------------------------------
  private libExt(x: LibEntry): LibExt {
    const v = this.view!
    return { id: x.id, version: x.version, listingId: v.listingId, period: v.period, basis: v.basis, live: !!v.live && !this.replay }
  }

  /** The library's indicators on the chart, in this order: each over the candles or in a pane of its own below the
   *  chart's own, following the stock, period and basis shown; one that cannot be drawn here is said (onLibraryError)
   *  and asked again on the next call (another period may have its fields). */
  async setLibrary(list: LibEntry[]) {
    this.library = list
    const want = new Map(this.minute() ? [] : list.map((x) => [libName(x.id), x]))   // 指标库: a day's bars and longer
    for (const ind of this.chart.getIndicators()) {
      const x = want.get(ind.name)
      if (ind.name.startsWith("WB_LIB_") && (!x || (ind.extendData as LibExt | undefined)?.version !== x.version)) {
        this.chart.removeIndicator({ name: ind.name })
      }
    }
    if (!this.view || this.minute()) return
    for (const [order, x] of list.entries()) {
      const name = libName(x.id)
      if (this.chart.getIndicators({ name }).length) {   // there: it follows what is shown
        this.chart.overrideIndicator({ name, extendData: this.libExt(x) })
        continue
      }
      const key = `${x.id}@${x.version}`
      let meta = this.libMeta.get(key)
      if (!meta) {
        const s = await fetchSeries(this.libExt(x), undefined)
        if (this.library !== list || !this.view) return   // asked again meanwhile: that call carries on
        if (s.error || !s.items || !s.pane) {
          this.onLibraryError?.(x.name, s.error ?? "算不出来")
          continue
        }
        meta = { id: x.id, version: x.version, name: x.name, pane: s.pane, items: s.items, levels: s.levels_ref ?? [] }
        this.libMeta.set(key, meta)
      }
      registerLibrary(meta)
      if (meta.pane === "main") {
        this.chart.createIndicator({ name, paneId: "candle_pane", extendData: this.libExt(x) }, true)
      } else {
        const paneId = `pane_${name}`
        this.chart.createIndicator({ name, paneId, extendData: this.libExt(x) })
        this.chart.setPaneOptions({ id: paneId, height: 80, minHeight: 30, order: SUB_PANES.length + 1 + order })
      }
    }
  }

  // ---- panes -------------------------------------------------------------------------------
  applyPanes(next: Panes) {
    this.panes = { ...next }
    const on = (k: keyof Panes) => next[k] && !(DAILY_ONLY.has(k) && this.view && !this.daily()) && !(NOT_MINUTE.has(k) && this.minute())
    setCourseMode(next.course)
    const has = (name: string) => this.chart.getIndicators({ name }).length > 0
    const main: [keyof Panes, string][] = [["ma", "WB_MA"], ["boll", "WB_BOLL"], ["course", "WB_COURSE"]]
    for (const [key, name] of main) {
      if (on(key) && !has(name)) this.chart.createIndicator({ name, paneId: "candle_pane" }, true)
      if (!on(key) && has(name)) this.chart.removeIndicator({ name })
    }
    SUB_PANES.forEach(([key, name, height], order) => {
      if (on(key) && !has(name)) {
        const paneId = `pane_${name}`
        this.chart.createIndicator(name === "WB_INDEX" && this.view
          ? { name, paneId, shortName: `大盘 ${indexName(this.view.indexId)}`, extendData: { indexId: this.view.indexId, period: this.view.period } }
          : { name, paneId })
        this.chart.setPaneOptions({ id: paneId, height, minHeight: 30, order: order + 1 })
      }
      if (!on(key) && has(name)) this.chart.removeIndicator({ name })
    })
    this.redrawChips()
    this.chart.resize() // repaint with the new 看盘 mode
  }

  refreshTheme() {
    const palette = readPalette()
    this.palette = palette
    setIndicatorPalette(palette)
    setLibraryPalette(palette)
    this.chart.setStyles(chartStyles(palette))
    this.redrawStructures()
    this.redrawChips()
  }

  /** MACD golden-cross marks by the zero axis (default) or by price against MA60 (spec §8.6). */
  setCrossMode(mode: "zero" | "ma60") {
    setCrossMode(mode)
    this.chart.resize()
  }

  // ---- structures and the rule (spec §8.2, §6.3) ------------------------------------------------
  /** The rule's signals, 筹码 and the left pressure are a day's; structures are every period's own (design
   *  2026-10-12-structures-every-period). */
  private daily() {
    return this.view?.period === "day"
  }

  /** A minute chart (10-12) shows the market only: no drawings, structures, 指标库, 大盘 pane or replay on it. */
  private minute() {
    return !!this.view && isMinute(this.view.period)
  }

  private shownStructures(): StructView {
    const v = this.structView
    const labels = this.replay ? v.labels.filter((l) => l.cutoff <= this.replay!) : v.labels
    const structures = asOf(v.structures, this.replay, v.dates)
    // 当前形态 (or your focus, or the judgement) in full, the rest faint; 当前形态's prices at the right edge, how far from
    // the last price on screen (the replay day's close; while the day trades, the bar still forming)
    const { strong, now } = emphasis(structures, v.focus ?? null, v.highlight)
    const close = this.bars().at(-1)?.close
    const prices = close ? levelTags(structures, now ? strong : emphasis(structures, null, []).strong, close, v.digits,
      v.layers.pressure ? v.pressure : null) : []
    this.nowLevels = prices   // the room right of the last bar is 当前形态's, a focus or not: the bars do not jump aside
    // a structure picked: its prices against now would mislead; the left pressure is now's whatever is picked. A line of
    // 当前形态 the pointer rests on in the 价位梯 keeps now's prices, itself among them
    const mark = close && v.rung ? markOf(v.rung, close, v.digits) : null
    const levels = !close ? undefined : mark ? levelsWith(prices, mark) : now ? prices : prices.filter((t) => t.tone === "pressure" || t.tone === "short")
    return { ...v, structures, labels, palette: this.palette, strong: [...strong], now, levels, mark,
      barLen: (from, to, beforeTo) => barLength(settled(this.loadedBars()), from, to, beforeTo) }   // as the side panel reads it
  }

  private shownRule(): RuleView {
    if (!this.daily()) return { ...EMPTY_RULE, palette: this.palette }
    const t = this.replay, on = this.structView.layers
    return {
      signals: on.signals ? (t ? this.ruleView.signals.filter((s) => s.signal_date <= t) : this.ruleView.signals) : [],
      bands: on.signals ? this.ruleView.bands : [],
      danger: on.danger ? (t ? this.ruleView.danger.filter((d) => d.date <= t) : this.ruleView.danger) : [],   // 危险提醒: a layer of its own
      plan: t ? [] : this.ruleView.plan,   // 价格行为分析's plan: made on the latest day, not shown on a replayed one
      palette: this.palette,
    }
  }

  /** 筹码分布 of the crosshair's day, else of the last bar (the replay day in a replay): daily A 股 bars, in a 复权 basis. */
  private shownChips(): ChipView {
    const v = this.view
    if (!this.panes.chips || !v || !this.daily()) return EMPTY_CHIPS
    const base: ChipView = { ...EMPTY_CHIPS, digits: v.market === "CN" ? 2 : 1, palette: this.palette }
    if (v.market !== "CN") return { ...base, note: "筹码分布：日股没有换手率，画不了" }
    if (v.basis === "raw") return { ...base, note: "筹码分布要复权后的价格：请切到前复权" }
    const bars = this.bars()
    if (!bars.length) return base
    const at = this.chipDay ? bars.findIndex((b) => b.date === this.chipDay) : -1
    const i = at >= 0 ? at : bars.length - 1
    const c = chips(bars, i)
    return { ...base, chips: c, date: bars[i].date, partial: !!c && c.bars < CHIP_BARS && this.older,
             note: c ? null : "筹码分布：这一段没有换手率" }
  }

  private redrawChips() {
    const c = this.shownChips()
    this.chart.overrideIndicator({ name: "WB_CHIPS", extendData: { view: () => c } })
  }

  private measure: CanvasRenderingContext2D | null = null
  private room = 80   // px kept right of the last bar (klinecharts' own 80)
  private pinned = true   // the last bar sits at that room's edge (not scrolled off it)
  private nowLevels: LevelTag[] = []   // 当前形态's prices, drawn or not

  /** 当前形态's prices sit right of the last bar, in room kept there for the widest of them — not on the latest candles —
   *  at most a quarter of a narrow chart (there they may cover the last few). */
  private fitLevels() {
    this.measure ??= document.createElement("canvas").getContext("2d")
    let room = 80
    if (this.nowLevels.length && this.measure) {
      this.measure.font = `${fs(this.palette, 12)}px ${this.palette.fontSans}`
      const widest = Math.max(...this.nowLevels.map((t) => Math.ceil(this.measure!.measureText(`${t.text} ↑`).width) + 20))
      room = Math.max(room, Math.min(widest, (this.el.clientWidth - 70) / 4))
    }
    this.room = room
    this.keepRoom()
  }

  /** klinecharts keeps the room in bars: a zoom (a new bar space) changes its width — put back what the prices need, while
   *  the latest bars are what is on screen (setting it scrolls back to them; its getter reads where the chart is scrolled). */
  private keepRoom() {
    if (this.pinned && Math.abs(this.chart.getOffsetRightDistance() - this.room) > 1) this.chart.setOffsetRightDistance(this.room)
  }

  /** Back to 当前形态: the latest bars, with their prices' room. */
  toNow() {
    this.pinned = true
    this.keepRoom()
  }

  /** Each view goes in as a getter (see Live): computed once per change, not on every frame. */
  private redrawStructures() {
    const [s, r] = [this.shownStructures(), this.shownRule()]
    this.fitLevels()
    const c = this.compareView ? { ...this.compareView, palette: this.palette } : EMPTY_COMPARE
    this.chart.overrideIndicator({ name: "WB_STRUCT", extendData: { view: () => s } })
    this.chart.overrideIndicator({ name: "WB_RULE", extendData: { view: () => r } })
    this.chart.overrideIndicator({ name: "WB_COMPARE", extendData: { view: () => c } })
  }

  setStructures(view: Partial<StructView>) {
    this.structView = { ...this.structView, ...view }
    this.redrawStructures()
  }

  /** 对比: another listing's or index's closes by bar timestamp (null to stop comparing). */
  setCompare(view: Omit<CompareView, "palette"> | null) {
    this.compareView = view
    this.redrawStructures()
  }

  setRule(view: Partial<RuleView>) {
    this.ruleView = { ...this.ruleView, ...view }
    this.redrawStructures()
  }

  /** Replay: show the chart as of day `t` (null: back to the latest bar) — bars after t are not
   *  loaded, structures and signals are the ones known by t. */
  setReplay(t: string | null) {
    if (t === this.replay) return
    this.replay = t
    this.redrawStructures()
    if (this.view) {
      this.keepBarSpace = this.chart.getBarSpace().bar
      this.chart.resetData()
    }
  }

  /** A new minute: the forming bar as it is now, put in place — the chart stays where you scrolled it. A week's or a
   *  month's bar that changes its date, or a day that has gone, reloads instead. */
  async liveTick() {
    const v = this.view
    if (!v?.live || this.replay || !this.feed) return
    const my = this.seq
    try {
      const r = await endpoints.bars({ listing_id: v.listingId, period: v.period, basis: v.basis, limit: 2, live: true })
      if (my !== this.seq || this.view !== v || !this.feed) return // reloaded meanwhile: that load has it
      const rows = toKLine(r.rows), end = this.bars().at(-1)
      const step = end ? liveStep(end.timestamp, rows) : null
      if (step === "reload") {
        this.keepBarSpace = this.chart.getBarSpace().bar
        this.chart.resetData()
      } else if (step) {
        for (const bar of step) this.feed(bar) // the chart's last bar replaced in place, then the next one added
      }
    } catch (e) {
      this.onError?.(e)
    }
  }

  // ---- drawings ----------------------------------------------------------------------------
  private bars(): WbBar[] {
    return this.chart.getDataList() as WbBar[]
  }

  /** The bar at x pixels from the chart's left edge (null outside the loaded bars). */
  private barAt(x: number): WbBar | null {
    const p = this.chart.convertFromPixel([{ x }], { paneId: "candle_pane" }) as Partial<{ dataIndex: number }>[] | Partial<{ dataIndex: number }>
    const i = (Array.isArray(p) ? p[0] : p)?.dataIndex
    const bars = this.bars()
    return i !== undefined && i >= 0 && i < bars.length ? bars[i] : null
  }

  /** Where a loaded bar's close is on the page (null when the bar is not loaded). */
  pixelOf(date: string): { x: number; y: number } | null {
    const bars = this.bars()
    const i = bars.findIndex((b) => b.date === date)
    if (i < 0) return null
    const p = this.chart.convertToPixel({ dataIndex: i, value: bars[i].close }, { paneId: "candle_pane", absolute: true }) as { x?: number; y?: number }
    const r = this.el.getBoundingClientRect()
    return p.x === undefined || p.y === undefined ? null : { x: r.left + p.x, y: r.top + p.y }
  }

  /** The bars on the chart (display basis), e.g. to offer a sample box's touches. */
  loadedBars(): WbBar[] {
    return this.bars()
  }

  /** A structure picked in the list ([from, to]; to null: running): brought into view, centred, unless all of it is on
   *  screen already. */
  async reveal(listingId: string, from: string, to: string | null) {
    const r = this.chart.getVisibleRange(), shown = this.bars()
    const [first, last] = [shown[Math.max(0, r.from)]?.date, shown[Math.min(shown.length, r.to) - 1]?.date]
    if (first && last && from >= first && (to ?? shown.at(-1)?.date ?? from) <= last) return
    if (!shown.length || shown[0].date > from) await this.revealDay(listingId, from)   // older than the bars loaded: load them
    const b = this.bars(), i = b.findIndex((x) => x.date >= from), j = to ? b.findIndex((x) => x.date >= to) : b.length - 1
    await this.revealDay(listingId, i >= 0 && j >= i ? b[Math.floor((i + j) / 2)].date : from)   // its middle
  }

  /** Bring `day` into view, in the middle (单股调试: a day opened from the gallery, 判定日志, 漏检排查 or 版本对比 could sit
   *  months off-screen): once `listingId`'s bars are on the chart, loading older pages first when it is before them. */
  async revealDay(listingId: string, day: string) {
    for (let t = 0; t < 100 && this.readyFor !== listingId; t++) await sleep(100)   // its bars still loading
    for (let pages = 0; pages < 8 && this.readyFor === listingId; pages++) {   // 8 pages: 4,800 bars
      const bars = this.bars()
      if (!bars.length) return
      const r = this.chart.getVisibleRange()
      const shown = Math.max(1, r.to - r.from)
      if (bars[0].date <= day || !this.older) {
        const i = bars.findIndex((b) => b.date >= day)
        const j = i < 0 ? bars.length : i + Math.floor(shown / 2)   // scrollToDataIndex puts a bar at the right edge
        if (j >= bars.length - 1) this.chart.scrollToRealTime()
        else this.chart.scrollToDataIndex(j)
        return
      }
      const loaded = new Promise<void>((res) => { this.olderLoaded = res })
      // the first bar at the left edge: the chart asks for the page before once its range starts at bar 0 (to `shown`, its
      // rounding stops two bars short, and no page came after the first)
      this.chart.scrollToDataIndex(Math.min(bars.length - 1, Math.max(0, shown - 4)))
      await Promise.race([loaded, sleep(5000)])
      this.olderLoaded = null
    }
  }

  /** Remove one drawing (it is deleted on the backend like any drawing the user removes). */
  removeDrawing(id: string) {
    this.chart.removeOverlay({ id })
  }

  private overlayHandlers(listingId: string) {
    return {
      groupId: "user",
      onDrawEnd: ({ overlay }: { overlay: Overlay }) => {
        if (overlay.id === this.pending) { this.pending = null; this.onDrawingDone?.() }   // the rail goes back to 光标
        if (overlay.paneId !== "candle_pane") { // KLineChart starts a drawing on whichever pane is clicked first; drawings are prices
          this.replacing = true // never saved: nothing to delete on the backend
          try { this.chart.removeOverlay({ id: overlay.id }) } finally { this.replacing = false }
          toast("标注只能画在主图上")
          return
        }
        void this.persist(listingId, overlay)
        if (overlay.name === TOOLS.rect) {
          this.onRectDrawn?.(overlay.id, overlay.points.filter((p) => p.timestamp !== undefined && p.value !== undefined)
            .map((p) => ({ date: isoDate(p.timestamp!), price: p.value! })))
        }
      },
      onPressedMoveEnd: ({ overlay }: { overlay: Overlay }) => void this.persist(listingId, overlay),
      // a line's right-click opens its menu (价格提醒 on / off, delete); any other drawing's deletes it, as before
      onRightClick: (e: OverlayEvent<unknown>) => {
        if (!ALERTABLE.has(e.overlay.name) || !this.onLineMenu) return
        e.preventDefault?.()
        this.onLineMenu({ id: e.overlay.id, alert: this.alerts.get(e.overlay.id) ?? false, x: e.pageX ?? 0, y: e.pageY ?? 0 })
      },
      // KLineChart also fires onRemoved when we clear overlays to redraw them for another stock,
      // period or basis; only a removal by the user deletes the saved drawing
      onRemoved: ({ overlay }: { overlay: Overlay }) => {
        if (this.replacing) return
        const saved = this.savedOf(listingId, overlay)
        this.anchors.delete(overlay.id)
        this.deleting.set(overlay.id, endpoints.deleteDrawing(overlay.id).catch((e) => this.onError?.(e)))
        if (!saved) return
        if (this.removing) this.removing.push(saved)
        else this.onUserRemoved?.([saved])
      },
    }
  }

  private async loadDrawings(listingId: string) {
    this.readyFor = null
    this.pending = null
    this.replacing = true
    try {
      this.chart.removeOverlay({ groupId: "user" })
    } finally {
      this.replacing = false
    }
    this.anchors.clear()
    const drawings = this.minute() ? [] : await endpoints.drawings(listingId).catch((e) => { this.onError?.(e); return [] })
    if (this.view?.listingId !== listingId) return
    const bars = this.bars()
    for (const d of drawings) {
      const shown = d.points.map((p) => toDisplayPoint(bars, p, this.scales))
      this.anchors.set(d.id, { stored: d.points, shown })
      this.alerts.set(d.id, d.alert)
      this.chart.createOverlay({
        name: d.kind, id: d.id, visible: this.drawingsVisible,
        points: shown, styles: this.lineStyle(d.alert),
        extendData: (d.styles as { text?: string }).text,
        ...this.overlayHandlers(listingId),
      })
    }
    this.readyFor = listingId
  }

  private async persist(listingId: string, overlay: Overlay) {
    const prev = this.anchors.get(overlay.id)
    const points = reanchor(prev?.stored ?? [], prev?.shown ?? [], overlay.points, this.scales)
    if (points.length === 0) return
    this.anchors.set(overlay.id, { stored: points, shown: overlay.points.map((p) => ({ timestamp: p.timestamp ?? 0, value: p.value ?? 0 })) })
    await endpoints.saveDrawing({
      id: overlay.id, listing_id: listingId, kind: overlay.name, points,
      styles: overlay.extendData ? { text: overlay.extendData } : {}, alert: this.alerts.get(overlay.id) ?? false,
    }).catch((e) => this.onError?.(e))
  }

  /** `alert`: a line that alerts when the price crosses it (the 提醒线 tool). */
  startDrawing(tool: Tool, text?: string, alert = false) {
    if (!this.view || this.minute()) return
    this.cancelDrawing()   // one tool at a time
    const id = crypto.randomUUID().replaceAll("-", "")
    this.pending = id
    this.alerts.set(id, alert)
    this.chart.createOverlay({
      name: TOOLS[tool], id, extendData: text, styles: this.lineStyle(alert),
      ...this.overlayHandlers(this.view.listingId),
    })
  }

  /** Leave the drawing tool (Esc, 光标, another tool): a drawing not finished is dropped, never saved — the next click
   *  no longer places it (with 提醒线 that was a saved price alert). */
  cancelDrawing() {
    const id = this.pending
    if (!id) return
    this.pending = null
    this.alerts.delete(id)
    this.replacing = true   // never saved: nothing to delete on the backend
    try { this.chart.removeOverlay({ id }) } finally { this.replacing = false }
  }

  /** Turn a line's 价格提醒 on or off (saved with the line). */
  setLineAlert(id: string, on: boolean) {
    const o = this.chart.getOverlays({ id })[0]
    if (!o || !this.view) return
    this.alerts.set(id, on)
    this.chart.overrideOverlay({ id, styles: this.lineStyle(on) })
    void this.persist(this.view.listingId, o)
  }

  private lineStyle(alert: boolean) {
    const p = this.palette
    return alert ? { line: { color: p.warning, size: 1, style: "dashed" as const, dashedValue: [6, 3] } }
      : { line: { color: p.annotate, size: 1, style: "solid" as const } }
  }

  setDrawingsVisible(visible: boolean) {
    this.drawingsVisible = visible
    for (const o of this.chart.getOverlays({ groupId: "user" })) this.chart.overrideOverlay({ id: o.id, visible })
  }

  clearDrawings() {
    this.removing = []
    try {
      this.chart.removeOverlay({ groupId: "user" }) // onRemoved deletes each one on the backend
    } finally {
      const gone = this.removing
      this.removing = null
      if (gone.length) this.onUserRemoved?.(gone)
    }
  }

  /** A drawing as it is saved, from what the chart holds of it. */
  private savedOf(listingId: string, overlay: Overlay): Saved | null {
    const stored = this.anchors.get(overlay.id)?.stored
    if (!stored?.length) return null
    return { id: overlay.id, listing_id: listingId, kind: overlay.name, points: stored,
      styles: overlay.extendData ? { text: overlay.extendData } : {}, alert: this.alerts.get(overlay.id) ?? false }
  }

  /** 撤销: drawings removed a moment ago, saved again (after their deletes went through) and drawn again. */
  async restoreDrawings(gone: Saved[]) {
    await Promise.all(gone.map((d) => this.deleting.get(d.id)))
    await Promise.all(gone.map((d) => endpoints.saveDrawing(d)))
    if (this.view && gone.some((d) => d.listing_id === this.view!.listingId)) await this.loadDrawings(this.view.listingId)
  }

  resize() {
    this.chart.resize()
  }

  dispose() {
    clearIndexCache()
    dispose(this.el)
  }
}
