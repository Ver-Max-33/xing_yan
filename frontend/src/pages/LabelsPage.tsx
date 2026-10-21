// 箱体标注 (spec §5.10, §6.3 手画对照, §13 P2 验收): your sample boxes and how far the algorithm's boxes agree
// with them — per sample (edges, overlap, start), per group (调参 70% / 检验 30%, fixed when a sample is
// made), per category, against your own blind redraws — and the P2 acceptance line. Review each
// disagreement, redraw a sample a week later without seeing the old line, or open one where it was drawn.
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { CircleCheck, CircleX, Dices, RefreshCw } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { ask } from "@/lib/ask"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { type Market, useInstrument } from "@/lib/api"
import { codeOf } from "@/lib/format"
import { BOX_CATEGORIES, type Label, type LabelResult, NOT_BOX_CATEGORIES, ruleEndpoints } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { Empty, PageHeader, Section } from "./common"
import { BOX_TARGET, canRedraw, NOT_BOX_TARGET, PER_MARKET, progress, REDRAW_TARGET } from "./labels/progress"

const CATEGORY = { ...BOX_CATEGORIES, ...NOT_BOX_CATEGORIES }
const VERDICT: Record<NonNullable<Label["verdict"]>, string> = { algo_wrong: "算法错", me_wrong: "我画错", both_ok: "两种都说得通" }
const SCALE: Record<string, string> = { long: "大箱", mid: "中箱", short: "小箱" }
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`)

function Name({ id }: { id: string }) {
  const { data } = useInstrument(id)
  return <>{data?.name ?? codeOf(id)} <span className="num text-[0.75rem] text-subtle-foreground">{codeOf(id)}</span></>
}

function Diff({ r }: { r: LabelResult | undefined }) {
  if (!r) return <span className="text-subtle-foreground">—</span>
  if (!r.is_box) return r.false_draw ? <span className="text-warning">算法在这里画了箱（误画）</span> : <span className="text-structure">算法没画（对）</span>
  if (!r.box_id) return <span className="text-warning">算法在这里没有箱体</span>
  const parts = [`上沿差 ${pct(r.top_diff)}`, `下沿差 ${pct(r.bottom_diff)}`, `重叠 ${r.overlap?.toFixed(2) ?? "—"}`, `起点差 ${r.start_diff ?? "—"} 根`]
  return (
    <span className={cn("num", r.consistent ? "text-structure" : "text-warning")} title={parts.join(" · ")}>
      {r.consistent ? "一致" : "不一致"}<span className="text-[0.75rem] text-subtle-foreground"> · {parts.join(" · ")}</span>
    </span>
  )
}

type Filter = "all" | "pending" | "differ" | "redraw"
/** The algorithm disagrees with the sample (for "not a box": it drew a box there). Redraws have no result. */
const disputed = (r: LabelResult | undefined) => !!r && !r.consistent

export default function LabelsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [filter, setFilter] = useState<Filter>("all")
  const [picking, setPicking] = useState(false)
  const labelsQ = useQuery({ queryKey: ["labels", "*", "qfq"], queryFn: () => ruleEndpoints.labels(undefined, "qfq"), refetchOnMount: "always" })
  const agreeQ = useQuery({ queryKey: ["agreement", "all"], queryFn: () => ruleEndpoints.agreement(null), staleTime: 5 * 60_000 })
  const labels = labelsQ.data ?? []
  const p = progress(labels)
  const results = new Map((agreeQ.data?.labels ?? []).map((r) => [r.id, r]))
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ["labels"] }), qc.invalidateQueries({ queryKey: ["agreement"] })])

  const pick = async (market: Market) => {
    setPicking(true)
    try {
      const pool = await ruleEndpoints.sample(market, 300, Math.floor(Math.random() * 10_000))
      if (!pool.length) throw new Error("这个市场还没有数据")
      const id = pool[Math.floor(Math.random() * pool.length)]
      const dates = await ruleEndpoints.dates(id)
      if (dates.length < 300) throw new Error(`${id} 的历史太短，再点一次换一只`)
      const day = dates[250 + Math.floor(Math.random() * (dates.length - 270))] // a year of history before it, a month after
      navigate(`/k/${id}?day=${day}&blind=1`)
    } catch (e) {
      toast.error(`没找到：${String(e)}`)
    } finally {
      setPicking(false)
    }
  }
  const setVerdict = async (id: string, v: Label["verdict"]) => {
    try {
      await ruleEndpoints.labelVerdict(id, v)
      await refresh()
    } catch (e) {
      toast.error(`没记上：${String(e)}`)
    }
  }
  const remove = async (l: Label) => {
    if (!(await ask({ title: "删除这条样例？", text: `${l.listing_id} ${l.start} – ${l.end}。删除后它不再算进一致率和校准，没法恢复。`, action: "删除样例" }))) return
    try {
      await ruleEndpoints.deleteLabel(l.id)
      await refresh()
    } catch (e) {
      toast.error(`没删掉：${String(e)}`)
    }
  }

  const shown = labels.filter((l) => {
    const r = results.get(l.id)
    if (filter === "pending") return disputed(r) && !l.verdict
    if (filter === "differ") return disputed(r)
    if (filter === "redraw") return canRedraw(l, labels)
    return true
  })
  const a = agreeQ.data
  const CHECKS: [string, string][] = a ? [
    ["test_rate_vs_yours", a.summary.test.rate === null || a.self.rate === null
      ? `检验组「是箱体」的一致率不低于你自己重画一致率的 80%（还缺${a.summary.test.rate === null ? "检验组的样例" : ""}${a.summary.test.rate === null && a.self.rate === null ? "和" : ""}${a.self.rate === null ? "重画基线" : ""}）`
      : `检验组「是箱体」的一致率 ${pct(a.summary.test.rate)} 不低于你自己重画一致率 ${pct(a.self.rate)} 的 80%`],
    ["no_category_at_zero", "检验组没有哪一类是 0 个一致"],
    ["false_draws_at_most_one", `检验组「不是箱体」的样例，算法误画不超过 1 个（现在 ${a.summary.test.false_draws} 个）`],
    ["all_reviewed", `分歧样例都已复核（还有 ${a.summary.tune.pending + a.summary.test.pending} 个待复核）`],
  ] : []
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="箱体标注">
        <Button variant="outline" size="sm" disabled={picking} onClick={() => void pick("CN")}><Dices className="size-4" />随机找一只 A 股来画</Button>
        <Button variant="outline" size="sm" disabled={picking} onClick={() => void pick("JP")}><Dices className="size-4" />随机找一只日股来画</Button>
        <Button variant="ghost" size="sm" onClick={() => void refresh()}><RefreshCw className={cn("size-4", agreeQ.isFetching && "animate-spin")} />重算一致性</Button>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto px-6">
        <Section title="标注集" hint="自己画箱：在 K 线上打开回放到某一天（看不到之后的 K 线），用矩形画出箱体，点「存为样例」。每类 A 股、日股各 2 个；另画 8 个「不是箱体」；画完一周后挑 16 个盲画一遍作你自己的基线。">
          <div className="num flex flex-wrap gap-6 text-[0.875rem]">
            <div><span className="text-muted-foreground">是箱体</span> {p.boxes} / {BOX_TARGET}</div>
            <div><span className="text-muted-foreground">不是箱体</span> {p.notBoxes} / {NOT_BOX_TARGET}</div>
            <div><span className="text-muted-foreground">重画基线</span> {p.redraws} / {REDRAW_TARGET}</div>
            {p.done && <Badge variant="outline" className="border-structure/60 text-structure">标注集画完了</Badge>}
          </div>
          <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(230px,1fr))] gap-x-6 gap-y-1 text-[0.8125rem]">
            {Object.entries(BOX_CATEGORIES).map(([k, name]) => {
              const c = p.byCategory[k] ?? { CN: 0, JP: 0 }
              const full = c.CN >= PER_MARKET && c.JP >= PER_MARKET
              return (
                <div key={k} className="flex items-baseline gap-2">
                  <span className={cn("min-w-0 flex-1 truncate", full ? "text-muted-foreground" : "text-foreground")} title={name}>{name}</span>
                  <span className={cn("num", c.CN >= PER_MARKET ? "text-structure" : "text-subtle-foreground")}>A {c.CN}/{PER_MARKET}</span>
                  <span className={cn("num", c.JP >= PER_MARKET ? "text-structure" : "text-subtle-foreground")}>日 {c.JP}/{PER_MARKET}</span>
                </div>
              )
            })}
          </div>
        </Section>
        <Section title="验收线" hint="验收线在看到算法结果之前就定下了；算的是全部样例（A 股和日股一起），箱体用默认设置。">
          {!a ? <Empty>{agreeQ.isFetching ? "在样例所在的股票上重算箱体…" : agreeQ.error ? `算不出来：${String(agreeQ.error)}` : "…"}</Empty> : (
            <div className="space-y-1.5 text-[0.875rem]" data-testid="acceptance">
              {!p.done && <div className="text-subtle-foreground">标注集还没画完（{p.boxes}/{BOX_TARGET} · {p.notBoxes}/{NOT_BOX_TARGET} · {p.redraws}/{REDRAW_TARGET}），下面的结果先作参考。</div>}
              {CHECKS.map(([k, text]) => (
                <div key={k} className="flex items-start gap-1.5">
                  {a.acceptance.checks[k] ? <CircleCheck className="mt-0.5 size-4 shrink-0 text-structure" /> : <CircleX className="mt-0.5 size-4 shrink-0 text-subtle-foreground" />}
                  <span>{text}</span>
                </div>
              ))}
              {a.acceptance.too_strict && <div className="text-warning">你自己重画的一致率低于 60%：说明 2% 和 0.7 这两个数对你的画法太严，先一起看分歧样例、放宽它们，再开始验收。</div>}
              <div className="pt-1">{a.acceptance.passed && p.done ? <Badge className="bg-structure">验收通过</Badge> : <Badge variant="outline">还没通过</Badge>}</div>
            </div>
          )}
        </Section>
        {a && (
          <Section title="一致率" hint={`单个样例一致：上下沿差都 ≤ ${pct(a.thresholds.edge)}、价区重叠 ≥ ${a.thresholds.overlap}、起点差 ≤ max(${a.thresholds.start_bars} 根, 箱长的 ${pct(a.thresholds.start_share)})；你判为「两种都说得通」的计为一致。`}>
            <Table>
              <TableHeader><TableRow><TableHead>分组</TableHead><TableHead className="text-right">是箱体</TableHead><TableHead className="text-right">一致</TableHead><TableHead className="text-right">一致率</TableHead><TableHead className="text-right">不是箱体</TableHead><TableHead className="text-right">误画</TableHead><TableHead className="text-right">待复核</TableHead></TableRow></TableHeader>
              <TableBody>
                {(["tune", "test"] as const).map((g) => {
                  const s = a.summary[g]
                  return (
                    <TableRow key={g}>
                      <TableCell>{g === "tune" ? "调参组（70%）" : "检验组（30%）"}</TableCell>
                      <TableCell className="num text-right">{s.boxes}</TableCell><TableCell className="num text-right">{s.consistent}</TableCell>
                      <TableCell className="num text-right">{pct(s.rate)}</TableCell><TableCell className="num text-right">{s.not_boxes}</TableCell>
                      <TableCell className="num text-right">{s.false_draws}</TableCell><TableCell className="num text-right">{s.pending}</TableCell>
                    </TableRow>
                  )
                })}
                <TableRow>
                  <TableCell>你自己重画</TableCell><TableCell className="num text-right">{a.self.pairs}</TableCell><TableCell className="num text-right">{a.self.consistent}</TableCell>
                  <TableCell className="num text-right">{pct(a.self.rate)}</TableCell><TableCell colSpan={3} />
                </TableRow>
              </TableBody>
            </Table>
          </Section>
        )}
        <Section title={`样例 ${labels.length}`}>
          <Tabs value={filter} onValueChange={(v) => setFilter(v as Filter)}>
            <TabsList>
              <TabsTrigger value="all">全部</TabsTrigger><TabsTrigger value="pending">待复核</TabsTrigger>
              <TabsTrigger value="differ">不一致</TabsTrigger><TabsTrigger value="redraw">可以重画</TabsTrigger>
            </TabsList>
          </Tabs>
          {shown.length === 0 ? <Empty>{labels.length ? "这一栏是空的" : "还没有样例：在 K 线上用矩形画箱、存为样例，或者点右上角随机找一只来画"}</Empty> : (
            <Table className="mt-2">
              <TableHeader><TableRow><TableHead>股票</TableHead><TableHead>类别</TableHead><TableHead>区间</TableHead><TableHead>截止</TableHead><TableHead>分组</TableHead><TableHead>与算法</TableHead><TableHead>复核</TableHead><TableHead /></TableRow></TableHeader>
              <TableBody>
                {shown.map((l) => {
                  const r = results.get(l.id)
                  return (
                    <TableRow key={l.id} data-testid={`label-${l.id}`}>
                      <TableCell><Name id={l.listing_id} />{l.redraw_of && <Badge variant="outline" className="ml-1.5 h-5 px-1.5 text-[0.75rem] font-normal">重画</Badge>}</TableCell>
                      <TableCell className="text-[0.8125rem]">{CATEGORY[l.category] ?? l.category}{l.scale ? ` · ${SCALE[l.scale]}` : ""}</TableCell>
                      <TableCell className="num text-[0.8125rem]">{l.start} – {l.end}</TableCell>
                      <TableCell className="num text-[0.8125rem]">{l.cutoff}</TableCell>
                      <TableCell className="text-[0.8125rem]">{l.grp === "tune" ? "调参" : "检验"}</TableCell>
                      <TableCell className="max-w-[20rem] truncate text-[0.8125rem]"><Diff r={r} /></TableCell>
                      <TableCell className="text-[0.8125rem]">
                        {l.verdict ? <button type="button" className="hover:underline" title="改复核结论" onClick={() => void setVerdict(l.id, null)}>{VERDICT[l.verdict]}</button>
                          : disputed(r) ? (
                            <span className="flex gap-1">
                              {(Object.keys(VERDICT) as NonNullable<Label["verdict"]>[]).map((v) => (
                                <Button key={v} variant="outline" size="sm" className="h-6 px-1.5 text-[0.75rem]" onClick={() => void setVerdict(l.id, v)}>{VERDICT[v]}</Button>
                              ))}
                            </span>
                          ) : <span className="text-subtle-foreground">—</span>}
                      </TableCell>
                      <TableCell className="text-right whitespace-nowrap">
                        <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[0.75rem]" onClick={() => navigate(`/k/${l.listing_id}?day=${l.cutoff}`)}>打开</Button>
                        {canRedraw(l, labels) && <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[0.75rem]" onClick={() => navigate(`/k/${l.listing_id}?redraw=${l.id}`)}>重画</Button>}
                        <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[0.75rem] text-subtle-foreground" onClick={() => void remove(l)}>删除</Button>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Section>
      </div>
    </div>
  )
}
