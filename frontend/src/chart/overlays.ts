// Drawing tools KLineChart lacks: a rectangle (the "样例框" of spec §8.2/§5.10) and a measuring box
// that shows the move and the bar count between two points (handy for box width and length).
import { type OverlayTemplate, registerOverlay } from "klinecharts"

const rectCorners = (a: { x: number; y: number }, b: { x: number; y: number }) =>
  [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }]

const RECT: OverlayTemplate = {
  name: "wbRect",
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) =>
    coordinates.length < 2 ? [] : [{ type: "polygon", attrs: { coordinates: rectCorners(coordinates[0], coordinates[1]) }, styles: { style: "stroke" } }],
}

const MEASURE: OverlayTemplate = {
  name: "wbMeasure",
  totalStep: 3,
  needDefaultPointFigure: true,
  createPointFigures: ({ coordinates, overlay }) => {
    if (coordinates.length < 2) return []
    const [p0, p1] = overlay.points
    const pct = p0?.value && p1?.value !== undefined ? (p1.value / p0.value - 1) * 100 : null
    // both ends counted, as a box's "N 根" is: measured over a box, the two agree
    const bars = p0?.dataIndex !== undefined && p1?.dataIndex !== undefined ? Math.abs(p1.dataIndex - p0.dataIndex) + 1 : null
    const text = `${pct === null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`} · ${bars ?? "—"} 根`
    const [a, b] = coordinates
    return [
      { type: "polygon", attrs: { coordinates: rectCorners(a, b) }, styles: { style: "stroke_fill", color: "rgba(59,130,246,0.10)", borderColor: "rgba(59,130,246,0.6)" } },
      { type: "text", attrs: { x: (a.x + b.x) / 2, y: Math.min(a.y, b.y) - 4, text, align: "center", baseline: "bottom" } },
    ]
  },
}

let registered = false
export function registerOverlays() {
  if (registered) return
  registered = true
  registerOverlay(RECT)
  registerOverlay(MEASURE)
}

/** Toolbar tools -> overlay names (built-in ones plus ours). */
export const TOOLS = {
  segment: "segment",       // 趋势线
  ray: "rayLine",           // 射线
  hline: "horizontalStraightLine", // 水平线 (price alerts later use these)
  rect: "wbRect",           // 矩形 / 样例框
  text: "simpleAnnotation", // 文字标注
  measure: "wbMeasure",     // 测量
  fib: "fibonacciLine",     // 斐波那契回撤 (spec §8.2)
} as const
export type Tool = keyof typeof TOOLS
