// 行业详情 (redesign §2.4.3): the chosen industry — its level and size, today's change, its tag with the two numbers that
// make it, against the market over 20 bars, the share up, limit-ups, the turnover against its 5-day average (from 10:30
// while live), three leaders (to their K-line), its 二级 (a 一级's) or its siblings (a 二级's), and the way to 选股's results
// in it. No member lists: the page stops at industries.
import { ArrowRight } from "lucide-react"
import { useNavigate } from "react-router"
import { Button } from "@/components/ui/button"
import { changeClass, fmtPct } from "@/lib/format"
import type { BoardResponse, Industry } from "@/lib/market"
import { industryName, tagSentence } from "@/lib/marketWords"
import { readPersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import { tagTone } from "./order"

function Num({ label, value, sub, title }: { label: string; value: string; sub?: string; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[0.6875rem] text-muted-foreground">{label}</div>
      <div className="flex flex-wrap items-baseline gap-x-1.5"><span className="num text-[0.9375rem] whitespace-nowrap">{value}</span>
        {sub && <span className="num text-[0.6875rem] whitespace-nowrap text-subtle-foreground">{sub}</span>}</div>
    </div>
  )
}

export function IndustryDetail({ r, ind, level, related, of, dayWord, onPick }: {
  r: BoardResponse; ind: Industry; level: "l1" | "l2"; related: Industry[]; of: number; dayWord: string
  onPick: (level: "l1" | "l2", name: string) => void
}) {
  const navigate = useNavigate()
  const cn_ = r.market === "CN", live = r.mode === "live"
  const scheme = !cn_ ? "东证 33 業種" : level === "l2" ? "申万二级" : "申万一级"
  const early = live && r.session.time < "10:30:00"
  const sentence = tagSentence(ind)
  const toScreen = () => {
    const rule = readPersisted<{ rule: string | null } | null>("screen.view", null)?.rule
    navigate(`/screen?${rule ? `rule=${encodeURIComponent(rule)}&` : ""}industry=${level}:${encodeURIComponent(ind.name)}`)
  }
  const meta = `${level === "l2" && ind.parent ? `属${ind.parent} · ` : ""}${scheme} · ${ind.members} 只`   // the name is never squeezed: this gives way, its 一级 last
  const kidsTitle = level === "l1" && cn_ ? `${ind.name}：${related.map((x) => industryName(x.name)).join("、")}` : undefined
  return (
    <div className="flex flex-col gap-3.5 rounded-md border border-border p-4" data-testid="industry-detail">
      <div>
        <div className="flex items-baseline gap-2">
          <span className="shrink-0 text-[0.9375rem] font-semibold" title={kidsTitle}>{industryName(ind.name)}</span>
          <span className="min-w-0 truncate text-[0.6875rem] text-subtle-foreground" title={meta}>{meta}</span>
          <span className={cn("num ml-auto text-[1.0625rem] font-medium", changeClass(ind.chg))}>{fmtPct(ind.chg)}</span>
        </div>
        {sentence && (
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.75rem]" data-testid="industry-tag">
            {ind.tag && <span className={cn("rounded px-1.5 py-0.5 font-medium", tagTone(ind.tag))}>{ind.tag}</span>}
            <span className="text-muted-foreground">{sentence}</span>
          </div>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
        <Num label="近 20 日比大盘" value={fmtPct(ind.rel20, 1)} title="行业近 20 根 K 线涨幅的中位数，减全市场的" />
        <Num label="上涨" value={ind.up_ratio == null ? "—" : `${Math.round(ind.up_ratio * 100)}%`} sub={`${ind.up} / ${ind.traded}`} title={`${ind.traded} 只成交过的里 ${ind.up} 只上涨`} />
        {cn_ && <Num label="涨停" value={ind.limit_up == null ? "未知" : String(ind.limit_up)} />}
        <Num label="成交额" value={early || ind.amount_ratio == null ? "—" : `${ind.amount_ratio.toFixed(1)}×`} sub="5 日均"
          title={early ? "10:30 以后才有（更早按成交节奏折算偏差大）" : "成交额是这些股票近 5 日平均的几倍（盘中按成交节奏折算到此刻）"} />
      </div>
      {ind.leaders.length > 0 && (
        <div data-testid="industry-leaders"><div className="mb-1 text-[0.75rem] font-semibold">领涨</div>
          {ind.leaders.map((s) => (
            <button key={s.listing_id} type="button" onClick={() => navigate(`/k/${s.listing_id}`)} data-testid="leader"
              className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-[0.8125rem] hover:bg-accent">
              <span className="min-w-0 truncate">{s.name ?? s.listing_id}</span><span className="flex-1" />
              {s.sealed && <span className="text-[0.6875rem] text-up">涨停</span>}
              <span className={cn("num w-16 text-right text-[0.75rem]", changeClass(s.chg))}>{fmtPct(s.chg)}</span>
            </button>
          ))}
        </div>
      )}
      {cn_ && level === "l1" && !related.length && (   // no 二级 in this data yet (it came with a later update): said, not left out
        <div className="text-[0.75rem]" data-testid="industry-children"><span className="font-semibold">申万二级</span>
          <span className="ml-2 text-subtle-foreground">二级行业下次更新数据后才有</span></div>
      )}
      {cn_ && related.length > 0 && (
        <div data-testid="industry-children">
          <div className="mb-1 text-[0.75rem] font-semibold">{level === "l1" ? "申万二级" : `同属${ind.parent ?? ""}的二级`}<span className="font-normal text-muted-foreground">（{dayWord}）</span></div>
          {related.map((x) => (
            <button key={x.name} type="button" onClick={() => onPick("l2", x.name)} data-testid="child-row" data-industry={x.name}
              className={cn("flex w-full flex-col rounded px-1 py-1 text-left hover:bg-accent", x.name === ind.name && "bg-accent")}>
              <span className="flex w-full items-baseline gap-2 text-[0.8125rem]">
                <span className="min-w-0 flex-1 truncate">{industryName(x.name)}</span>
                <span className={cn("num shrink-0 text-[0.75rem]", changeClass(x.chg))}>{fmtPct(x.chg)}</span>
              </span>
              <span className="num text-[0.6875rem] text-subtle-foreground">{x.rank == null ? "不排名" : `第 ${x.rank} / ${of}`}{(x.limit_up ?? 0) > 0 ? ` · 涨停 ${x.limit_up}` : ""}</span>
            </button>
          ))}
        </div>
      )}
      <Button variant="outline" size="sm" onClick={toScreen} data-testid="to-screen">看这个行业的选股结果<ArrowRight className="size-3.5" /></Button>
    </div>
  )
}
