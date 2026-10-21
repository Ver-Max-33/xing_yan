// 行业 (redesign §2.4): the heat grid of 申万一级 (日股 33 業種) or the ranking (一级 / 二级), the chosen industry's detail
// beside. Remembered per market on this computer: the view, the heat grid's number, the ranking's level and order, the
// chosen 一级 and 二级 (the heat grid and the ranking's 一级 share one). Keys: arrows move in the grid, ↑↓ in the
// ranking; the page's address (?level=&industry=, from the K-line chip and 选股) selects one in the ranking.
import { useEffect, useMemo, useRef } from "react"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { BoardResponse, Industry } from "@/lib/market"
import { dayWord, industryName } from "@/lib/marketWords"
import { usePersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import { HeatGrid, HeatLegend } from "./HeatGrid"
import { IndustryDetail } from "./IndustryDetail"
import { byWindow, type SortKey, sortRows, type Window } from "./order"
import { Ranking } from "./Ranking"

export interface Wanted { level: "l1" | "l2"; name: string }
const byChange = (rows: Industry[]) => [...rows].sort((a, b) => (a.chg == null ? 1 : 0) - (b.chg == null ? 1 : 0) || (b.chg ?? 0) - (a.chg ?? 0))
const ITEM = "h-6 px-2 text-[0.75rem]"

export function Industries({ r, narrow, want, onWanted }: { r: BoardResponse; narrow: boolean; want: Wanted | null; onWanted: () => void }) {
  const m = r.market, b = r.board!, cn_ = m === "CN"
  const [view, setView] = usePersisted<"grid" | "rank">(`market.ind.${m}.view`, "grid")
  const [win, setWin] = usePersisted<Window>(`market.ind.${m}.win`, "chg")
  const [lvl, setLvl] = usePersisted<"l1" | "l2">(`market.ind.${m}.level`, "l1")
  const [sort, setSort] = usePersisted<SortKey>(`market.ind.${m}.sort`, "chg")
  const [sel1, setSel1] = usePersisted<string | null>(`market.ind.${m}.sel.l1`, null)
  const [sel2, setSel2] = usePersisted<string | null>(`market.ind.${m}.sel.l2`, null)
  const gridRef = useRef<HTMLDivElement>(null), listRef = useRef<HTMLDivElement>(null)
  const rows1 = b.industries.l1, rows2 = b.industries.l2, hasL2 = cn_ && rows2.length > 0
  const grid = view === "grid"
  const level: "l1" | "l2" = !grid && lvl === "l2" && hasL2 ? "l2" : "l1"
  const by: SortKey = sort === "limit_up" && !b.limits.known ? "chg" : sort
  const word = dayWord(r)
  const kids = useMemo(() => {
    const k = new Map<string, Industry[]>()
    for (const x of rows2) if (x.parent) k.set(x.parent, [...(k.get(x.parent) ?? []), x])
    return k
  }, [rows2])
  const childrenOf = (n: string) => (kids.get(n) ?? []).map((x) => industryName(x.name))
  useEffect(() => {   // ?level=&industry= from the K-line chip or 选股: that industry in the ranking
    if (!want) return
    setView("rank")
    if (want.level === "l2" && hasL2) { setLvl("l2"); setSel2(want.name) } else { setLvl("l1"); setSel1(want.name) }
    onWanted()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per address
  }, [want])
  if (!rows1.length) return <section data-testid="market-industries"><h2 className="pb-2.5 text-[0.8125rem] font-semibold">行业</h2>
    <p className="text-[0.8125rem] text-subtle-foreground">这个数据档没有行业数据</p></section>
  const rows = level === "l2" ? rows2 : rows1
  const ordered = grid ? byWindow(rows1, win) : (() => { const s = sortRows(rows, by); return [...s.ranked, ...s.small] })()
  const sel = level === "l2" ? sel2 : sel1
  const current = ordered.find((x) => x.name === sel) ?? ordered[0]
  const select = (name: string) => (level === "l2" ? setSel2(name) : setSel1(name))
  const pick = (lv: "l1" | "l2", name: string) => {
    if (lv === "l2") { setView("rank"); setLvl("l2"); setSel2(name) } else setSel1(name)
  }
  const focus = (name: string) => requestAnimationFrame(() => (grid ? gridRef : listRef).current?.querySelector<HTMLElement>(`[data-industry="${CSS.escape(name)}"]`)?.focus())
  const onKey = (e: React.KeyboardEvent) => {
    const k = ordered.findIndex((x) => x.name === current?.name)
    let step = 0
    if (grid) {
      const tiles = [...(gridRef.current?.querySelectorAll<HTMLElement>("[data-testid=heat-tile]") ?? [])]
      const cols = Math.max(1, tiles.filter((t) => t.offsetTop === tiles[0]?.offsetTop).length)
      step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key] ?? 0
    } else step = { ArrowDown: 1, ArrowUp: -1 }[e.key] ?? 0
    if (!step) return
    e.preventDefault()
    const next = ordered[Math.max(0, Math.min(ordered.length - 1, k + step))]
    if (next) { select(next.name); focus(next.name) }
  }
  const related = !current || !cn_ ? [] : byChange(level === "l1" ? kids.get(current.name) ?? [] : kids.get(current.parent ?? "") ?? [])
  const of = (lv: "l1" | "l2") => b.industries[lv].filter((x) => x.rank != null).length
  const ranked = rows.filter((x) => !x.small).length, small = rows.length - ranked
  const scheme = cn_ ? (level === "l2" ? "申万二级" : "申万一级") : "东证 33 業種"
  const shade = win === "chg" ? `按${word === "今天" ? "今天" : ` ${word} `}最大的涨跌幅分档` : `按${win === "rel5" ? "近 5 日" : "近 20 日"}比大盘最大的差距分档`
  const count = (n: number) => (cn_ ? ` ${n} 个` : "")   // 日股's scheme names its 33 already
  const caption = grid ? `${scheme}${count(rows1.length)} · 颜色深浅${shade} · 点一格看详情`
    : `${scheme}${count(ranked)}${small ? `（成分 3 只及以下的 ${small} 个不排名）` : ""} · 近 5 日、近 20 日是比大盘`
  return (
    <section id="sec-industries" className="scroll-mt-4" data-testid="market-industries">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pb-3">
        <h2 className="text-[0.8125rem] font-semibold">行业</h2>
        <ToggleGroup type="single" size="sm" variant="outline" value={view} onValueChange={(v) => v && setView(v as "grid" | "rank")} aria-label="行业的看法" data-testid="industry-view">
          <ToggleGroupItem value="grid" className={ITEM}>热力格</ToggleGroupItem>
          <ToggleGroupItem value="rank" className={ITEM}>强弱榜</ToggleGroupItem>
        </ToggleGroup>
        {grid ? (
          <ToggleGroup type="single" size="sm" variant="outline" value={win} onValueChange={(v) => v && setWin(v as Window)} aria-label="热力格的数" data-testid="industry-window">
            <ToggleGroupItem value="chg" className={ITEM}>{word}</ToggleGroupItem>
            <ToggleGroupItem value="rel5" className={ITEM}>近 5 日</ToggleGroupItem>
            <ToggleGroupItem value="rel20" className={ITEM}>近 20 日</ToggleGroupItem>
          </ToggleGroup>
        ) : <>
          {cn_ && (
            <ToggleGroup type="single" size="sm" variant="outline" value={level} onValueChange={(v) => v && setLvl(v as "l1" | "l2")} aria-label="行业级别" data-testid="industry-level">
              <ToggleGroupItem value="l1" className={ITEM}>一级</ToggleGroupItem>
              <ToggleGroupItem value="l2" className={ITEM} disabled={!hasL2} title={hasL2 ? undefined : "二级行业下次更新数据后才有"}>二级</ToggleGroupItem>
            </ToggleGroup>
          )}
          <select value={by} onChange={(e) => setSort(e.target.value as SortKey)} aria-label="排序" data-testid="industry-sort"
            className="h-6 rounded border border-border bg-background px-1 text-[0.75rem] text-muted-foreground">
            <option value="chg">按{word === "今天" ? "今天" : ` ${word} `}涨幅排</option><option value="rel5">按近 5 日排</option><option value="rel20">按近 20 日排</option>
            {b.limits.known && <option value="limit_up">按涨停数排</option>}
          </select>
        </>}
        <span className="min-w-0 text-[0.75rem] text-subtle-foreground" data-testid="industry-caption">{caption}</span>
        {grid && <span className="ml-auto"><HeatLegend rows={rows1} win={win} /></span>}
      </div>
      {/* the ranking scrolls in a frame as tall as the detail (it sits in the column absolutely: the detail sets the height) */}
      <div className={cn("flex gap-5", grid ? "items-start" : "items-stretch")} onKeyDown={onKey}>
        <div className={cn("min-w-0 flex-1", !grid && "relative min-h-[20rem]")}>
          {grid ? <HeatGrid rows={ordered} win={win} selected={current?.name ?? null} onSelect={select} childrenOf={childrenOf} gridRef={gridRef} />
            : <Ranking rows={rows} sort={by} level={level} selected={current?.name ?? null} onSelect={select} limits={b.limits.known}
                dayWord={word} childrenOf={childrenOf} listRef={listRef} />}
        </div>
        <div className={cn("shrink-0", narrow ? "w-[17.5rem]" : "w-[20rem]")}>
          {current && <IndustryDetail r={r} ind={current} level={level} related={related} of={of("l2")} dayWord={word} onPick={pick} />}
        </div>
      </div>
    </section>
  )
}
