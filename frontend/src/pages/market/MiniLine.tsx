// The two small lines of the 市场 page (redesign §2.2, §2.3): 上证 since the open over its close before, and the limit-ups
// by minute today against the day before. No text inside the drawing; hovering shows the minute's values in a floating
// readout of its own.
import { useState } from "react"
import { cn } from "@/lib/utils"
import { linePath, minuteLabel } from "./fmt"

export interface MiniSeries { name: string; points: [number, number][]; tone: string; step?: boolean }

export function MiniLine({ lines, base, format, className, testid }: {
  lines: MiniSeries[]; base?: number | null; format: (v: number) => string; className?: string; testid?: string
}) {
  const [at, setAt] = useState<number | null>(null)
  const values = lines.flatMap((l) => l.points.map((p) => p[1])).concat(base != null ? [base] : [])
  if (!lines.some((l) => l.points.length > 1)) return null
  const W = 240, H = 40, lo = Math.min(...values), hi = Math.max(...values)
  const x = (i: number) => i, y = (v: number) => H - 3 - ((v - lo) / (hi - lo || 1)) * (H - 6)
  const near = (l: MiniSeries, i: number) => l.points.reduce<[number, number] | null>((best, p) => (best == null || Math.abs(p[0] - i) < Math.abs(best[0] - i) ? p : best), null)
  return (
    <div className={cn("relative", className)} data-testid={testid}
      onMouseMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); setAt(Math.round(((e.clientX - r.left) / r.width) * 240)) }}
      onMouseLeave={() => setAt(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="block h-full w-full" aria-hidden>
        {base != null && <line x1={0} x2={W} y1={y(base)} y2={y(base)} className="stroke-border" strokeWidth={1} vectorEffect="non-scaling-stroke" />}
        {lines.map((l) => <path key={l.name} d={linePath(l.points, x, y, l.step)} className={cn("fill-none", l.tone)} strokeWidth={1.25}
          vectorEffect="non-scaling-stroke" />)}
      </svg>
      {at != null && (
        <div role="tooltip" className="pointer-events-none absolute bottom-full left-0 z-10 mb-1 rounded border border-border bg-popover px-1.5 py-0.5 text-[0.6875rem] whitespace-nowrap shadow-sm">
          <span className="num text-muted-foreground">{minuteLabel(Math.max(1, Math.min(240, at)))}</span>
          {lines.map((l) => { const p = near(l, at); return p ? <span key={l.name} className="num ml-2">{l.name} {format(p[1])}</span> : null })}
        </div>
      )}
    </div>
  )
}
