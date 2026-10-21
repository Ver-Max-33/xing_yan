// Chart colours come from the same CSS tokens as the rest of the UI (design system 00).
import type { DeepPartial, Styles } from "klinecharts"

export interface Palette {
  up: string; down: string; structure: string; warning: string; annotate: string; error: string; falseBottom: string
  boxLong: string; boxMid: string; boxShort: string
  fg: string; muted: string; subtle: string; border: string; bg: string; popover: string
  ma1: string; ma2: string; ma3: string; ma4: string; fontSans: string; fontMono: string
  scale: number // the root font size / 16 (设置 → 外观 → 字号): the chart's text grows with the page's
}

/** A chart text size: `n` at the standard size (16px root), scaled with the 字号 setting. */
export const fs = (p: Palette, n: number) => Math.round(n * p.scale)

export function readPalette(el: Element = document.documentElement): Palette {
  const cs = getComputedStyle(el)
  const v = (n: string) => cs.getPropertyValue(n).trim()
  return {
    up: v("--up"), down: v("--down"), structure: v("--structure"), warning: v("--warning"),
    annotate: v("--annotate"), error: v("--error"), falseBottom: v("--false-bottom"),
    boxLong: v("--box-long"), boxMid: v("--box-mid"), boxShort: v("--box-short"), fg: v("--foreground"), muted: v("--muted-foreground"),
    subtle: v("--subtle-foreground"), border: v("--border"), bg: v("--background"), popover: v("--popover"),
    ma1: v("--ma-1"), ma2: v("--ma-2"), ma3: v("--ma-3"), ma4: v("--ma-4"),
    // the page's own fonts (设置 → 外观 → 字体), so the chart and the page read alike
    fontSans: v("--app-font") || "sans-serif", fontMono: v("--app-mono") || "monospace",
    scale: (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16) / 16,
  }
}

/** The price axes' width, fixed (review 10-13): klinecharts sizes them by the labels of the bars in view, and which bars are
 *  in view moves with that width — some charts never settled: every redraw (a 价位梯 row hovered, a layer switched)
 *  jumped them two bars sideways and rescaled the prices (CN:002016 1 分钟). Room for 99,999.99 in the axis' font. */
function axisWidth(p: Palette): number | "auto" {
  const c = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d")
  if (!c) return "auto"
  c.font = `${fs(p, 12)}px ${p.fontMono}`
  return Math.ceil(c.measureText("99,999.99").width) + 16
}

export function chartStyles(p: Palette): DeepPartial<Styles> {
  const text = { color: p.muted, family: p.fontMono, size: fs(p, 12) }
  return {
    grid: { show: true, horizontal: { color: p.border, style: "dashed", dashedValue: [2, 4] }, vertical: { show: false } },
    candle: {
      type: "candle_solid",
      bar: {
        compareRule: "current_open",
        upColor: p.up, downColor: p.down, noChangeColor: p.muted,
        upBorderColor: p.up, downBorderColor: p.down, noChangeBorderColor: p.muted,
        upWickColor: p.up, downWickColor: p.down, noChangeWickColor: p.muted,
      },
      priceMark: {
        high: { color: p.muted, textFamily: p.fontMono, textSize: fs(p, 11) },
        low: { color: p.muted, textFamily: p.fontMono, textSize: fs(p, 11) },
        // the page's own colour on the tag (white was 3.6:1 on the dark theme's red and green)
        last: { upColor: p.up, downColor: p.down, noChangeColor: p.muted, text: { color: p.bg, family: p.fontMono, size: fs(p, 12) } },
      },
      tooltip: {
        showRule: "always", showType: "standard", offsetLeft: 8, offsetTop: 6,
        title: { show: true, color: p.fg, family: p.fontSans, size: fs(p, 13) },
        legend: { color: p.muted, family: p.fontMono, size: fs(p, 12) },
      },
    },
    indicator: {
      tooltip: {
        showRule: "always", showType: "standard", offsetLeft: 8,
        title: { show: true, showName: true, showParams: false, color: p.muted, family: p.fontMono, size: fs(p, 12) },
        legend: { family: p.fontMono, size: fs(p, 12) },
      },
      lastValueMark: { show: false },
    },
    xAxis: { axisLine: { color: p.border }, tickLine: { color: p.border }, tickText: text },
    yAxis: { size: axisWidth(p), axisLine: { color: p.border }, tickLine: { color: p.border }, tickText: text },
    separator: { size: 1, color: p.border, fill: true, activeBackgroundColor: p.border },
    crosshair: {
      horizontal: { line: { color: p.subtle, style: "dashed" }, text: { family: p.fontMono, size: fs(p, 12), backgroundColor: p.popover, borderColor: p.border, color: p.fg } },
      vertical: { line: { color: p.subtle, style: "dashed" }, text: { family: p.fontMono, size: fs(p, 12), backgroundColor: p.popover, borderColor: p.border, color: p.fg } },
    },
    overlay: {
      line: { color: p.annotate, size: 1 },
      point: { color: p.annotate, borderColor: p.annotate, activeColor: p.annotate, activeBorderColor: p.annotate },
      text: { color: p.bg, backgroundColor: p.annotate, family: p.fontSans, size: fs(p, 12) },
      rect: { color: "transparent", borderColor: p.annotate, borderSize: 1 },
    },
  }
}
