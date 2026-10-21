// 热力格 (redesign §2.4.1, §4): every 申万一级 (日股 33 業種) as a tile — its name, the chosen number (today's median change,
// or the last 5 / 20 bars against the market), its tag and its limit-ups — strongest top left, shaded by the largest move
// shown (four steps a side; 0.00% grey). The tiles' ink is the page's own on every step.
import type { HeatStep } from "@/lib/marketWords"
import { heatStep, industryName } from "@/lib/marketWords"
import { fmtPct } from "@/lib/format"
import type { Industry } from "@/lib/market"
import { cn } from "@/lib/utils"
import { scaleOf, type Window } from "./order"

const SHADE: Record<HeatStep, string> = {
  4: "bg-heat-up-4", 3: "bg-heat-up-3", 2: "bg-heat-up-2", 1: "bg-heat-up-1", 0: "bg-heat-flat",
  "-1": "bg-heat-down-1", "-2": "bg-heat-down-2", "-3": "bg-heat-down-3", "-4": "bg-heat-down-4",
} as Record<HeatStep, string>

export function HeatGrid({ rows, win, selected, onSelect, childrenOf, gridRef }: {
  rows: Industry[]; win: Window; selected: string | null; onSelect: (name: string) => void
  childrenOf: (name: string) => string[]; gridRef: React.RefObject<HTMLDivElement | null>
}) {
  const scale = scaleOf(rows, win)
  return (
    <div ref={gridRef} className="grid grid-cols-[repeat(auto-fill,minmax(7.5rem,1fr))] gap-1.5" data-testid="heat-grid" role="listbox" aria-label="行业">
      {rows.map((r) => {
        const v = r[win], step = heatStep(v, scale)
        const kids = childrenOf(r.name)
        return (
          <button key={r.name} type="button" role="option" aria-selected={r.name === selected} onClick={() => onSelect(r.name)}
            data-testid="heat-tile" data-industry={r.name} title={kids.length ? `${r.name}：${kids.join("、")}` : r.name}
            className={cn("flex min-h-[5.25rem] flex-col items-start gap-0.5 rounded-md px-2.5 py-2 text-left text-foreground outline-none",
              step == null ? "bg-accent" : SHADE[step], r.name === selected ? "ring-2 ring-foreground" : "focus-visible:ring-2 focus-visible:ring-ring")}>
            <span className="text-[0.8125rem] leading-tight font-medium">{industryName(r.name)}</span>
            <span className="num text-[1.0625rem] font-medium">{fmtPct(v)}</span>
            {(r.tag || (r.limit_up ?? 0) > 0) && (
              <span className="text-[0.6875rem] leading-tight">{[r.tag, (r.limit_up ?? 0) > 0 ? `涨停 ${r.limit_up}` : null].filter(Boolean).join(" · ")}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export function HeatLegend({ rows, win }: { rows: Industry[]; win: Window }) {
  const s = scaleOf(rows, win)
  const steps: HeatStep[] = [-4, -3, -2, -1, 0, 1, 2, 3, 4]
  return (
    <span className="flex items-center gap-1.5 text-[0.6875rem] text-subtle-foreground" data-testid="heat-legend">
      <span className="num">{fmtPct(-s)}</span>
      <span className="flex gap-px" aria-hidden>{steps.map((k) => <span key={k} className={cn("h-2.5 w-3 first:rounded-l-sm last:rounded-r-sm", SHADE[k])} />)}</span>
      <span className="num">{fmtPct(s)}</span>
    </span>
  )
}
