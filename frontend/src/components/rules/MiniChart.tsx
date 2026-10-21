// A hit's thumbnail (spec §6.3 样例图集): the same chart and the same painters as the big chart, small — the
// bars up to a few weeks after the signal, only the structures the hit stands on (a 金品: its big and small box, named),
// the close MAs the rule reads, the signal day marked.
// With no ids (a clarification card's option, §6.2) it draws that rule's own signals in the window and the
// structures they stand on, so the options of a card look as different as they are.
// Built when it scrolls into view, so a long gallery stays light.
import { useQuery } from "@tanstack/react-query"
import { dispose, init } from "klinecharts"
import { useEffect, useRef, useState } from "react"
import { toKLine } from "@/chart/data"
import { registerAll, setIndicatorPalette } from "@/chart/indicators"
import { registerStructureIndicators } from "@/chart/paint"
import { DEFAULT_LAYERS, EMPTY_STRUCT, type RuleView, type StructView } from "@/chart/structures"
import { chartStyles, readPalette } from "@/chart/theme"
import { endpoints } from "@/lib/api"
import { boxesOf, ruleEndpoints, type RuleRef } from "@/lib/rules"
import { cn } from "@/lib/utils"

const plusDays = (d: string, n: number) => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10)
const BARS = 140
// the MA lines the bars carry, computed on the whole history (a rule's other periods are not drawn)
// ponytail: only 5 / 10 / 20 / 60; another period (MA(C, 30)) needs the bars endpoint to carry it
const MA_KEY: Record<number, string> = { 5: "ma5", 10: "ma10", 20: "ma20", 60: "ma60" }

interface Props { ruleRef: RuleRef; listingId: string; day: string; ids: string[]; digits: number; className?: string }

export function MiniChart({ ruleRef, listingId, day, ids, digits, className }: Props) {
  const el = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const node = el.current
    if (!node) return
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { setVisible(true); io.disconnect() }
    }, { rootMargin: "300px" })
    io.observe(node)
    return () => io.disconnect()
  }, [])
  const refKey = JSON.stringify(ruleRef)
  const previewQ = useQuery({ queryKey: ["preview", refKey, listingId, "qfq"], queryFn: () => ruleEndpoints.preview(ruleRef, listingId, "qfq"),
    enabled: visible, staleTime: Infinity, retry: false })
  const barsQ = useQuery({ queryKey: ["mini-bars", listingId, day], enabled: visible, staleTime: Infinity,
    queryFn: () => endpoints.bars({ listing_id: listingId, period: "day", basis: "qfq", end: plusDays(day, 30), limit: BARS }) })
  const idKey = ids.join(",")
  useEffect(() => {
    const node = el.current
    if (!node || !barsQ.data || !previewQ.data) return
    registerAll() // the "wb" locale, in case no big chart has been opened yet
    registerStructureIndicators()
    const palette = readPalette()
    const chart = init(node, { locale: "wb", timezone: "UTC", styles: chartStyles(palette) })
    if (!chart) return
    const root = node.firstElementChild   // a thumbnail takes no keys: not a Tab stop (KLineChart makes it tabindex 1)
    if (root instanceof HTMLElement) root.tabIndex = -1
    chart.setStyles({
      grid: { show: false }, xAxis: { show: false }, yAxis: { show: false }, crosshair: { show: false },
      candle: { tooltip: { showRule: "none" }, priceMark: { show: false } }, separator: { size: 0 },
      indicator: { tooltip: { showRule: "none" } },   // the MAs drawn are captioned below, without the ones it does not read
    })
    const mas = new Set((previewQ.data.rule.mas ?? []).map((n) => MA_KEY[n]).filter(Boolean))
    const bars = toKLine(barsQ.data.rows).map((b) => {   // only the MAs the rule reads
      const x: Record<string, unknown> = { ...b }
      for (const k of Object.values(MA_KEY)) if (!mas.has(k)) delete x[k]
      return x as typeof b
    })
    chart.setDataLoader({ getBars: ({ callback }) => callback(bars, { forward: false, backward: false }) })
    chart.setSymbol({ ticker: listingId, pricePrecision: digits, volumePrecision: 0 })
    chart.setPeriod({ type: "day", span: 1 })
    chart.setBarSpace(Math.max(1, (node.clientWidth - 6) / Math.max(bars.length, 1)))
    const all = idKey === ""
    const first = bars[0]?.timestamp ?? 0
    const shown = all ? previewQ.data.signals.filter((x) => Date.parse(`${x.signal_date}T00:00:00Z`) >= first && x.signal_date <= plusDays(day, 30)) : []
    const keep = new Set((all ? shown.flatMap((x) => Object.entries(x).filter(([k, v]) => k !== "listing_id" && k !== "signal_date" && typeof v === "string").map(([, v]) => v as string))
      : idKey.split(",")).flatMap(boxesOf))
    const s = previewQ.data.structures
    const struct: StructView = {
      ...EMPTY_STRUCT, palette, digits,
      structures: { ...EMPTY_STRUCT.structures, boxes: s.boxes.filter((b) => keep.has(b.id)), slant: s.slant.filter((x) => keep.has(x.id)),
        gold: s.gold.filter((g) => keep.has(g.small_box_id) && keep.has(g.big_box_id)) },   // tags its small box (下品)
      layers: { ...DEFAULT_LAYERS, boxShort: true, protos: true, slant: true, pressure: false, labels: false },
    }
    const rule: RuleView = all ? { signals: shown, bands: [], danger: [], plan: [], palette }
      : { signals: [], bands: [{ stage: "", kind: "signal", start: day, end: day }], danger: [], plan: [], palette }
    const hidden = { tooltip: { showRule: "none" as const } }
    chart.createIndicator({ name: "WB_STRUCT", paneId: "candle_pane", styles: hidden, extendData: { view: () => struct } }, true)
    chart.createIndicator({ name: "WB_RULE", paneId: "candle_pane", styles: hidden, extendData: { view: () => rule } }, true)
    if (mas.size) {
      setIndicatorPalette(palette)   // in case no big chart has been opened yet
      chart.createIndicator({ name: "WB_MA", paneId: "candle_pane", styles: hidden }, true)
    }
    return () => dispose(node)
  }, [barsQ.data, previewQ.data, idKey, day, listingId, digits])
  const rows = barsQ.data?.rows
  const maShown = (previewQ.data?.rule.mas ?? []).filter((n) => n in MA_KEY)
  const quiet = idKey === "" && !!rows?.length && !!previewQ.data
    && !previewQ.data.signals.some((x) => x.signal_date >= rows[0].date && x.signal_date <= plusDays(day, 30))
  return (
    <div className={cn("relative h-[9.375rem] w-full", className)}>
      <div ref={el} className="h-full w-full" data-testid="mini-chart" />
      {maShown.length > 0 && <div className="num pointer-events-none absolute top-1 left-2 text-[0.75rem] text-subtle-foreground">{maShown.map((n) => `MA${n}`).join(" · ")}</div>}
      {quiet && <div className="absolute inset-x-0 bottom-1 text-center text-[0.75rem] text-subtle-foreground">这里没有信号</div>}
      {visible && (previewQ.error || barsQ.error) && <div className="absolute inset-0 flex items-center justify-center text-[0.75rem] text-error">画不出来</div>}
    </div>
  )
}
