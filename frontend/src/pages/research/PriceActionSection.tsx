// 研究 → 价格行为 (design 2026-10-20 §5.4, §5.5, §6.3, dev branch): what the AI's own plans and the program's did, each
// beside a random control of 20 stocks traded the same way; whether its stage, direction and next-stage calls lined up
// with the 5- and 20-day excess; and every analysis. Each stock's first analysis of a day counts, made on real 正式档 data.
import { useMemo, useState } from "react"
import { Link } from "react-router"
import { changeClass, codeOf, fmtPct, shortDate } from "@/lib/format"
import {
  aiWouldHaveBought, CYCLE_ZH, DIRECTION_ZH, evaluationRows, type PAEvaluation, type PAGroup, type PlanStats, usePARecent,
} from "@/lib/pa"
import { cn } from "@/lib/utils"
import { Empty, Section } from "../common"

const SHORT_ORDER = { 限价单: "挂限价", 突破单: "突破买", 市价单: "开盘买", 不下单: "不买" } as const
const GROUP_TITLE = { program: "程序判的方向", direction: "AI 判的方向", cycle: "AI 判的阶段", next: "它预测的下一阶段方向",
  buy: "买还是不买" } as const
const keyName = (g: keyof typeof GROUP_TITLE, k: string | null) =>
  k == null ? "—" : g === "cycle" ? CYCLE_ZH[k] ?? k : g === "buy" ? k : DIRECTION_ZH[k] ?? k
const open = (lid: string) => `/k/${encodeURIComponent(lid)}?panel=research&tab=pa`
const pct = (v: number | null, digits = 2) => <span className={changeClass(v)}>{fmtPct(v, digits)}</span>
const th = "h-8 border-b border-border [&>th]:px-2 [&>th]:font-normal [&>th]:whitespace-nowrap"

function PlanTable({ ev, target, when }: { ev: PAEvaluation; target: "tp1" | "tp2"; when: "all" | "yes" | "no" }) {
  const plans = when === "all" ? ev.plans : ev.intraday[when]
  return (
    <table className="w-full max-w-[50rem] text-[0.875rem]" data-testid="pa-evaluation">
      <thead className="text-[0.8125rem] text-muted-foreground">
        <tr className={th}>
          <th className="text-left">方案</th><th className="text-right">分析</th><th className="text-right">有方案</th>
          <th className="text-right">买到的比例</th><th className="text-right">已结算</th><th className="text-right">先到止盈</th>
          <th className="text-right">收益均值（扣费）</th><th className="text-right">中位数</th><th className="text-right">平均几倍风险</th>
        </tr>
      </thead>
      <tbody>
        {evaluationRows(plans, target).map(({ label, row }: { label: string; row: PlanStats }, i) => (
          <tr key={i} className={cn("h-8 border-b border-border/60 [&>td]:px-2", i % 2 === 1 && "text-muted-foreground")}>
            <td className="whitespace-nowrap">{i % 2 === 1 ? <span className="pl-3">└ {label}</span> : label}</td>
            <td className="num text-right">{row.n}</td><td className="num text-right">{row.orders}</td>
            <td className="num text-right">{fmtPct(row.bought_share, 0, false)}</td><td className="num text-right">{row.settled}</td>
            <td className="num text-right">{fmtPct(row.win, 1, false)}</td>
            <td className="num text-right">{pct(row.ret_mean)}</td><td className="num text-right">{pct(row.ret_median)}</td>
            <td className="num text-right">{row.r_mean == null ? "—" : row.r_mean.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function GroupTable({ name, groups }: { name: keyof typeof GROUP_TITLE; groups: PAGroup[] }) {
  if (!groups.length) return null
  return (
    <table className="w-full max-w-[40rem] text-[0.875rem]">
      <thead className="text-[0.8125rem] text-muted-foreground">
        <tr className={th}>
          <th className="text-left">{GROUP_TITLE[name]}</th><th className="text-right">分析</th><th className="text-right">已结算</th>
          <th className="text-right">5 日超额均值</th><th className="text-right">中位数</th>
          <th className="text-right">20 日超额均值</th><th className="text-right">中位数</th>
        </tr>
      </thead>
      <tbody>
        {groups.map((g) => (
          <tr key={String(g.key)} className="h-8 border-b border-border/60 [&>td]:px-2">
            <td className="whitespace-nowrap">{keyName(name, g.key)}</td><td className="num text-right">{g.n}</td>
            <td className="num text-right">{g.settled}</td>
            <td className="num text-right">{pct(g.x5_mean)}</td><td className="num text-right">{pct(g.x5_median)}</td>
            <td className="num text-right">{pct(g.x20_mean)}</td><td className="num text-right">{pct(g.x20_median)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function PriceActionSection() {
  const { data, error } = usePARecent()
  const [target, setTarget] = useState<"tp1" | "tp2">("tp1")
  const [when, setWhen] = useState<"all" | "yes" | "no">("all")
  const counts = useMemo(() => {   // §5.5: a stock analysed often has overlapping windows; say how often
    const n = new Map<string, number>()
    for (const r of data?.records ?? []) n.set(r.listing_id, (n.get(r.listing_id) ?? 0) + 1)
    return n
  }, [data])
  if (error) return <Section title="价格行为"><div className="text-[0.875rem] text-error">读不出来：{String(error)}</div></Section>
  if (!data) return <Section title="价格行为"><Empty>读取…</Empty></Section>
  const ev = data.evaluation
  return (
    <>
      <Section title="价格行为评估" hint={<>
        每次分析之后，从分析后的第一个开盘起，按 A 股规则（T+1、涨跌停、盘中碰止损就卖）对账两份方案：程序检查后的方案（K 线页上显示的就是这份，照方案买时该用这一份），
        和 AI 原方案（AI 自己写的、没经过程序检查，只用来看程序的检查是帮了忙还是帮了倒忙）。每份配一组随机对照，同一天从沪深非 ST、上市满 60 个交易日的股票里抽 20 只，
        按同样的买法和距离算。只算每只股票同一个数据日的第一次分析；演示数据、测试档不算。</>}>
        {!ev.enough && <p className="mb-2 text-[0.8125rem] text-warning">样本还少，只看数字：程序检查后的方案结算满 30 个之前不下结论。</p>}
        <div className="mb-2 flex flex-wrap items-center gap-3 text-[0.8125rem]">
          <label className="flex items-center gap-1.5 text-muted-foreground">止盈
            <select aria-label="按哪个止盈算" value={target} onChange={(e) => setTarget(e.target.value as "tp1" | "tp2")}
              className="h-7 rounded-md border border-input px-2 text-foreground">
              <option value="tp1">到第一止盈就全卖</option><option value="tp2">到第二止盈才全卖</option>
            </select></label>
          <label className="flex items-center gap-1.5 text-muted-foreground">分析时间
            <select aria-label="盘中还是收盘后的分析" value={when} onChange={(e) => setWhen(e.target.value as "all" | "yes" | "no")}
              className="h-7 rounded-md border border-input px-2 text-foreground">
              <option value="all">全部</option><option value="no">收盘后</option><option value="yes">盘中</option>
            </select></label>
        </div>
        <PlanTable ev={ev} target={target} when={when} />
        <p className="mt-1 text-[0.8125rem] text-subtle-foreground">随机对照每次分析抽 20 只，先取这 20 只的平均，再按分析算均值和中位数，和上面的方案一次一个数对齐；买到的比例按抽到的股票算。</p>
        <div className="mt-4 flex flex-col gap-4">
          {(Object.keys(GROUP_TITLE) as (keyof typeof GROUP_TITLE)[]).map((g) => <GroupTable key={g} name={g} groups={ev.groups[g]} />)}
        </div>
        <p className="mt-2 text-[0.8125rem] text-subtle-foreground">超额：从第一个开盘起，第 5、20 个交易日对照沪深 300 的涨跌差，不管有没有方案。
          它自己说方向判断只比抛硬币好一两个百分点，这里看的是在 A 股上对不对得上。</p>
      </Section>
      <Section title="最近的价格行为分析" hint="在 K 线页右侧「AI 研究」→「价格行为」里生成；每次都记下，同一只股票分析了几次写在名字旁边（结算区间会重叠）。">
        {data.records.length === 0 ? <Empty>还没有分析。打开一只 A 股的 K 线，点「AI 研究」→「价格行为」。</Empty> : (
          <table className="w-full max-w-[60rem] text-[0.875rem]" data-testid="pa-records">
            <thead className="text-[0.8125rem] text-muted-foreground">
              <tr className={th}>
                <th className="text-left">分析</th><th className="text-left">股票</th><th className="text-left">数据日</th>
                <th className="text-left">方案</th><th className="text-left">状态</th><th className="text-right">5 日超额</th>
                <th className="text-right">20 日超额</th>
              </tr>
            </thead>
            <tbody>
              {data.records.map((r) => (
                <tr key={r.id} className="h-8 border-b border-border/60 [&>td]:px-2">
                  <td className="num whitespace-nowrap">{r.created_at.slice(5, 16).replace("T", " ")}</td>
                  <td className="whitespace-nowrap"><Link className="hover:underline" to={open(r.listing_id)}>
                    <span className="num">{codeOf(r.listing_id)}</span></Link>
                    <span className="ml-1 text-[0.8125rem] text-subtle-foreground">共 {counts.get(r.listing_id)} 次</span></td>
                  <td className="num whitespace-nowrap">{shortDate(r.day)}{r.intraday ? " 盘中" : ""}</td>
                  <td className="whitespace-nowrap">{r.final_plan ? SHORT_ORDER[r.final_plan.order_type] : "—"}
                    {aiWouldHaveBought(r) && <span className="text-subtle-foreground">（AI 原想买，程序没让）</span>}</td>
                  <td className={cn("whitespace-nowrap", r.status === "failed" && "text-error")}>
                    {r.status === "ok" ? (r.demo ? "演示数据" : !r.adjusted ? "测试档" : r.settled ? "已结算" : "待结算")
                      : { running: "分析中", failed: "没做成", cancelled: "已取消" }[r.status]}</td>
                  <td className="num text-right">{pct(r.x5)}</td><td className="num text-right">{pct(r.x20)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  )
}
