// 大盘 in one row (redesign §2.2): each index with its change, its state and where it stands to its 60-day line and box
// (上证 with its line since the open, not in the narrow layout); 涨跌家数 with a red / grey / green bar; the turnover with
// the day before's — before 10:30 at the same minute, after it the whole day projected (the volume clock is unbiased
// from 10:30). 日股: its indexes, its 涨跌家数 and the 東証's turnover.
import { Fragment } from "react"
import { changeClass, fmtPct, indexName } from "@/lib/format"
import { STATE_NAME, type BoardResponse, type IndexNow, type TapeResponse } from "@/lib/market"
import { amountLine } from "@/lib/marketWords"
import { cn } from "@/lib/utils"
import { count, yuan, yen } from "./fmt"
import { MiniLine } from "./MiniLine"

const IDS = { CN: ["CN:IDX:000001", "CN:IDX:399001", "CN:IDX:399006"], JP: ["JP:IDX:TOPIX", "JP:IDX:N225"] }
const HINT = "60 日线：近 60 根收盘的平均（含此刻的价）。状态：收在 60、200 日线之上且 60 日线比 5 根前高为上升，都在下面且 60 日线向下为下降，其余为震荡。"
  + "箱体：指数自己的箱，看大盘先看它。"

function boxNote(i: IndexNow): string | null {
  if (i.to_top != null && i.to_top >= 0) return `已过箱顶 ${fmtPct(i.to_top, 1, false)}`
  if (i.to_bottom != null && i.to_bottom <= 0) return `已破箱底 ${fmtPct(-i.to_bottom, 1, false)}`
  if (i.to_top != null && i.to_top >= -0.02) return `距箱顶 ${fmtPct(-i.to_top, 1, false)}`
  if (i.to_bottom != null && i.to_bottom <= 0.02) return `距箱底 ${fmtPct(i.to_bottom, 1, false)}`
  return null
}

function Cell({ first, label, title, testid, children, sub }: {
  first: boolean; label: string; title?: string; testid: string; children: React.ReactNode; sub: React.ReactNode
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", first ? "pr-4" : "border-l border-border px-4")} title={title} data-testid={testid}>
      <span className="text-[0.75rem] text-muted-foreground">{label}</span>
      {children}
      {/* the line wraps between its phrases, not inside one */}
      <span className="text-[0.6875rem] leading-snug text-subtle-foreground">
        {typeof sub === "string" ? sub.split(" · ").map((x, k) => <Fragment key={k}>{k ? " · " : ""}<span className="whitespace-nowrap">{x}</span></Fragment>) : sub}
      </span>
    </div>
  )
}

export function Overview({ r, tape, narrow }: { r: BoardResponse; tape: TapeResponse | undefined; narrow: boolean }) {
  const b = r.board!
  const cn_ = r.market === "CN", live = r.mode === "live" && r.session.state !== "after"   // after the close the day's total is known
  const ids = IDS[r.market].filter((id) => b.indexes[id])
  const shLine = cn_ && !narrow && tape ? tape.rows.filter((x) => x.sh_last != null).map((x) => [x.i, x.sh_last!] as [number, number]) : []
  const lim = b.limits
  const money = cn_ ? yuan : yen
  const amountSub = amountLine(r, money)
  return (
    <div className={cn("grid gap-y-3", cn_ ? "grid-cols-5" : "grid-cols-[repeat(auto-fit,minmax(11rem,1fr))]")} data-testid="market-overview">
      {ids.map((id, k) => {
        const i = b.indexes[id]
        const sub = [i.state == null ? null : STATE_NAME[String(i.state)][0], i.dist60 != null ? `60 日线${i.dist60 >= 0 ? "上" : "下"} ${fmtPct(Math.abs(i.dist60), 1, false)}` : null, boxNote(i)]
        return (
          <Cell key={id} first={k === 0} label={indexName(id)} title={HINT} testid={`overview-${id}`} sub={sub.filter(Boolean).join(" · ")}>
            <span className="flex min-w-0 flex-wrap items-center gap-x-2.5">
              <span className="num text-[1.125rem] font-medium whitespace-nowrap">{i.last.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
              <span className={cn("num text-[0.8125rem] whitespace-nowrap", changeClass(i.chg))}>{fmtPct(i.chg)}</span>
              {k === 0 && shLine.length > 1 && (
                <MiniLine className="h-7 min-w-12 flex-1" testid="overview-sh-line" base={tape?.index?.prev} format={(v) => v.toFixed(2)}
                  lines={[{ name: "上证", points: shLine, tone: "stroke-foreground" }]} />
              )}
            </span>
          </Cell>
        )
      })}
      <Cell first={ids.length === 0} label="涨跌家数" testid="overview-breadth"
        sub={cn_ && lim.known ? `平 ${count(b.flat)} · 涨停 ${count(lim.limit_up)} · 跌停 ${count(lim.limit_down)}` : `平 ${count(b.flat)} · ${cn_ ? "成交" : "东证"} ${count(b.traded)} 只`}>
        <span className="flex flex-wrap items-baseline gap-x-2.5 text-[1.125rem] font-medium">
          <span className="whitespace-nowrap text-up">涨 <span className="num">{count(b.up)}</span></span>
          <span className="whitespace-nowrap text-down">跌 <span className="num">{count(b.down)}</span></span>
        </span>
        <span className="flex h-1.5 gap-px overflow-hidden rounded-full" aria-hidden>
          <span className="bg-up" style={{ flexGrow: b.up }} /><span className="bg-subtle-foreground/50" style={{ flexGrow: b.flat }} /><span className="bg-down" style={{ flexGrow: b.down }} />
        </span>
      </Cell>
      <Cell first={false} label={cn_ ? "成交额（沪深京 A 股）" : "成交额（东证）"} testid="overview-amount" sub={amountSub}
        title={r.mode === "auction_result" ? "集合竞价撮合的成交额" : live ? "到此刻的成交额；10:30 以后按成交节奏推算全天（更早的推算偏差大，不推算）" : "这一天的成交额，和前一天比"}>
        <span className="num text-[1.125rem] font-medium whitespace-nowrap">{money(b.amount)}</span>
      </Cell>
    </div>
  )
}
