// 数据 (spec §4.5, §4.6): snapshots, quality issues and the shadow cross-check against an independent source, per
// market — and what the command line does, here: update now, 补历史 (resumable), a sample checked now. Each runs in
// the market's turn (never beside its update or scan) with its progress in view; demo data is never fetched into.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { History, Loader2, RefreshCw, ShieldCheck } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { endpoints, type Market, useStatus } from "@/lib/api"
import { doing, jobEndpoints, useJobs } from "@/lib/jobs"
import { cn } from "@/lib/utils"
import { Empty, PageHeader, Section } from "./common"
import { MARKET_NAME } from "@/lib/format"

const SEVERITY: Record<string, string> = { block: "拦截", row: "行", warn: "提示" }
const DEMO = "这里是演示数据：不从数据源取数，也没有独立来源可以核对。换到正式档（设置 → 数据档）后再补历史"
const yesterday = () => new Date(Date.now() - 86_400_000).toLocaleDateString("sv")   // YYYY-MM-DD, local: 补历史 ends there
const tenYearsAgo = () => { const d = new Date(); return `${d.getFullYear() - 10}-${String(d.getMonth() + 1).padStart(2, "0")}-01` }

function BackfillDialog({ market, open, onOpenChange, hasData }: { market: Market; open: boolean; onOpenChange: (o: boolean) => void; hasData: boolean }) {
  const qc = useQueryClient()
  const [start, setStart] = useState(tenYearsAgo)
  const [onlyFin, setOnlyFin] = useState(false)
  const go = useMutation({
    mutationFn: () => jobEndpoints.backfill(market, start, onlyFin && hasData ? ["financials"] : undefined),
    onSuccess: () => {
      onOpenChange(false)
      void qc.invalidateQueries({ queryKey: ["jobs"] })
      toast.success("开始补历史：进度在这一页和底部状态栏")
    },
    onError: (e) => toast.error(String(e)),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>补{MARKET_NAME[market]}的历史</DialogTitle>
          <DialogDescription>从起始日取到昨天（今天的由每天的更新取），取完发布成新快照并扫描已启用的规则。全市场十年要取很久（按数据源的限速）；
            中途关掉应用或出错，再补一次会从停下的地方接着取，取完之前不做每天的更新。</DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-3 text-[0.875rem]" onSubmit={(e) => { e.preventDefault(); go.mutate() }}>
          <label className="flex items-center gap-3">起始日
            <Input type="date" aria-label="补历史的起始日" value={start} max={yesterday()} onChange={(e) => setStart(e.target.value)} className="h-8 w-40" /></label>
          <label className={cn("flex items-center gap-2", !hasData && "text-subtle-foreground")} title={hasData ? undefined : "先补日线：还没有数据时只补财务报告，会得到一个没有日线的快照"}>
            <input type="checkbox" checked={onlyFin && hasData} disabled={!hasData} onChange={(e) => setOnlyFin(e.target.checked)} />只补财务报告（日线已经补过）</label>
          <DialogFooter className="mt-1"><Button type="submit" disabled={!start || go.isPending}>开始补历史</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default function DataPage() {
  const [market, setMarket] = useState<Market>("CN")
  const { data: status } = useStatus()
  const m = status?.markets.find((x) => x.market === market)
  const snaps = useQuery({ queryKey: ["snapshots", market], queryFn: () => endpoints.snapshots(market) })
  const quality = useQuery({ queryKey: ["quality", market], queryFn: () => endpoints.quality(market) })
  const cc = useQuery({ queryKey: ["crosscheck", market], queryFn: () => endpoints.crosscheck(market) })
  const qc = useQueryClient()
  const { data: jobs } = useJobs()
  const job = jobs?.markets.find((j) => j.market === market)
  const busy = !!job?.running, demo = !!job?.latest?.demo
  const [backfill, setBackfill] = useState(false)
  useEffect(() => {   // the market's turn over (or a job that began and ended between two polls): read again what it changed
    if (busy) return
    for (const k of ["snapshots", "quality", "crosscheck"]) void qc.invalidateQueries({ queryKey: [k, market] })
    void qc.invalidateQueries({ queryKey: ["status"] })
  }, [busy, job?.updated_at, market, qc])
  const start = (what: Promise<unknown>) => what.then(() => qc.invalidateQueries({ queryKey: ["jobs"] }), (e) => toast.error(String(e)))
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-3">数据   {/* the market switch where every page has it */}
        <span role="tablist" aria-label="市场" className="no-drag flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {(["CN", "JP"] as Market[]).map((k) => (
            <button key={k} type="button" role="tab" aria-selected={market === k} onClick={() => setMarket(k)}
              className={cn("rounded px-2 py-0.5", market === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
              {MARKET_NAME[k]}
            </button>
          ))}
        </span></span>} />
      <div className="min-h-0 flex-1 overflow-y-auto px-6">
        <div data-testid="data-snapshot">
        <Section title="当前快照" hint="每次发布都是一个不可变的快照。每天收盘后自动更新，也可以现在更新；第一次用正式档先补历史。">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" disabled={busy || !job?.latest} title={job?.latest ? undefined : "还没有数据：先补历史，之后每天的更新才有基础"}
              onClick={() => void start(jobEndpoints.run(market).then((r) => {
              if (!r.fetch) toast("这次没有新的日线可取（交易日收盘前、演示数据，或补历史还没取完），只把已有的数据重新扫描一遍")
            }))}>
              <RefreshCw className="size-4" />立即更新</Button>
            <Button size="sm" variant="outline" disabled={busy || demo} title={demo ? DEMO : undefined} onClick={() => setBackfill(true)}>
              <History className="size-4" />补历史…</Button>
            {busy ? <span className="flex items-center gap-1.5 text-[0.8125rem] text-muted-foreground"><Loader2 className="size-3.5 animate-spin" />
              {job?.message ?? (job ? doing(job) : "进行中…")}</span>
              : job?.message && <span className={cn("text-[0.8125rem]", ["error", "delayed", "interrupted"].includes(job.status) ? "text-warning" : "text-muted-foreground")}>{job.message}</span>}
          </div>
          {demo && <p className="mb-2 text-[0.8125rem] text-subtle-foreground">{DEMO}。</p>}
          {m?.snapshot ? (
            <div className="num text-[0.875rem]">
              <div>{m.snapshot.id}</div>
              <div className="text-muted-foreground">数据截至 {m.snapshot.as_of} · 发布于 {m.snapshot.created_at.replace("T", " ").slice(0, 19)} · {m.snapshot.note}</div>
              {m.pending_units > 0 && <div className="text-warning">有 {m.pending_units} 个已下载、未发布的单元（补历史中断了：再点「补历史」会接着取）</div>}
            </div>
          ) : <Empty>还没有数据：点「补历史…」从数据源取</Empty>}
        </Section>
        </div>
        <BackfillDialog key={market} market={market} open={backfill} onOpenChange={setBackfill} hasData={!!m?.snapshot} />
        <Section title="影子校验" hint="每天更新后随机抽 30 只，与独立来源逐项比对原始价格、成交量和成交额。只报告，从不用独立来源改数据。">
          <div className="mb-3">
            <Button size="sm" variant="outline" disabled={busy || demo || !job?.latest}
              title={demo ? DEMO : job?.latest ? "抽 30 只 × 最近 5 天，和独立来源比对" : "还没有数据：补历史之后才有东西可以核对"}
              onClick={() => void start(jobEndpoints.crosscheck(market))}><ShieldCheck className="size-4" />抽样核对</Button>
          </div>
          {!cc.data?.summary.length ? <Empty>还没有核对记录</Empty> : (
            <>
              <div className="flex flex-wrap gap-2">
                {cc.data.summary.map((s) => (
                  <Badge key={`${s.reference}-${s.field}`} variant="outline" className={cn("num font-normal", s.ok < s.n && "border-error/60 text-error")}>
                    {s.reference} · {s.field} {s.ok}/{s.n}
                  </Badge>
                ))}
              </div>
              {cc.data.mismatches.length > 0 && (
                <Table className="mt-3">
                  <TableHeader><TableRow><TableHead>来源</TableHead><TableHead>证券</TableHead><TableHead>日期</TableHead><TableHead>字段</TableHead><TableHead className="text-right">本地</TableHead><TableHead className="text-right">独立来源</TableHead></TableRow></TableHeader>
                  <TableBody>
                    {cc.data.mismatches.map((x, i) => (
                      <TableRow key={i}>
                        <TableCell>{x.reference}</TableCell><TableCell className="num">{x.listing_id}</TableCell><TableCell className="num">{x.date}</TableCell>
                        <TableCell>{x.field}</TableCell><TableCell className="num text-right">{x.stored}</TableCell><TableCell className="num text-right">{x.ref_value}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </>
          )}
        </Section>
        <Section title="质量问题" hint="拦截级问题不会被发布；行级问题会让相关条件判为「未知」。">
          {!quality.data?.length ? <Empty>当前快照没有质量问题</Empty> : (
            <Table>
              <TableHeader><TableRow><TableHead>级别</TableHead><TableHead>检查</TableHead><TableHead>证券</TableHead><TableHead>日期</TableHead><TableHead>说明</TableHead></TableRow></TableHeader>
              <TableBody>
                {quality.data.map((q, i) => (
                  <TableRow key={i}>
                    <TableCell><Badge variant="outline" className={cn("font-normal", q.severity === "row" && "text-warning")}>{SEVERITY[q.severity] ?? q.severity}</Badge></TableCell>
                    <TableCell className="num">{q.check_name}</TableCell><TableCell className="num">{q.listing_id ?? "—"}</TableCell>
                    <TableCell className="num">{q.date ?? "—"}</TableCell><TableCell className="text-muted-foreground">{q.detail}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>
        <Section title="快照历史">
          <div className="num grid grid-cols-[auto_auto_auto_minmax(0,1fr)] gap-x-6 gap-y-2 text-[0.8125rem] whitespace-nowrap">
            {(snaps.data ?? []).map((s) => (
              <div key={s.id} className="contents">
                <span>{s.id}</span><span className="text-muted-foreground">{s.as_of}</span>
                <span className="text-subtle-foreground">{s.created_at.replace("T", " ").slice(0, 19)}</span>
                <span className="truncate text-subtle-foreground">{s.note}</span>
              </div>
            ))}
          </div>
        </Section>
      </div>
    </div>
  )
}
