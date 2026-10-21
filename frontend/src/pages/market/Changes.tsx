// 盘面变化 (designs 2026-10-12-market-live §4, -market-page-redesign §2.6): the day's changes, the newest first — the latest
// five, the earlier ones folded — only on this page, never pushed. The leading industry's changes are by 申万一级.
import { useState } from "react"
import type { BoardResponse } from "@/lib/market"
import { cn } from "@/lib/utils"
import { DOT, eventKey, hm } from "./fmt"
import { Head } from "./parts"

const SHOWN = 5

export function Changes({ r }: { r: BoardResponse }) {
  const [all, setAll] = useState(false)
  const list = [...r.events].reverse()
  const live = r.mode === "live" || r.mode === "auction_result"
  const head = live ? `今天 ${list.length} 条` : `${(r.as_of ?? "").slice(5)} 共 ${list.length} 条`
  const shown = all ? list : list.slice(0, SHOWN)
  return (
    <section data-testid="market-changes">
      <Head title="盘面变化" extra={<span className="text-[0.75rem] text-muted-foreground" data-testid="changes-head">{head} · 只记在这一页</span>} />
      <div className="flex flex-col gap-3">
        {list.length === 0 && <p className="text-[0.8125rem] text-subtle-foreground">{live ? "还没有值得记的变化" : "这一天没有记下变化"}</p>}
        {shown.map((e) => (
          <div key={eventKey(e)} className="flex gap-2.5" data-testid="change-row">
            <span className="num w-10 shrink-0 text-[0.75rem] text-muted-foreground">{hm(e.at)}</span>
            <span className={cn("mt-[0.3rem] size-[0.4375rem] shrink-0 rounded-full", DOT[e.tone])} />
            <div className="min-w-0 flex-1">
              <div className="text-[0.8125rem]">{e.title}</div>
              {e.detail && <div className="text-[0.75rem] text-subtle-foreground">{e.detail}</div>}
            </div>
          </div>
        ))}
        {list.length > SHOWN && (
          <button type="button" className="w-fit text-[0.75rem] text-muted-foreground hover:text-foreground" data-testid="changes-more" onClick={() => setAll(!all)}>
            {all ? "收起" : `更早的 ${list.length - SHOWN} 条 ›`}</button>
        )}
      </div>
    </section>
  )
}
