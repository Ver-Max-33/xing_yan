// 验证 (spec §9, designs 08 and 18): 事件研究 — every signal of a rule over the years, bought at the next day's open,
// what followed it against the market's benchmark, same-day signals counted as one sample, the 样本外 against the
// 样本内, the groups and the worst cases; 条件体检 — what dropping each condition does, each parameter scanned on its
// own, and one condition tested alone against random entries; 组合回测 — a book of each market run on the rule's signals
// with its trading rules, costs and cash ledger; 稳健性 — the parameters' grid as a heatmap of books and the costs
// raised. Runs go in the background one at a time and are kept with the rule version and the data they ran on.
// Results are suggestions: nothing here changes a rule.
import { useQueryClient } from "@tanstack/react-query"
import { Download, Loader2, Play } from "lucide-react"
import { useMemo, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"
import { RunNow } from "@/components/RunNow"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import type { Market } from "@/lib/api"
import { exportRows } from "@/lib/datause"
import { changeClass, codeOf, fmtPct, indexName, MARKET_NAME, marketNames, shortDate } from "@/lib/format"
import { type RuleSummary, useRules } from "@/lib/rules"
import { usePersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import {
  type BookBody, type BookReport, type Checkup, type CourseCheck, DEFAULT_BOOK, drift, FEW, type Group, ledgerCSV, navCSV, type Robust, rowsCSV, type Scan, type Single,
  type Summary, tradesCSV, useRunsOf, useStartValidation, useValidateSetup, type ValidateKind, type ValidateRun, validateEndpoints, VERDICT, winsOf,
  EXIT, measure,
} from "@/lib/validate"
import { Empty, Kpi, KpiRow, PageHeader, Parts } from "./common"
import { AttributionView } from "./validate/AttributionView"
import { PlanReport } from "./validate/PlanReport"
import { BookBar, BookView, RobustView } from "./validate/BookView"

type Tab = "event" | "checkup" | "backtest" | "robust"
const TABS: [Tab, string, string, string][] = [   // tab, its name, the button that runs it again, what it does
  ["event", "事件研究", "重新运行", "还没跑过这条规则的事件研究：把区间里的每个信号按次日开盘买入，看之后的收益和超额。"],
  ["checkup", "条件体检", "重新体检", "还没给这条规则做过体检：依次去掉每个条件、逐个扫描参数，看哪些条件真的有用。"],
  ["backtest", "组合回测", "重新回测", "还没给这条规则做过组合回测：按上面的交易设定，每个市场一本账，逐日撮合、记资金账本。"],
  ["robust", "稳健性", "重新检验", "还没做过稳健性检验：把标了 tune 的参数在网格上各跑一遍组合回测，再把成本放大到 2 倍、3 倍。"],
]
const FIRST: Record<Tab, string> = { event: "运行事件研究", checkup: "开始体检", backtest: "运行组合回测", robust: "开始检验" }
const KIND_NAME: Record<ValidateKind, string> = { event: "事件研究", checkup: "条件体检", backtest: "组合回测", robust: "稳健性检验", single: "单条件检验", calibrate: "校准" }
const pct = (v: number | null | undefined, d = 1) => fmtPct(v, d)
const rate = (v: number | null | undefined) => fmtPct(v, 1, false)

export default function ValidatePage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const tab: Tab = TABS.find(([k]) => k === params.get("tab"))?.[0] ?? "event"
  const [book, setBook] = usePersisted<BookBody>("validate.book", DEFAULT_BOOK) // the 交易设定 of 组合回测 and 稳健性
  const { data: rules = [] } = useRules()
  const usable = rules.filter((r) => !r.archived && !r.error)
  const rule = usable.find((r) => r.id === params.get("rule")) ?? usable.find((r) => r.enabled) ?? usable[0]
  // the markets and range you chose belong to the rule you chose them for
  const [pick, setPick] = useState<{ rule?: string; markets?: Market[]; start?: string; end?: string }>({})
  const mine = pick.rule === rule?.id ? pick : {}
  const choose = (p: typeof pick) => setPick({ ...mine, ...p, rule: rule?.id })
  const ms = (mine.markets ?? rule?.markets ?? []).filter((m) => rule?.markets.includes(m))
  const { data: setup } = useValidateSetup(ms)
  const start = mine.start ?? setup?.start ?? "", end = mine.end ?? setup?.end ?? ""
  // the latest run says what is going on (running, paused, cancelled, failed); the latest finished one stays in view
  const { runs, latest, active, shown: run, act } = useRunsOf(rule?.id, tab)
  const begin = useStartValidation()
  const busy = begin.isPending || active?.status === "queued" || active?.status === "running"
  const paused = active?.status === "paused"
  // the 交易设定 open at first only before a kind's first run (what was there when the tab opened: never folded under you)
  const [formOpenAsked, setFormOpen] = useState<boolean | null>(null)
  const [firstSeen, setFirstSeen] = useState<Record<string, boolean>>({})   // tab → whether it had a finished run when opened
  if (runs.isSuccess && firstSeen[tab] === undefined) setFirstSeen({ ...firstSeen, [tab]: !!runs.data?.some((r) => r.status === "done") })
  const formOpen = formOpenAsked ?? firstSeen[tab] === false
  const books = tab === "backtest" || tab === "robust"
  const [bookErrors, setBookErrors] = useState<string[]>([])   // 交易设定 fields holding a number they cannot take
  const held = books && bookErrors.length ? `交易设定里${bookErrors[0]}` : null
  const go = (kind: ValidateKind) => rule && begin.mutate(
    { kind, rule_id: rule.id, markets: ms, start, end, ...(kind === "backtest" || kind === "robust" ? book : {}) },
    { onError: (e) => toast.error(String(e)) })
  const testData = setup?.markets.some((m) => m.profile === "test")
  const exportCSV = async () => {
    if (!run || run.kind !== "event" || run.status !== "done") return
    const rows = await exportRows(qc, await validateEndpoints.rows(run.id), (r) => r.market)   // 数据使用 (§4.10)
    if (!rows) return
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([rowsCSV(rows, run.oos_start)], { type: "text/csv;charset=utf-8" }))
    a.download = `事件研究-${run.rule_id}-v${run.version}-${run.end}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const exportBook = async (what: "trades" | "ledger" | "nav") => {   // 组合回测's tables, each market in its own currency
    if (!run || run.kind !== "backtest" || run.status !== "done") return
    const csv = async <T extends { market: Market }>(rows: T[], write: (rows: T[]) => string) => {
      const kept = await exportRows(qc, rows, (r) => r.market)   // 数据使用 (§4.10): a market kept from export is left out
      return kept && write(kept)
    }
    const text = what === "trades" ? await csv(await validateEndpoints.trades(run.id), tradesCSV)
      : what === "ledger" ? await csv(await validateEndpoints.ledger(run.id), ledgerCSV) : await csv(await validateEndpoints.nav(run.id), navCSV)
    if (!text) return
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }))
    a.download = `组合回测-${{ trades: "成交", ledger: "资金账本", nav: "净值" }[what]}-${run.rule_id}-v${run.version}-${run.end}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-2">验证
        {/* when the toolbar is short of room the rule's name gives way first, then the note; the controls stay whole */}
        <select aria-label="规则" value={rule?.id ?? ""} onChange={(e) => setParams({ rule: e.target.value, tab })}
          className="no-drag h-7 max-w-[20rem] min-w-[7rem] rounded-md border border-input px-2 text-[0.875rem] font-normal">
          {usable.map((r) => <option key={r.id} value={r.id}>{r.enabled ? "● " : ""}{r.name} v{r.version}</option>)}
        </select>
        <span className="no-drag flex shrink-0 items-center gap-1 text-[0.8125rem] font-normal">
          <Input aria-label="开始日期" type="date" value={start} onChange={(e) => choose({ start: e.target.value })} className="h-7 w-[8.25rem] text-[0.8125rem]" />
          <span className="text-subtle-foreground">→</span>
          <Input aria-label="结束日期" type="date" value={end} onChange={(e) => choose({ end: e.target.value })} className="h-7 w-[8.25rem] text-[0.8125rem]" />
        </span>
        <span role="group" aria-label="市场" className="no-drag flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {(rule?.markets ?? []).map((m) => {
            const empty = setup?.markets.some((x) => x.market === m && !x.last)
            return (
              <button key={m} type="button" aria-pressed={ms.includes(m)} title={empty ? "这个数据档还没有这个市场的数据，运行时跳过" : undefined}
                onClick={() => choose({ markets: ms.includes(m) ? ms.filter((x) => x !== m) : [...ms, m] })}
                className={cn("rounded px-2 py-0.5", ms.includes(m) ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
                  empty && "line-through decoration-subtle-foreground")}>{MARKET_NAME[m]}</button>
            )
          })}
        </span>
        {(() => {
          const note = `入场：信号次日开盘 · 基准：${setup?.markets.map((m) => indexName(m.benchmark)).join(" / ") ?? "—"}`
          // a narrow toolbar (Windows keeps its buttons' room) leaves it out: squeezed it was cut to nothing
          return <span title={note} className="min-w-0 truncate text-[0.8125rem] font-normal text-muted-foreground @max-[56rem]:hidden">{note}</span>
        })()}
      </span>}>
        {tab === "event" && <Button size="sm" variant="ghost" onClick={exportCSV} disabled={run?.status !== "done"} aria-label="导出报告" title="导出报告">
          <Download className="size-4" /><span className="@max-[56rem]:hidden">导出报告</span></Button>}
        {tab === "backtest" && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="ghost" disabled={run?.status !== "done"} aria-label="导出" title="导出">
                <Download className="size-4" /><span className="@max-[56rem]:hidden">导出</span></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {([["trades", "成交"], ["ledger", "资金账本"], ["nav", "净值"]] as const).map(([k, label]) => (
                <DropdownMenuItem key={k} onSelect={() => void exportBook(k)}>{label}（CSV）</DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {!(runs.isSuccess && !latest) && (   // before the first run the page's own button starts it
          <Button size="sm" onClick={() => (paused ? void act("resume") : go(tab))} disabled={!rule || busy || !ms.length || (!paused && !!held)}
            title={paused ? "从暂停的地方接着算" : held ?? TABS.find(([k]) => k === tab)![2]} aria-label={paused ? "继续运行" : TABS.find(([k]) => k === tab)![2]}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
            <span className="@max-[56rem]:hidden">{paused ? "继续运行" : TABS.find(([k]) => k === tab)![2]}</span>
            <span className="hidden @max-[56rem]:inline">{paused ? "继续" : "运行"}</span>   {/* narrow: its tab says which */}
          </Button>
        )}
      </PageHeader>
      {active && (active.status === "queued" || active.status === "running" || active.status === "paused") && <RunNow run={active} title={KIND_NAME[active.kind]} after="页面上还是上一次跑完的结果" onAct={act} />}
      {testData && (
        <div className="border-b border-warning/30 bg-warning/10 px-6 py-2 text-[0.8125rem] text-warning">
          测试数据档：{setup?.markets.filter((m) => m.profile === "test").map((m) => MARKET_NAME[m.market]).join("、")}的数据不完整，结论只用于检查流程，不代表规则的真实表现。切换到正式档后重新运行。
        </div>
      )}
      <div className="flex items-center gap-3 border-b border-border px-6 py-2">
        <span role="tablist" className="flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] whitespace-nowrap">
          {TABS.map(([k, label]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => rule && setParams({ rule: rule.id, tab: k })}
              className={cn("rounded px-2.5 py-0.5", tab === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>{label}</button>
          ))}
        </span>
        {(active ?? run) && <RunLine active={active} run={run} rule={rule} />}
      </div>
      {books && <BookBar value={book} onChange={setBook} markets={ms} defaults={setup?.books ?? {}} ran={run?.status === "done" ? run.settings : null}
        open={formOpen} onOpen={setFormOpen} onErrors={setBookErrors} />}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!rule && <Empty>还没有规则。先在规则库里写一条。</Empty>}
        {rule && runs.isSuccess && !latest && (
          <div className="flex flex-col items-center gap-3 py-16 text-[0.875rem] text-muted-foreground">
            {TABS.find(([k]) => k === tab)![3]}
            <Button onClick={() => go(tab)} disabled={busy || !ms.length || !!held} title={held ?? undefined}><Play className="size-4" />{FIRST[tab]}</Button>
          </div>
        )}
        {run?.status === "done" && run.result && (tab === "event" ? <EventStudy s={(run.result as { summary: Summary }).summary} />
          : tab === "checkup" ? <CheckupView c={run.result as Checkup} rule={rule} />
          : tab === "backtest" ? <BookView books={(run.result as { books: Partial<Record<Market, BookReport>> }).books} runId={run.id} />
          : <RobustView r={run.result as Robust} markets={run.markets} />)}
        {active?.status === "error" && <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-error" role="alert">
          最近一次没跑完：{active.error}{run ? "。下面是上一次跑完的结果" : ""}</div>}
        {tab === "checkup" && rule && <div className="px-5 pb-6"><SingleTest markets={ms} start={start} end={end} checks={setup?.checks ?? []} /></div>}
        {runs.isError && <div className="px-6 py-10 text-center text-[0.875rem] text-error">读不出验证记录：{String(runs.error)}</div>}
      </div>
    </div>
  )
}

/** Which finished run the page shows (the latest one running, paused or cancelled has its strip above). */
function RunLine({ active, run, rule }: { active?: ValidateRun; run?: ValidateRun; rule?: RuleSummary }) {
  const newer = active && run && active.id !== run.id   // the latest is not the one shown
  const stale = run && run.kind !== "single" && rule && run.version !== rule.version
  return (
    // beside the tabs it gives way first: its parts go onto more lines, a date never broken
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[0.8125rem] text-subtle-foreground">
      {active?.status === "cancelled" && <span>最近一次已取消。</span>}
      {run && <>
        {newer ? <span>下面是上一次跑完的结果：</span> : null}
        <span className="num"><Parts sep=" · " parts={[`v${run.version}`, `${run.start} → ${run.end}`, marketNames(run.markets),
          `${run.created_at.slice(5, 16).replace("T", " ")} 运行`]} /></span>
        <span><Parts sep=" · " parts={[`样本内 ${run.start} → ${run.oos_start}`, `样本外 ${run.oos_start} → ${run.end}（运行前固定）`]} /></span>
        {stale && <span className="text-warning">规则已经改过（现在是 v{rule.version}），重新运行看新版本</span>}
      </>}
    </span>
  )
}

// ---- 事件研究 -----------------------------------------------------------------------------------------------------

/** 规则自己的判断 (spec §9.1): its lines and days, and how its signals ended. */
function JudgeLine({ j }: { j: NonNullable<Summary["judge"]> }) {
  const ends = (Object.keys(EXIT) as (keyof typeof EXIT)[]).filter((k) => j.exits[k]).map((k) => `${EXIT[k]} ${rate(j.exits[k])}`)
  return (
    <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground" data-testid="judge">
      按这条规则自己的判断：第二天开盘进去，{j.fail ? <>收盘跌破失效线 <span className="num text-foreground">{j.fail}</span> 算失败（再下一天开盘出来），</> : null}
      {j.target ? <>最高价碰到目标位 <span className="num text-foreground">{j.target}</span> 算成功，</> : null}最多看 {j.days} 个交易日。
      {j.n > 0 && <> {ends.join(" · ")}；中位数持有 <span className="num">{j.held ?? "—"}</span> 天。</>} 胜率、中位数都是到结束那天、扣掉同期大盘的超额。
    </div>
  )
}

function EventStudy({ s }: { s: Summary }) {
  const d = drift(s.split)
  const blocked = s.blocked.limit + s.blocked.halt
  const m = s.metric === "plan" ? "20 日" : measure(s)   // by the plan: the report on top, the 20-day figures under it
  return (
    <div data-testid="event-study">
      {s.plan && <PlanReport p={s.plan} />}
      {s.plan && <div className="border-b border-border px-6 pt-3 text-[0.8125rem] font-semibold">信号之后 1 到 60 日 <span className="font-normal text-subtle-foreground">第二天开盘买、扣掉同期大盘的超额，不按交易计划卖</span></div>}
      <KpiRow>
        <Kpi label="信号数" value={s.signals.toLocaleString()} sub={`有效样本 ${s.effective}（同日信号已聚类）`} />
        {s.effective < FEW   // a few samples: how many of them, not a rate that claims more (spec §9.1)
          ? <Kpi label={`${m}胜了几个`} value={winsOf(s.win, s.effective)} sub={<span className="text-warning">胜率 {rate(s.win)} · 样本少，不下结论</span>} />
          : <Kpi label={`${m}胜率`} value={rate(s.win)} sub="超额收益 > 0 的比例" />}
        <Kpi label={`${m}超额 · 中位数`} value={pct(s.median)} tone={changeClass(s.median)}
          sub={`四分位 ${pct(s.q25)} ~ ${pct(s.q75)}${s.mean != null ? ` · 平均 ${pct(s.mean)}` : ""}`} />
        <Kpi label="最大浮盈 / 浮亏 · 平均" value={`${pct(s.mfe)} / ${pct(s.mae)}`} sub="持有 20 日内" />
        {s.split.out.effective < FEW || s.split.in.effective < FEW
          ? <Kpi label="样本外胜了几个" value={winsOf(s.split.out.win, s.split.out.effective)}
              sub={<span className="text-warning">样本内 {winsOf(s.split.in.win, s.split.in.effective)} · 样本少，不比较</span>} />
          : <Kpi label="样本外胜率" value={rate(s.split.out.win)} sub={<span className={d === "有衰减" ? "text-warning" : undefined}>
              样本内 {rate(s.split.in.win)}{d ? `，${d}` : ""}</span>} />}
      </KpiRow>
      {s.judge && <JudgeLine j={s.judge} />}
      {(s.few || blocked > 0 || s.gone > 0) && (
        <div className="flex flex-col gap-1 border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground">
          {s.few && <span className="text-warning">有效样本少于 30 个：只看数字，不下结论。</span>}
          {blocked > 0 && <span>{blocked} 个信号次日买不到（开盘涨停 {s.blocked.limit}、停牌 {s.blocked.halt}），占 {rate(blocked / s.signals)}，没有算进收益。</span>}
          {s.gone > 0 && <span>{s.gone} 个信号之后 20 日内股票停止交易（退市或长期停牌），没有 20 日结果，单独列在导出的报告里。</span>}
        </div>
      )}
      {s.gap && s.gap.n > 0 && (
        <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground" data-testid="next-open">
          次日开盘（相对信号日收盘，复权后）：中位数 <span className={cn("num", changeClass(s.gap.median))}>{pct(s.gap.median, 2)}</span>，
          低开的样本占 <span className="num">{rate(s.gap.down)}</span>（{s.gap.n} 个样本，开盘买不到的也算）
        </div>
      )}
      {/* each pair side by side when there is room for both, one under the other when not */}
      <div className="@container"><div className="grid border-b border-border @min-[60rem]:grid-cols-2">
        <div className="border-b border-border px-6 py-4 @min-[60rem]:border-r @min-[60rem]:border-b-0">
          <h3 className="text-[0.875rem] font-semibold">持有期超额收益 <span className="font-normal text-subtle-foreground">中位数 · 灰色为四分位范围</span></h3>
          <Horizons rows={s.horizons} />
        </div>
        <div className="px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">{m}超额收益分布 <span className="font-normal text-subtle-foreground">每格 2% · {s.effective} 个样本</span></h3>
          <Histogram h={s.hist} median={s.median} />
        </div>
      </div></div>
      <div className="@container"><div className="grid @min-[60rem]:grid-cols-2">
        <div className="border-b border-border px-6 py-4 @min-[60rem]:border-r @min-[60rem]:border-b-0">
          <h3 className="text-[0.875rem] font-semibold">分组表现 <span className="font-normal text-subtle-foreground">{s.metric === "judge" ? "按规则自己的判断" : "20 日"}</span></h3>
          <Groups groups={[...s.groups.market, ...s.groups.year.map((g) => ({ ...g, key: `${g.key} 年` })), ...s.groups.state,
            ...s.groups.industry.slice(0, 8)]} />
        </div>
        <div className="px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">最差的 {s.worst.length} 个 <span className="font-normal text-subtle-foreground">按{s.metric === "judge" ? "规则自己的判断" : " 20 日"}超额</span></h3>
          <Worst rows={s.worst} />
        </div>
      </div></div>
    </div>
  )
}

function Horizons({ rows }: { rows: Summary["horizons"] }) {
  const vals = rows.flatMap((r) => [r.q25, r.q75, r.median]).filter((v): v is number => v != null)
  if (!vals.length) return <Empty>还没有可算的持有期</Empty>
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals)
  const y = (v: number) => 180 - ((v - lo) / (hi - lo || 1)) * 160
  const w = 560 / rows.length
  return (
    <svg viewBox="0 0 600 210" className="mt-2 w-full max-w-[48rem]" role="img" aria-label="各持有期的超额收益">
      <line x1="30" x2="590" y1={y(0)} y2={y(0)} className="stroke-border" strokeDasharray="3 3" />
      {rows.map((r, i) => {
        const cx = 30 + w * i + w / 2
        return (
          <g key={r.h}>
            {r.q25 != null && r.q75 != null && <rect x={cx - 18} width="36" y={y(r.q75)} height={Math.max(1, y(r.q25) - y(r.q75))} className="fill-accent" />}
            {r.median != null && <>
              <line x1={cx - 18} x2={cx + 18} y1={y(r.median)} y2={y(r.median)} className="stroke-foreground" strokeWidth="2" />
              <text x={cx} y={y(r.q75 ?? r.median) - 6} textAnchor="middle" className="num fill-muted-foreground text-[0.75rem]">{pct(r.median)}</text>
            </>}
            <text x={cx} y="204" textAnchor="middle" className="fill-subtle-foreground text-[0.75rem]">{r.h} 日{r.n ? "" : "（无）"}</text>
          </g>
        )
      })}
    </svg>
  )
}

function Histogram({ h, median }: { h: Summary["hist"]; median: number | null }) {
  if (!h.counts.length) return <Empty>还没有样本</Empty>
  const top = Math.max(...h.counts), w = 560 / h.counts.length
  const x = (v: number) => 30 + ((v - h.lo) / h.step) * w
  return (
    <svg viewBox="0 0 600 210" className="mt-2 w-full max-w-[48rem]" role="img" aria-label="20 日超额收益的分布">
      {h.counts.map((n, i) => {
        const left = h.lo + i * h.step
        return <rect key={i} x={x(left) + 1} width={Math.max(1, w - 2)} y={180 - (n / top) * 160} height={(n / top) * 160}
          className={left + h.step / 2 >= 0 ? "fill-up" : "fill-down"}><title>{`${pct(left, 0)} ~ ${pct(left + h.step, 0)}：${n}`}</title></rect>
      })}
      {median != null && <>
        <line x1={x(median)} x2={x(median)} y1="12" y2="180" className="stroke-foreground" />
        <text x={x(median) + 4} y="20" className="num fill-foreground text-[0.75rem]">中位数 {pct(median)}</text>
      </>}
      {[0, Math.floor(h.counts.length / 2), h.counts.length].map((i, k) => (   // the ends anchored inward: never cut at the edge
        <text key={i} x={x(h.lo + i * h.step)} y="200" textAnchor={k === 0 ? "start" : k === 2 ? "end" : "middle"}
          className="num fill-subtle-foreground text-[0.75rem]">{pct(h.lo + i * h.step, 0)}</text>
      ))}
    </svg>
  )
}

function Groups({ groups }: { groups: Group[] }) {
  return (
    <table className="mt-2 w-full text-[0.875rem]">
      <thead className="text-[0.8125rem] text-muted-foreground">
        <tr className="h-8 border-b border-border [&>th]:font-normal"><th className="text-left">分组</th><th className="text-right">信号</th>
          <th className="text-right">有效</th><th className="text-right">胜率</th><th className="text-right">超额中位数</th></tr>
      </thead>
      <tbody>
        {groups.map((g) => (
          <tr key={String(g.key)} className={cn("h-8 border-b border-border/60", g.effective < FEW && "text-subtle-foreground")}>
            <td>{g.key}{g.effective < FEW && <span className="ml-1.5 text-[0.75rem]">样本少</span>}</td>
            <td className="num text-right">{g.signals}</td><td className="num text-right">{g.effective}</td>
            <td className="num text-right">{g.effective < FEW ? winsOf(g.win, g.effective) : rate(g.win)}</td>
            <td className={cn("num text-right", g.effective >= FEW && changeClass(g.median))}>{pct(g.median)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Worst({ rows }: { rows: Summary["worst"] }) {
  if (!rows.length) return <Empty>没有样本</Empty>
  return (
    <div className="mt-2 flex flex-col" data-testid="worst-cases">
      {rows.map((r) => (
        <Link key={`${r.listing_id}|${r.signal_date}`} to={`/k/${r.listing_id}?day=${r.signal_date}`}
          className="flex h-8 items-center gap-3 border-b border-border/60 px-1 text-[0.875rem] hover:bg-accent">
          <span className="max-w-32 min-w-0 flex-1 truncate">{r.name ?? codeOf(r.listing_id)}</span>
          <span className="num min-w-16 shrink-0 text-subtle-foreground">{codeOf(r.listing_id)}</span>
          <span className="num shrink-0 whitespace-nowrap text-subtle-foreground">{r.signal_date}</span>
          <span className={cn("num ml-auto min-w-20 shrink-0 text-right whitespace-nowrap", changeClass(r.x20))}>{pct(r.x20)}</span>
          <span className="num min-w-20 shrink-0 text-right whitespace-nowrap text-subtle-foreground" title="20 日内最大浮亏">{pct(r.mae)}</span>
        </Link>
      ))}
    </div>
  )
}

// ---- 条件体检 -----------------------------------------------------------------------------------------------------
function CheckupView({ c, rule }: { c: Checkup; rule?: RuleSummary }) {
  const plan = c.metric === "plan"   // judged by the trade plan's expectancy (2026-10-11); older runs by win rates
  const [param, setParam] = useState(c.scans[0]?.param)
  const scan = c.scans.find((s) => s.param === param) ?? c.scans[0]
  return (
    // 阈值扫描 beside the tables when there is room for both, under them when not (the tables need the width)
    <div className="@container" data-testid="checkup"><div className="grid @min-[80rem]:grid-cols-[minmax(0,1fr)_26.25rem]">
      <div className="border-b border-border px-6 py-4 @min-[80rem]:border-r @min-[80rem]:border-b-0">
        <h3 className="text-[0.875rem] font-semibold">逐条消融 <span className="font-normal text-subtle-foreground">
          去掉一个条件重跑事件研究 · 现在：{c.base.signals} 个信号，{plan
            ? <>按交易计划每个信号日平均 样本内 {pct(c.base.mean_in)} / 样本外 {pct(c.base.mean_out)}</>
            : <>{measure(c)}胜率 样本内 {rate(c.base.win_in)} / 样本外 {rate(c.base.win_out)}</>} · 本次比较了 {c.compared} 个变体</span></h3>
        {c.ablation.length === 0 ? <Empty>这条规则除了事件本身没有别的条件</Empty> : (
          <div className="overflow-x-auto"><table className="mt-2 w-full text-[0.875rem]" data-testid="ablation">
            <thead className="text-[0.8125rem] text-muted-foreground">
              <tr className="h-8 border-b border-border [&>th]:whitespace-nowrap [&>th]:px-1 [&>th]:font-normal"><th className="text-left">条件</th><th className="text-right">去掉后信号</th>
                {plan
                  ? <><th className="text-right">平均变化 · 内</th><th className="text-right">平均变化 · 外</th><th className="text-right">胜率变化 · 外</th></>
                  : <><th className="text-right">胜率变化 · 内</th><th className="text-right">胜率变化 · 外</th><th className="text-right">超额中位数变化 · 外</th></>}
                <th className="pl-4 text-left">结论</th></tr>
            </thead>
            <tbody>
              {c.ablation.map((a) => {
                const [label, tone] = VERDICT[a.verdict]
                return (
                  <tr key={a.condition} className={cn("h-9 border-b border-border/60", a.verdict === "有害" && "bg-warning/5")}>
                    <td className="max-w-[15rem] truncate" title={a.text}><span className="text-subtle-foreground">{a.condition}</span> {a.text}</td>
                    <td className="num text-right">{a.signals.toLocaleString()}</td>
                    {plan
                      ? <><td className="num text-right">{pts(a.d_mean_in ?? null)}</td><td className="num text-right">{pts(a.d_mean_out ?? null)}</td>
                        <td className="num text-right">{pts(a.d_win_out)}</td></>
                      : <><td className="num text-right">{pts(a.d_win_in)}</td><td className="num text-right">{pts(a.d_win_out)}</td>
                        <td className="num text-right">{pct(a.d_median_out)}</td></>}
                    <td className={cn("whitespace-nowrap pl-4", tone)}
                      title={a.verdict === "多余" ? `去掉它，样本内外的${plan ? "平均收益" : "胜率"}变化都在误差以内：看不出它有没有用，不等于它没用。样本更多时再看。` : undefined}>{label}</td>
                  </tr>
                )
              })}
            </tbody>
          </table></div>
        )}
        <p className="mt-2 text-[0.8125rem] text-subtle-foreground">变化 = 去掉该条件后 − 现在（百分点）{plan ? "，平均是按交易计划买卖、每个信号日的平均收益（扣费后）" : ""}。负数说明这个条件在帮忙；样本内外方向一致且超过标准误才下结论。结果只是建议，不会自动改规则。</p>
        <h3 className="mt-6 text-[0.875rem] font-semibold">还缺什么条件 <span className="font-normal text-subtle-foreground">候选条件归因：每个特征按信号当天的值分五组，看{plan ? "按交易计划的平均收益" : " 20 日超额"}从低到高</span></h3>
        {c.attribution ? <AttributionView a={c.attribution} rule={rule} metric={measure(c)} by={plan ? "mean" : "median"} />
          : <p className="mt-1 text-[0.8125rem] text-subtle-foreground">这次体检在归因之前跑的，重新体检就有。</p>}
      </div>
      <div className="px-5 py-4">
        <div className="flex items-center">
          <h3 className="text-[0.875rem] font-semibold">阈值扫描</h3>
          {c.scans.length > 0 && (
            <select aria-label="参数" value={scan?.param} onChange={(e) => setParam(e.target.value)}
              className="ml-auto h-7 rounded-md border border-input px-2 text-[0.8125rem]">
              {c.scans.map((s) => <option key={s.param} value={s.param}>{s.param}（现在 {s.current}）</option>)}
            </select>
          )}
        </div>
        {scan ? <ScanChart s={scan} plan={plan} /> : <Empty>这条规则没有带范围的数值参数可扫</Empty>}
      </div>
    </div></div>
  )
}

const pts = (v: number | null) => (v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}`)

function ScanChart({ s, plan = false }: { s: Scan; plan?: boolean }) {
  const p = s.points
  const [kin, kout] = plan ? (["mean_in", "mean_out"] as const) : (["win_in", "win_out"] as const)   // by the plan: its expectancy
  const wins = p.flatMap((x) => [x[kin], x[kout]]).filter((v): v is number => v != null)
  if (!wins.length) return <Empty>扫描结果里没有可算的样本</Empty>
  const pad = plan ? 0.002 : 0.02
  const lo = Math.min(...wins) - pad, hi = Math.max(...wins) + pad, top = Math.max(...p.map((x) => x.signals), 1)
  const x = (i: number) => 30 + (i * 360) / Math.max(1, p.length - 1)
  const y = (v: number) => 170 - ((v - lo) / (hi - lo || 1)) * 150
  const line = (k: typeof kin | typeof kout) => p.map((q, i) => (q[k] == null ? null : `${x(i)},${y(q[k]!)}`)).filter(Boolean).join(" ")
  const cur = p.findIndex((q) => q.value === s.current)
  const plat = s.plateau ? [p.findIndex((q) => q.value === s.plateau![0]), p.findIndex((q) => q.value === s.plateau![1])] : null
  const few = p.filter((q) => q.few).map((q) => q.value)
  return (
    <div data-testid="threshold-scan">
      <svg viewBox="0 0 400 200" className="mt-2 w-full max-w-[36rem]" role="img" aria-label={`参数 ${s.param} 的扫描`}>
        {plat && <rect x={x(plat[0]) - 6} width={x(plat[1]) - x(plat[0]) + 12} y="16" height="154" className="fill-accent" />}
        {p.map((q, i) => <rect key={i} x={x(i) - 5} width="10" y={170 - (q.signals / top) * 60} height={(q.signals / top) * 60} className="fill-muted" />)}
        <polyline points={line(kin)} fill="none" className="stroke-muted-foreground" strokeWidth="1.5" />
        <polyline points={line(kout)} fill="none" className="stroke-primary" strokeWidth="1.5" />
        {cur >= 0 && <line x1={x(cur)} x2={x(cur)} y1="12" y2="170" className="stroke-foreground" />}
        {p.map((q, i) => <text key={i} x={x(i)} y="188" textAnchor={i === 0 && p.length > 1 ? "start" : i === p.length - 1 && p.length > 1 ? "end" : "middle"}
          className="num fill-subtle-foreground text-[0.6875rem]">{q.value}</text>)}
      </svg>
      <div className="flex gap-4 text-[0.75rem] text-subtle-foreground">
        <span><span className="mr-1 inline-block h-0.5 w-3 bg-muted-foreground align-middle" />样本内{plan ? "平均" : "胜率"}</span>
        <span><span className="mr-1 inline-block h-0.5 w-3 bg-primary align-middle" />样本外{plan ? "平均" : "胜率"}</span>
        <span><span className="mr-1 inline-block size-2 bg-muted align-middle" />信号数</span>
      </div>
      <p className="mt-2 text-[0.8125rem] text-muted-foreground">
        {s.plateau ? `${s.plateau[0]} 到 ${s.plateau[1]} 之间样本外${plan ? "平均收益" : "胜率"}在当前值的标准误以内（平台区）；当前 ${s.current}${
          s.plateau[0] === s.current || s.plateau[1] === s.current ? " 在平台边上" : " 在平台里"}。` : `当前 ${s.current} 附近没有稳定的平台。`}
        {few.length > 0 && ` ${few.join("、")} 的有效样本不到 30 个，数字不可信。`}
      </p>
    </div>
  )
}

function SingleTest({ markets, start, end, checks }: { markets: Market[]; start: string; end: string; checks: CourseCheck[] }) {
  const [expr, setExpr] = useState("")
  const picked = checks.find((c) => c.expr === expr.trim())   // a stock-picking claim, as it was filled in
  const { active, shown: run, act } = useRunsOf(undefined, "single")
  const begin = useStartValidation()
  const busy = begin.isPending || active?.status === "queued" || active?.status === "running"
  const r = run?.status === "done" ? (run.result as Single) : null
  const verdict = useMemo(() => {
    if (!r || r.events.win == null || r.random.win == null) return null
    if (r.events.few) return "有效样本少于 30 个：只看数字，不下结论。"
    const gap = r.events.win - r.random.win
    const by = (Math.abs(gap) * 100).toFixed(1)
    return Math.abs(gap) < 0.03 ? "单独使用和随机入场几乎没有差别。" : gap > 0 ? `单独使用比随机入场的胜率高 ${by} 个百分点。` : `单独使用比随机入场的胜率还低 ${by} 个百分点。`
  }, [r])
  return (
    <div className="mt-6 border-t border-border pt-4">
      <div className="flex items-center gap-3">
        <h3 className="text-[0.875rem] font-semibold">单独检验一个条件</h3>
        {checks.length > 0 && (
          <select aria-label="选股说法" value={picked?.id ?? ""} onChange={(e) => {
            const c = checks.find((x) => x.id === e.target.value)
            if (c) { setExpr(c.expr); begin.reset() }
          }} className="ml-auto h-7 max-w-[55%] rounded-md border border-input px-2 text-[0.8125rem]">
            <option value="">选股说法：选一条，公式就填好</option>
            {checks.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
        )}
      </div>
      <form className="mt-2 flex gap-2" onSubmit={(e) => {
        e.preventDefault()
        if (expr.trim()) begin.mutate({ kind: "single", expr: expr.trim(), markets, start, end })
      }}>
        <Input aria-label="条件公式" value={expr} onChange={(e) => { setExpr(e.target.value); begin.reset() }}
          placeholder="CROSS(MACD.DIF, MACD.DEA) AND MACD.DIF > 0" className="num h-8 text-[0.8125rem]" />
        <Button size="sm" variant="outline" type="submit" disabled={busy || !expr.trim()}>{busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}检验</Button>
      </form>
      {picked && <p className="mt-1 text-[0.8125rem] text-subtle-foreground" data-testid="single-look">看什么：{picked.look}</p>}
      {begin.error && <div className="mt-2 text-[0.8125rem] text-error" role="alert">{String(begin.error)}</div>}
      {active && (active.status === "queued" || active.status === "running" || active.status === "paused") && (
        <div className="-mx-4 mt-2 border-t border-border"><RunNow run={active} title={KIND_NAME[active.kind]} after="页面上还是上一次跑完的结果" onAct={act} testid="single-now" /></div>
      )}
      {active?.status === "error" && <div className="mt-2 text-[0.8125rem] text-error">{active.error}</div>}
      {r && (
        <div className="mt-3 text-[0.8125rem]" data-testid="single-result">
          <div className="text-subtle-foreground"><span className="num">{r.expr}</span> · {marketNames(run!.markets)} · {run!.start} → {run!.end} · {r.events.signals.toLocaleString()} 次</div>
          <div className="mt-2 grid grid-cols-2 gap-3">
            <div><div className="text-muted-foreground">20 日胜率</div><div className="num text-[1.1875rem]">{rate(r.events.win)}</div>
              <div className="text-subtle-foreground">随机入场 {rate(r.random.win)}</div></div>
            <div><div className="text-muted-foreground">20 日超额中位数</div><div className={cn("num text-[1.1875rem]", changeClass(r.events.median))}>{pct(r.events.median)}</div>
              <div className="text-subtle-foreground">样本外 {pct(r.events.split.out.median)}（{shortDate(run!.oos_start)} 起）</div></div>
          </div>
          {r.events.gap && r.random.gap && r.events.gap.n > 0 && (
            <div className="mt-2 text-subtle-foreground" data-testid="single-gap">
              次日低开：事件 <span className="num text-foreground">{rate(r.events.gap.down)}</span>（中位数 {pct(r.events.gap.median, 2)}），
              随机入场 <span className="num">{rate(r.random.gap.down)}</span>（中位数 {pct(r.random.gap.median, 2)}）
            </div>
          )}
          {verdict && <p className="mt-2 rounded-md border border-border bg-accent/40 px-3 py-2 text-muted-foreground">{verdict}</p>}
        </div>
      )}
    </div>
  )
}
