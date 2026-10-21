// 市场 (spec §7.3; designs 2026-10-12-market-live and -market-page-redesign): the market and its industries — 大盘 in one
// row, 情绪 in one row, the industries as a heat grid or a ranking with the chosen one's detail, 连板天梯 and the latest
// 盘面变化 — from the whole-market quote while A shares trade (every 10 s), else the stored day's close; the toolbar says
// whose numbers they are. 日股 has no whole-market intraday source: its close, indexes and 33 業種. No lists of your own:
// stocks are looked at in 选股 and on the K-line page, which carry their industry. Every number is a description, never
// a signal. Layout per the Pen boards 20 (wide), 20c (content under 1100 px), 20d (a stored day), 20f (日股).
import { useEffect, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { type Market } from "@/lib/api"
import { MARKET_NAME } from "@/lib/format"
import { type BoardResponse, useBoard, useTape } from "@/lib/market"
import { sessionNote, whyLine } from "@/lib/marketWords"
import { readPersisted, usePersisted, writePersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import { Empty, PageHeader } from "./common"
import { Changes } from "./market/Changes"
import { Glossary } from "./market/Glossary"
import { Industries, type Wanted } from "./market/Industries"
import { Ladder } from "./market/Ladder"
import { Mood } from "./market/Mood"
import { Overview } from "./market/Overview"

const NARROW = 1100   // the content's width under which the narrow layout (board 20c) takes over

function useNarrow(ref: React.RefObject<HTMLDivElement | null>): boolean {
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const o = new ResizeObserver(([e]) => setNarrow(e.contentRect.width < NARROW))
    o.observe(el)
    return () => o.disconnect()
  }, [ref])
  return narrow
}

function useClock(ms: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), ms); return () => window.clearInterval(id) }, [ms])
  return now
}

/** The toolbar's words (redesign §5): whose numbers these are, a dot while they are live, how fresh. */
function SessionNote({ r }: { r: BoardResponse }) {
  const now = useClock(1000)
  const n = sessionNote(r, now)
  return (
    <span className="flex min-w-0 items-center gap-2.5" data-testid="market-session">
      <span className="flex items-center gap-1.5 rounded bg-accent px-2 py-0.5 text-foreground">
        {n.dot && <span className={cn("size-1.5 rounded-full", n.dot === "live" ? "bg-up" : "bg-warning")} />}
        <span className="num">{n.label}</span>
      </span>
      {n.fresh && <span className={cn("truncate", n.fresh.late ? "text-warning" : "text-subtle-foreground")} data-testid="market-fresh" title={n.fresh.title}>{n.fresh.text}</span>}
    </span>
  )
}

export default function MarketPage() {
  const [market, setMarket] = usePersisted<Market>("market.market", "CN")
  const { data: r, error } = useBoard(market)
  const { data: tape } = useTape(market, market === "CN")
  // only the record of the board's own day: an app opened after the close has none of today's yet, and the endpoint
  // then answers the stored day's — drawn here it would be that day's lines under 今天 / 昨天
  const dayTape = tape && r?.board && tape.day === r.board.day ? tape : undefined
  const [params, setParams] = useSearchParams()
  const want: Wanted | null = params.get("industry") ? { level: params.get("level") === "l2" ? "l2" : "l1", name: params.get("industry")! } : null
  const box = useRef<HTMLDivElement>(null)
  const narrow = useNarrow(box)
  const b = r?.board
  const scrollKey = `market.scroll.${market}`
  const restored = useRef<string | null>(null)
  useEffect(() => {   // back from a K-line: where the page was
    if (!b || restored.current === market || !box.current) return
    restored.current = market
    box.current.scrollTop = readPersisted<number>(scrollKey, 0)
  }, [b, market, scrollKey])
  const why = r ? whyLine(r) : null
  const cn_ = market === "CN"
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-3">市场
        <span role="tablist" className="no-drag flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {(["CN", "JP"] as Market[]).map((m) => (
            <button key={m} type="button" role="tab" aria-selected={market === m} onClick={() => { restored.current = null; setMarket(m) }}
              className={cn("rounded px-2 py-0.5", market === m ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>{MARKET_NAME[m]}</button>
          ))}
        </span>
        {r && <span className="min-w-0 truncate text-[0.8125rem] font-normal"><SessionNote r={r} /></span>}</span>}>
        <Glossary />
      </PageHeader>
      <div ref={box} className="min-h-0 flex-1 overflow-y-auto px-6 py-5" data-testid="market-page" data-narrow={narrow ? "1" : "0"}
        onScroll={(e) => writePersisted(scrollKey, e.currentTarget.scrollTop)}>
        {error && <div className="py-10 text-center text-[0.875rem] text-error">读不出来：{String(error)}</div>}
        {!r && !error && <Empty>读取中…（第一次打开要几秒）</Empty>}
        {r && !b && <Empty>{r.why_not}<span className="mt-1.5 block">在 设置 → 数据 选这个市场的数据档、配好数据源的密钥，再到{" "}
          <Link to="/data" className="underline">数据</Link> 页「补历史…」</span></Empty>}
        {r && b && (
          <div className="flex flex-col gap-6">
            {why && <p className={cn("-mb-3 text-[0.75rem]", (r.error && r.mode === "close") || (b.impossible ?? 0) > 0 ? "text-warning" : "text-subtle-foreground")} data-testid="market-why">{why}</p>}
            <Overview r={r} tape={cn_ ? dayTape : undefined} narrow={narrow} />
            {cn_ && <Mood r={r} tape={dayTape} />}
            <Industries r={r} narrow={narrow} want={want} onWanted={() => setParams({}, { replace: true })} />
            {cn_ && (
              <div className="flex items-start gap-8">
                <div className="min-w-0 flex-1"><Ladder b={b} /></div>
                <div className={cn("shrink-0", narrow ? "w-[19rem]" : "w-[22rem]")}><Changes r={r} /></div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
