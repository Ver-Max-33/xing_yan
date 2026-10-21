// 研究记录与 AI 评估 (spec §10.4): every research with what followed it, and what followed each lean once its 20 trading
// days have passed — from the first open after the research was made, against the market's benchmark. Forward records
// only: a research is never run on the past (the model may know what came after).
import { Link, useNavigate } from "react-router"
import { changeClass, codeOf, fmtPct, shortDate } from "@/lib/format"
import { outcomeText, useResearchRecent } from "@/lib/research"
import { cn } from "@/lib/utils"
import { Empty, PageHeader, Section } from "./common"
import { PriceActionSection } from "./research/PriceActionSection"

export default function ResearchPage() {
  const { data, error } = useResearchRecent()
  const navigate = useNavigate()
  const open = (lid: string) => `/k/${encodeURIComponent(lid)}?panel=research&tab=ai`
  const ev = data?.evaluation
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="研究" />
      <div className="min-h-0 flex-1 overflow-y-auto px-6">
        {error && <div className="py-10 text-center text-[0.875rem] text-error">读不出来：{String(error)}</div>}
        {!data && !error && <Empty>读取…</Empty>}
        {data && ev && <>
          <Section title="AI 评估" hint={`只用前向记录：每次研究之后，从研究后的第一个开盘到第 ${ev.hold} 个交易日收盘，对照市场基准的超额。倾向是 AI 对证据的概括，不是买卖建议；这里看的是它的概括和之后的走势对不对得上。`}>
            <table className="w-full max-w-[45rem] text-[0.875rem]" data-testid="research-evaluation">
              <thead className="text-[0.8125rem] text-muted-foreground">
                <tr className="h-8 border-b border-border [&>th]:px-2 [&>th]:font-normal [&>th]:whitespace-nowrap">
                  <th className="text-left">倾向</th><th className="text-right">研究</th><th className="text-right">已结算</th>
                  <th className="text-right">{ev.hold} 日超额均值</th><th className="text-right">中位数</th><th className="text-right">超额为正</th>
                </tr>
              </thead>
              <tbody>
                {ev.groups.map((g) => (
                  <tr key={g.lean} className="h-8 border-b border-border/60 [&>td]:px-2">
                    <td className="whitespace-nowrap">{g.lean}</td><td className="num text-right">{g.n}</td><td className="num text-right">{g.settled}</td>
                    <td className={cn("num text-right", changeClass(g.mean_excess))}>{fmtPct(g.mean_excess, 2)}</td>
                    <td className={cn("num text-right", changeClass(g.median_excess))}>{fmtPct(g.median_excess, 2)}</td>
                    <td className="num text-right">{fmtPct(g.win, 1, false)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-[0.8125rem] text-subtle-foreground">
              {ev.enough ? "" : "支持、反对各有 30 条已结算之前，只看数字、不下结论。"}还有 {ev.pending} 条在等结算；演示数据上的研究不算。同一只股票相隔不到 {ev.hold} 个交易日的研究，结算的区间是重叠的。</p>
          </Section>
          <Section title="最近的研究" hint="在 K 线页右上角的「AI 研究」里生成；设置 → AI 可以让每天收盘扫描后，自动研究几只新出信号的股票。">
            {data.records.length === 0 ? <Empty>还没有研究。打开一只股票的 K 线，点「AI 研究」。</Empty> : (
              // at most a reading width, its columns together on a wide screen; the summary takes the rest of it, cut short —
              // narrow (a small window, 大 / 特大 text), 证据截至 and 来源 give way so the summary keeps some room
              <div className="@container">
              <table className="w-full max-w-[90rem] text-[0.875rem]" data-testid="research-records">
                <thead className="text-[0.8125rem] text-muted-foreground">
                  <tr className="h-8 border-b border-border [&>th]:px-2 [&>th]:font-normal [&>th]:whitespace-nowrap">
                    <th className="text-left">生成</th><th className="text-left">股票</th><th className="text-left @max-[56rem]:hidden">证据截至</th>
                    <th className="text-left">倾向</th><th className="text-left">摘要</th><th className="text-left">核对</th>
                    <th className="text-left @max-[56rem]:hidden">来源</th><th className="text-right">{ev.hold} 日超额</th>
                  </tr>
                </thead>
                <tbody>
                  {data.records.map((r) => (
                    <tr key={r.id} className="h-8 cursor-pointer border-b border-border/60 hover:bg-accent/40 [&>td]:px-2"
                      onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) void navigate(open(r.listing_id)) }}>
                      <td className="num whitespace-nowrap">{r.created_at.slice(5, 16).replace("T", " ")}</td>
                      <td className="whitespace-nowrap"><Link className="hover:underline" to={open(r.listing_id)}>
                        {r.name ?? codeOf(r.listing_id)} <span className="num text-subtle-foreground">{codeOf(r.listing_id)}</span></Link></td>
                      <td className="num whitespace-nowrap @max-[56rem]:hidden">{shortDate(r.day)}</td><td className="whitespace-nowrap">{r.output.lean}</td>
                      <td className="w-full max-w-0 truncate text-muted-foreground" title={r.output.summary}>{r.output.summary}</td>
                      <td className={cn("whitespace-nowrap", r.status !== "ok" && "text-warning")}>{r.status === "ok" ? "核对过" : "需复核"}</td>
                      <td className="text-muted-foreground @max-[56rem]:hidden"><span className="whitespace-nowrap">{r.trigger === "daily" ? "每日自动" : "手动"}</span>
                        {r.demo && <> · <span className="whitespace-nowrap">演示数据</span></>}</td>
                      <td className={cn("num whitespace-nowrap text-right", r.outcome === "settled" ? changeClass(r.excess20) : "text-subtle-foreground")}>{outcomeText(r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </Section>
        </>}
        <PriceActionSection />
      </div>
    </div>
  )
}
