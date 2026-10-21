// 强弱榜 (redesign §2.4.2): 申万一级 (all 31) or 二级 (all of them; the small ones — three members or fewer, no rank — at
// the end), by today's change, the last 5 or 20 bars against the market, or limit-ups — a list that scrolls in its own
// frame, as tall as the detail beside it, its headings kept at the top. Each row: its rank by today (under any order),
// name (a 二级 with its 一级), today as a bar from the middle and a number, the two windows, limit-ups, the share up, the
// first leader; a row chosen elsewhere is scrolled into view in the list. Too narrow for every column, the bar goes
// first (it only draws the number beside it), then 领涨, then 上涨 — the detail beside shows both for the chosen industry.
import { Fragment, useEffect, useLayoutEffect, useState } from "react"
import { changeClass, fmtPct } from "@/lib/format"
import type { Industry } from "@/lib/market"
import { industryName } from "@/lib/marketWords"
import { cn } from "@/lib/utils"
import { type SortKey, sortRows } from "./order"

const GAP = 0.5, PAD = 0.75   // rem: gap-x-2 between the columns, px-1.5 on both sides
const DROP = ["bar", "leader", "up"]

/** The list's own width in px, kept as it changes (measured before the first paint). */
function useWidth(ref: React.RefObject<HTMLDivElement | null>): number {
  const [w, setW] = useState(Infinity)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setW(el.clientWidth)
    const o = new ResizeObserver(([e]) => setW(e.contentRect.width))
    o.observe(el)
    return () => o.disconnect()
  }, [ref])
  return w
}

export function Ranking({ rows, sort, level, selected, onSelect, limits, dayWord, childrenOf, listRef }: {
  rows: Industry[]; sort: SortKey; level: "l1" | "l2"; selected: string | null; onSelect: (name: string) => void
  limits: boolean; dayWord: string; childrenOf: (name: string) => string[]; listRef: React.RefObject<HTMLDivElement | null>
}) {
  const { ranked, small } = sortRows(rows, sort)
  useEffect(() => {   // a row chosen elsewhere (the 一级 detail's 二级, the K-line chip) may sit far down: the list scrolls, the page does not
    const box = listRef.current, el = box?.querySelector<HTMLElement>(`[data-industry="${CSS.escape(selected ?? "")}"]`)
    if (!box || !el) return
    const head = (box.firstElementChild as HTMLElement | null)?.offsetHeight ?? 0
    const seen = el.offsetTop - head >= box.scrollTop && el.offsetTop + el.offsetHeight <= box.scrollTop + box.clientHeight
    if (!seen) box.scrollTop = el.offsetTop - (box.clientHeight + head - el.offsetHeight) / 2   // into the middle, its neighbours around it
  }, [selected, level, sort, listRef])
  const max = Math.max(0.0001, ...rows.map((r) => Math.abs(r.chg ?? 0)))
  // the columns: key, track, least width (rem), heading
  const all: [string, string, number, string][] = [["n", "2.5rem", 2.5, "名次"], ["name", "minmax(6.5rem,1.3fr)", 6.5, "行业"],
    ["bar", "minmax(2.25rem,1.4fr)", 2.25, ""], ["chg", "4.25rem", 4.25, dayWord], ["rel5", "4.25rem", 4.25, "近 5 日"],
    ["rel20", "4.25rem", 4.25, "近 20 日"], ...(limits ? [["limit", "3rem", 3, "涨停"] as [string, string, number, string]] : []),
    ["up", "3.25rem", 3.25, "上涨"], ["leader", "minmax(4.5rem,1fr)", 4.5, "领涨"]]
  const rem = useWidth(listRef) / (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16)
  let cols = all
  for (const k of DROP) if (cols.reduce((s, c) => s + c[2] + GAP, PAD - GAP) > rem) cols = cols.filter((c) => c[0] !== k)
  const grid = { gridTemplateColumns: cols.map((c) => c[1]).join(" ") }
  const row = (r: Industry) => {
    const share = r.chg == null ? 0 : (Math.abs(r.chg) / max) * 50
    const cell: Record<string, React.ReactNode> = {
      n: <span className="num text-[0.75rem] text-subtle-foreground">{r.rank ?? "—"}</span>,   // today's rank under any order (§8)
      name: <span className="flex min-w-0 items-baseline gap-1.5" title={level === "l1" && childrenOf(r.name).length ? `${r.name}：${childrenOf(r.name).join("、")}` : r.name}>
        <span className="truncate">{industryName(r.name)}</span>
        {level === "l2" && r.parent && <span className="shrink-0 text-[0.6875rem] text-subtle-foreground">{r.parent}</span>}
      </span>,
      bar: <span className="relative h-2" aria-hidden>
        <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
        {r.chg != null && <span className={cn("absolute inset-y-0", r.chg >= 0 ? "left-1/2 bg-up" : "right-1/2 bg-down")} style={{ width: `${share}%` }} />}
      </span>,
      chg: <span className={cn("num text-right", changeClass(r.chg))}>{fmtPct(r.chg)}</span>,
      rel5: <span className={cn("num text-right", changeClass(r.rel5))}>{fmtPct(r.rel5, 1)}</span>,
      rel20: <span className={cn("num text-right", changeClass(r.rel20))}>{fmtPct(r.rel20, 1)}</span>,
      limit: <span className="num text-right text-muted-foreground">{r.limit_up ? r.limit_up : ""}</span>,
      up: <span className="num text-right text-muted-foreground">{r.up_ratio == null ? "—" : `${Math.round(r.up_ratio * 100)}%`}</span>,
      leader: <span className="truncate text-[0.75rem] text-muted-foreground">{r.leaders[0]?.name ?? ""}</span>,
    }
    return (
      <button key={r.name} type="button" role="option" aria-selected={r.name === selected} onClick={() => onSelect(r.name)}
        data-testid="ranking-row" data-industry={r.name} style={grid}
        className={cn("grid w-full items-center gap-x-2 rounded px-1.5 py-1.5 text-left text-[0.8125rem]",
          r.name === selected ? "bg-accent font-medium" : "hover:bg-accent/60")}>
        {cols.map(([k]) => <Fragment key={k}>{cell[k]}</Fragment>)}
      </button>
    )
  }
  return (
    <div ref={listRef} className="absolute inset-0 flex flex-col overflow-y-auto" data-testid="ranking" role="listbox" aria-label="行业">
      <div className="sticky top-0 z-10 grid shrink-0 gap-x-2 bg-background px-1.5 pb-1.5 text-[0.6875rem] text-subtle-foreground" style={grid}>
        {cols.map(([k, , , head]) => <span key={k} className={["n", "name", "bar", "leader"].includes(k) ? undefined : "text-right"}
          title={k === "n" ? `按${dayWord === "今天" ? "今天" : ` ${dayWord} `}涨幅的名次（换了排序也不变；成分 3 只及以下、没有成交的不排名）` : undefined}>{head}</span>)}
      </div>
      {[...ranked, ...small].map((r) => row(r))}
    </div>
  )
}
