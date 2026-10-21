// 连板天梯 (designs 2026-10-12-market-live §5.6, -market-page-redesign §2.5): from the highest board down to the first —
// names from three boards up, each marked 封 (red) or 炸 (amber); 二板 and 首板 as counts; empty levels one after another
// fold into one line ("9–7 板 没有"), so where the ladder breaks is seen at once. 碰过涨停的 lists every stock that touched (sealed and broken).
import { useState } from "react"
import { useNavigate } from "react-router"
import { changeClass, fmtPct } from "@/lib/format"
import type { Board } from "@/lib/market"
import { cn } from "@/lib/utils"
import { ladderRows } from "./fmt"
import { Head } from "./parts"

const MARK = (sealed: boolean) => <span className={cn("text-[0.6875rem]", sealed ? "text-up" : "text-warning")}>{sealed ? "封" : "炸"}</span>
const LEVEL = "num min-w-14 shrink-0 text-[0.75rem] whitespace-nowrap text-muted-foreground"

export function Ladder({ b }: { b: Board }) {
  const navigate = useNavigate()
  const [all, setAll] = useState(false)
  const lim = b.limits
  return (
    <section data-testid="market-ladder">
      <Head title="连板天梯" extra={lim.height ? <span className="text-[0.75rem] text-muted-foreground">最高 {lim.height} 板</span> : null}
        right={b.limit_list.length ? <button type="button" className="hover:text-foreground" onClick={() => setAll(!all)}>{all ? "收起" : `碰过涨停的 ${b.limit_list.length} 只 ›`}</button> : null} />
      {!b.ladder.length && <p className="text-[0.8125rem] text-subtle-foreground">{lim.known ? (lim.height === null && lim.limit_up ? "日线没到昨天，连板数未知" : "还没有股票碰到涨停") : "涨跌停价未知"}</p>}
      {!all && (
        <div className="flex flex-col gap-2">
          {ladderRows(b.ladder).map((row) => row.kind === "gap" ? (
            <div key={`gap-${row.from}`} className="flex items-baseline gap-3" data-testid="ladder-gap">
              <span className={LEVEL}>{row.from === row.to ? `${row.from} 板` : `${row.from}–${row.to} 板`}</span>
              <span className="text-[0.75rem] text-subtle-foreground">没有</span>
            </div>
          ) : (
            <div key={row.level.level} className="flex items-baseline gap-3" data-testid="ladder-row">
              <span className={LEVEL}>{row.level.level === 1 ? "首板" : `${row.level.level} 板`}</span>
              {row.level.level >= 3 ? (
                <span className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                  {row.level.names.map((n) => (
                    <button key={n.listing_id} type="button" className="flex items-baseline gap-1 text-[0.8125rem] hover:underline" onClick={() => navigate(`/k/${n.listing_id}`)}>
                      {n.name ?? n.listing_id}{MARK(n.sealed)}
                    </button>
                  ))}
                  {row.level.more > 0 && <span className="text-[0.75rem] text-subtle-foreground">等 {row.level.sealed + row.level.broken} 只</span>}
                </span>
              ) : <span className="num text-[0.75rem] text-muted-foreground">封 {row.level.sealed} · 炸 {row.level.broken}</span>}
            </div>
          ))}
        </div>
      )}
      {all && (
        <div className="flex max-h-[16rem] flex-col overflow-y-auto" data-testid="market-limit-list">
          {b.limit_list.map((s) => (
            <button key={s.listing_id} type="button" onClick={() => navigate(`/k/${s.listing_id}`)}
              className="flex items-baseline gap-3 rounded-sm px-1 py-1 text-left text-[0.8125rem] hover:bg-accent">
              <span className={LEVEL}>{s.level == null ? "—" : s.level === 1 ? "首板" : `${s.level} 板`}</span>
              <span className="min-w-0 flex-1 truncate">{s.name ?? s.listing_id}</span>{MARK(s.sealed)}
              <span className={cn("num w-16 text-right text-[0.75rem]", changeClass(s.chg))}>{fmtPct(s.chg)}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}
