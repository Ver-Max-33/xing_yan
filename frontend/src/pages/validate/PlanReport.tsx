// 按交易计划 (design 2026-10-09 §6, §10): what a rule's signals earn when bought and sold by its plan — per signal day,
// with its interval, against stocks bought at random the same days the same way, trades by their ends, by year, by
// half, by the market's state — the top of the event study.
import { Link } from "react-router"
import { changeClass, codeOf, fmtPct } from "@/lib/format"
import { cn } from "@/lib/utils"
import { crossLine, entriesNote, type PlanPart, type PlanReport as Report, planLine, planSteps, planVerdict } from "@/lib/validate"
import { Empty, Kpi, KpiRow, Parts } from "../common"

const pct = (v: number | null | undefined, d = 1) => fmtPct(v ?? null, d)
const rate = (v: number | null | undefined) => fmtPct(v ?? null, 1, false)
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d))

function Interval({ ci }: { ci: [number | null, number | null] }) {
  return <>{pct(ci[0])} 到 {pct(ci[1])}</>
}

export function PlanReport({ p }: { p: Report }) {
  const blocked = (p.blocked?.limit ?? 0) + (p.blocked?.halt ?? 0)
  const verdict = planVerdict(p)
  const ends = Object.entries(p.reasons ?? {}).sort((a, b) => b[1] - a[1])
  return (
    <div data-testid="plan-report" className="border-b-4 border-border">
      <div className="border-b border-border px-6 py-3 text-[0.8125rem]">
        <span className="font-semibold">按交易计划</span>
        <span className="ml-2 text-muted-foreground"><Parts sep="；" parts={planSteps(p.plan)} /></span>
        <span className="ml-2 text-subtle-foreground">扣掉佣金、过户费、印花税和滑点；每个信号日算一个样本</span>
      </div>
      <KpiRow>
        <Kpi label="每个信号日平均" value={pct(p.expectancy)} tone={changeClass(p.expectancy)}
          sub={<>95% 区间 <Interval ci={p.ci} /> · {p.days} 个信号日</>} />
        <Kpi label="比同一天随机买的" value={p.vs_control == null ? "—" : pct(p.vs_control)} tone={changeClass(p.vs_control)}
          sub={<span className={verdict === "比随机买强" ? "text-success" : verdict === "比随机买差" ? "text-destructive" : undefined}>
            {verdict}{p.vs_control != null && <> · <Interval ci={p.vs_control_ci} /></>}</span>} />
        <Kpi label="胜率 · 盈亏比" value={`${rate(p.win)} / ${num(p.payoff)}`}
          sub={`平均赚 ${pct(p.avg_win)} · 平均亏 ${pct(p.avg_loss)} · 利润因子 ${num(p.pf)}`} />
        <Kpi label="平均持有" value={p.held == null ? "—" : `${p.held.toFixed(1)} 天`} sub={`每持有一天 ${pct(p.per_day, 2)}`} />
        <Kpi label="样本外每个信号日" value={pct(p.split.out.expectancy)} tone={changeClass(p.split.out.expectancy)}
          sub={`样本内 ${pct(p.split.in.expectancy)} · 比随机 ${pct(p.split.out.vs_control)} / ${pct(p.split.in.vs_control)}`} />
      </KpiRow>
      <div className="flex flex-col gap-1 border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground" data-testid="plan-line">
        <span className="text-foreground">{planLine(p)}</span>
        <span>
          {p.signals ?? 0} 个信号，买到 {p.bought ?? 0} 个
          {blocked > 0 && `（涨停买不到 ${p.blocked?.limit ?? 0}、停牌 ${p.blocked?.halt ?? 0}）`}；
          算进结果的 {p.finished ?? 0} 笔{(p.open ?? 0) > 0 && `，数据到头还在持有 ${p.open} 笔（不算）`}
          {(p.gone ?? 0) > 0 && `，停止交易按最后收盘卖出 ${p.gone} 笔`}
          {(p.no_room ?? 0) > 0 && `；${p.no_room} 笔的目标位不高于买入价，当作没有`}。
        </span>
        {ends.length > 0 && <span>怎么结束的：<Parts sep=" · " parts={ends.map(([k, v]) => `${p.labels[k] ?? k} ${rate(v)}`)} /></span>}
        <span>最大浮盈 / 浮亏（中位数）{pct(p.mfe)} / {pct(p.mae)}；随机对照每天 {p.per_day_control} 只，{p.control_days} 个信号日有对照
          {p.no_control_days > 0 && `，${p.no_control_days} 个没有（那天股票池里没有能买的）`}。</span>
        {p.cross && <span data-testid="plan-cross">{crossLine(p.cross)}。日线不知道越线是哪一分钟：按箱顶价（开盘就在箱顶上方的按开盘价）买，
          随机对照按它当天的平均价买。</span>}
        {p.few && <span className="text-warning">信号日少于 30 个：只看数字，不下结论。</span>}
      </div>
      {p.entries && <Entries p={p} />}
      <div className="@container"><div className="grid @min-[60rem]:grid-cols-2">
        <div className="border-b border-border px-6 py-4 @min-[60rem]:border-r @min-[60rem]:border-b-0">
          <h3 className="text-[0.875rem] font-semibold">逐年 <span className="font-normal text-subtle-foreground">每个信号日平均 · 胜率 · 利润因子 · 比随机</span></h3>
          <Years rows={p.years} />
          <Halves p={p} />
        </div>
        <div className="px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">分组 <span className="font-normal text-subtle-foreground">大盘在 200 日线上 / 下</span></h3>
          <GroupRows rows={p.groups.state ?? []} />
          {p.cross && <>
            <h3 className="mt-5 text-[0.875rem] font-semibold">越线那天收盘 <span className="font-normal text-subtle-foreground">成了信号、站上突破线、没站上（假突破）各自的平均</span></h3>
            <GroupRows rows={p.cross.kinds} />
          </>}
          {p.worst && p.worst.length > 0 && <>
            <h3 className="mt-5 text-[0.875rem] font-semibold">最差的 {p.worst.length} 笔 <span className="font-normal text-subtle-foreground">按交易计划的收益</span></h3>
            <Worst rows={p.worst} labels={p.labels} />
          </>}
        </div>
      </div></div>
    </div>
  )
}

function Entries({ p }: { p: Report }) {
  const e = p.entries!
  return (
    <div className="border-b border-border px-6 py-4" data-testid="plan-entries">
      <h3 className="text-[0.875rem] font-semibold">买法对比 <span className="font-normal text-subtle-foreground">同一条规则、同样的卖法，只换买入的时点</span></h3>
      <table className="mt-2 w-full text-[0.875rem]">
        <thead className="text-[0.8125rem] text-muted-foreground">
          <tr className="h-8 border-b border-border [&>th]:px-1 [&>th]:font-normal [&>th]:whitespace-nowrap">
            <th className="text-left">买法</th><th className="text-right">信号日</th><th className="text-right">每个信号日平均</th>
            <th className="text-right">95% 区间</th><th className="text-right">比随机</th><th className="text-right">胜率</th>
            <th className="text-right">平均持有</th></tr>
        </thead>
        <tbody>
          {e.rows.map((r) => (
            <tr key={r.entry} className="h-8 border-b border-border/60">
              <td className="whitespace-nowrap">{r.label}{r.entry === p.plan.entry && <span className="ml-1.5 text-[0.75rem] text-subtle-foreground">本规则</span>}
                {r.few && <span className="ml-1.5 text-[0.75rem] text-subtle-foreground">样本少</span>}</td>
              <td className="num text-right">{r.days}</td>
              <td className={cn("num text-right", changeClass(r.expectancy))}>{pct(r.expectancy)}</td>
              <td className="num text-right whitespace-nowrap">{r.ci ? <Interval ci={r.ci} /> : "—"}</td>
              <td className={cn("num text-right", changeClass(r.vs_control))}>{pct(r.vs_control)}</td>
              <td className="num text-right">{rate(r.win)}</td>
              <td className="num text-right whitespace-nowrap">{r.held == null ? "—" : `${r.held.toFixed(1)} 天`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-[0.8125rem] text-muted-foreground">{entriesNote(e.compared)}。越线买按日线近似：碰到箱顶就按箱顶价（开盘就在箱顶上方的按开盘价）。</p>
    </div>
  )
}

function Years({ rows }: { rows: Report["years"] }) {
  if (!rows.length) return <Empty>没有信号日</Empty>
  return (
    <table className="mt-2 w-full text-[0.875rem]" data-testid="plan-years">
      <thead className="text-[0.8125rem] text-muted-foreground">
        <tr className="h-8 border-b border-border [&>th]:px-1 [&>th]:font-normal [&>th]:whitespace-nowrap">
          <th className="text-left">年</th><th className="text-right">信号日</th><th className="text-right">笔数</th>
          <th className="text-right">平均</th><th className="text-right">胜率</th><th className="text-right">利润因子</th><th className="text-right">比随机</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.year} className="h-8 border-b border-border/60">
            <td>{r.year}</td><td className="num text-right">{r.days}</td><td className="num text-right">{r.finished}</td>
            <td className={cn("num text-right", changeClass(r.expectancy))}>{pct(r.expectancy)}</td>
            <td className="num text-right">{rate(r.win)}</td><td className="num text-right">{num(r.pf)}</td>
            <td className={cn("num text-right", changeClass(r.vs_control))}>{pct(r.vs_control)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Halves({ p }: { p: Report }) {
  const row = (name: string, x: PlanPart) => (
    <tr className="h-8 border-b border-border/60">
      <td className="whitespace-nowrap">{name}</td><td className="num text-right">{x.days}</td>
      <td className={cn("num text-right", changeClass(x.expectancy))}>{pct(x.expectancy)}</td>
      <td className="num text-right whitespace-nowrap"><Interval ci={x.ci} /></td>
      <td className={cn("num text-right", changeClass(x.vs_control))}>{pct(x.vs_control)}</td>
    </tr>
  )
  return (
    <table className="mt-4 w-full text-[0.875rem]" data-testid="plan-halves">
      <thead className="text-[0.8125rem] text-muted-foreground">
        <tr className="h-8 border-b border-border [&>th]:px-1 [&>th]:font-normal [&>th]:whitespace-nowrap">
          <th className="text-left">样本</th><th className="text-right">信号日</th><th className="text-right">平均</th>
          <th className="text-right">95% 区间</th><th className="text-right">比随机</th></tr>
      </thead>
      <tbody>{row(`样本内（到 ${p.split.oos_start}）`, p.split.in)}{row("样本外（运行前固定）", p.split.out)}</tbody>
    </table>
  )
}

function GroupRows({ rows }: { rows: (PlanPart & { key: string })[] }) {
  if (!rows.length) return <Empty>没有分组</Empty>
  return (
    <table className="mt-2 w-full text-[0.875rem]">
      <tbody>
        {rows.map((g) => (
          <tr key={g.key} className="h-8 border-b border-border/60">
            <td>{g.key}{g.few && <span className="ml-1.5 text-[0.75rem] text-subtle-foreground">样本少</span>}</td>
            <td className="num text-right">{g.days} 天</td>
            <td className={cn("num text-right", changeClass(g.expectancy))}>{pct(g.expectancy)}</td>
            <td className={cn("num text-right", changeClass(g.vs_control))}>比随机 {pct(g.vs_control)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Worst({ rows, labels }: { rows: NonNullable<Report["worst"]>; labels: Record<string, string> }) {
  return (
    <div className="mt-2 flex flex-col" data-testid="plan-worst">
      {rows.map((r) => (
        <Link key={`${r.listing_id}|${r.signal_date}`} to={`/k/${r.listing_id}?day=${r.signal_date}`}
          className="flex h-8 items-center gap-3 border-b border-border/60 px-1 text-[0.875rem] hover:bg-accent">
          <span className="max-w-32 min-w-0 flex-1 truncate">{r.name ?? codeOf(r.listing_id)}</span>
          <span className="num min-w-16 shrink-0 text-subtle-foreground">{codeOf(r.listing_id)}</span>
          <span className="num shrink-0 whitespace-nowrap text-subtle-foreground">{r.entry_date} → {r.exit_date}</span>
          <span className="shrink-0 truncate text-subtle-foreground">{labels[r.reason] ?? r.reason}</span>
          <span className={cn("num ml-auto min-w-16 shrink-0 text-right whitespace-nowrap", changeClass(r.ret))}>{pct(r.ret)}</span>
        </Link>
      ))}
    </div>
  )
}
