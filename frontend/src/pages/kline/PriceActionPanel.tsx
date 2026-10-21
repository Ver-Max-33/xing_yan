// 价格行为分析 on the K 线 page (design 2026-10-20 §6.1, dev branch): PA_Agent reads the stock's last 100 daily bars and
// says where it is now, where it may go, and where to buy and sell — one plan, the one left after the program's and the
// A-share checks, with what the AI first wrote and why each part changed (10-21). Before a run:
// what it would use and why it could not start; while it runs: its step and time; after: the result, the stock's analyses
// and how each plan was settled.
import { useMutation } from "@tanstack/react-query"
import { Loader2, Play, Square } from "lucide-react"
import { useEffect, useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { fmtPrice, shortDate } from "@/lib/format"
import {
  buyCondition, CYCLE_ZH, DIRECTION_ZH, isOrder, ORDER_ZH, type PAAnalysis, type PAPlan, paEndpoints, plain, PROGRAM_NODES, stageFacts,
  type TraceNode, tradeText, usePAHistory, usePAPreview, useStartPA,
} from "@/lib/pa"
import { cn } from "@/lib/utils"

const SHORT_ORDER = { 限价单: "挂限价", 突破单: "突破买", 市价单: "开盘买", 不下单: "不买" } as const
const STATUS_ZH = { running: "分析中", ok: "完成", failed: "没做成", cancelled: "已取消" } as const
const FIELD_ZH: Record<string, string> = {
  order_type: "买法", entry_price: "买入价", stop_loss_price: "止损", take_profit_price: "第一止盈", take_profit_price_2: "第二止盈",
}

const when = (iso: string) => `${iso.slice(5, 10)} ${iso.slice(11, 16)}`
const price = (v: number | null | undefined) => fmtPrice(v, "CN")
const duration = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`
}
const shown = (v: unknown) => (typeof v === "number" ? price(v) : v == null ? "—" : SHORT_ORDER[v as keyof typeof SHORT_ORDER] ?? String(v))
const side = (p: PAPlan) => (p.order_direction === "做空" ? "做空" : "买")
/** A plan in one line, as the AI first wrote it. */
const planText = (p: PAPlan) => `${p.order_direction === "做空" ? `做空（${SHORT_ORDER[p.order_type]}）` : ORDER_ZH[p.order_type]}：` +
  `${p.order_direction === "做空" ? "卖出" : "买入"} ${price(p.entry_price)}，止损 ${price(p.stop_loss_price)}，` +
  `第一止盈 ${price(p.take_profit_price)}，第二止盈 ${price(p.take_profit_price_2)}`

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-3">
      <div className="text-[0.8125rem] text-subtle-foreground">{title}</div>
      <div className="mt-1 text-[0.875rem] leading-6">{children}</div>
    </div>
  )
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex gap-2"><span className="w-16 shrink-0 text-[0.8125rem] text-subtle-foreground">{label}</span><span className="flex-1">{children}</span></div>
}

function PlanBox({ p }: { p: PAPlan | null }) {
  if (!p) return null
  const risk = isOrder(p) && p.entry_price != null && p.stop_loss_price != null ? p.entry_price - p.stop_loss_price : null
  const times = (tp: number | null) => (risk && tp != null && risk > 0 ? `（${((tp - p.entry_price!) / risk).toFixed(1)} 倍风险）` : "")
  return (
    <div className="rounded-md border border-border px-3 py-2 text-[0.8125rem] leading-6" data-testid="pa-plan">
      <div className="font-medium">{ORDER_ZH[p.order_type]}{p.order_direction === "做空" ? "（做空）" : ""}</div>
      {isOrder(p) && (
        <>
          <div className="num">买入 {price(p.entry_price)} · 止损 {price(p.stop_loss_price)}</div>
          <div className="num">第一止盈 {price(p.take_profit_price)}{times(p.take_profit_price)}</div>
          <div className="num">第二止盈 {price(p.take_profit_price_2)}{times(p.take_profit_price_2)}</div>
          <div className="text-muted-foreground">信心 {p.trade_confidence ?? "—"} · AI 估的胜率 {p.estimated_win_rate ?? "—"}%
            （AI 自己估的，不是统计出来的）</div>
        </>
      )}
    </div>
  )
}

/** `note`: PA's program-written warning — its own shorthand, folded here rather than under 风险提示; like the rest of the
 *  path its codes are said in Chinese (10-21). */
function Path({ nodes, note }: { nodes: TraceNode[]; note?: string }) {
  if (!nodes.length && !note) return null
  return (
    <details className="mt-3 text-[0.8125rem]" data-testid="pa-path">
      <summary className="cursor-pointer text-subtle-foreground">决策路径（{nodes.length} 个节点；「程序」是程序先判的，「AI 推翻」是 AI 改了程序的判断）</summary>
      {note && <div className="mt-1.5 leading-5 text-muted-foreground">程序的提示：{plain(note)}</div>}
      {nodes.map((n, i) => (
        <div key={`${n.node_id}-${i}`} className="mt-1.5 flex gap-2 leading-5">
          <span className="num w-9 shrink-0 text-subtle-foreground">{n.node_id}</span>
          <span className="flex-1">{n.question ? `${plain(n.question)} ` : ""}<b>{n.answer}</b>{n.reason ? `：${plain(n.reason)}` : ""}
            {n.overridden_by_ai && n.override_reason ? `（程序原判「${n.program_answer ?? "—"}」，推翻理由：${plain(n.override_reason)}）` : ""}</span>
          {PROGRAM_NODES.has(n.node_id) && <span className="h-5 shrink-0 rounded bg-muted px-1 text-[0.75rem] text-muted-foreground">程序</span>}
          {n.overridden_by_ai && <span className="h-5 shrink-0 rounded bg-warning/15 px-1 text-[0.75rem] text-warning">AI 推翻</span>}
        </div>
      ))}
    </details>
  )
}

function Result({ a }: { a: PAAnalysis }) {
  const marks = [a.demo && "演示数据，不对账", !a.demo && a.profile === "test" && "测试档没有复权，不对账",
    a.intraday === 1 && "盘中分析：第 0 天比分析那天早"].filter(Boolean)
  const header = (
    <>
      <div className="text-[0.875rem] font-medium">{when(a.created_at)} 分析<span className="ml-1 font-normal text-subtle-foreground">（用的是 {shortDate(a.day)} 收盘的数据）</span></div>
      <div className="mt-0.5 text-[0.8125rem] text-subtle-foreground">
        {a.model ?? "AI"}{a.cost != null ? ` · ¥${a.cost.toFixed(2)}` : ""}{a.latency_ms != null ? ` · 用时 ${duration(a.latency_ms)}` : ""}
        {marks.map((m) => <span key={m as string} className="ml-1 text-warning">· {m}</span>)}
      </div>
      <div className="mt-0.5 text-[0.75rem] text-subtle-foreground">文中 K1 指最新一根日线（{shortDate(a.day)}），K2 是前一根，往前依次类推。</div>
    </>
  )
  if (a.status !== "ok" || !a.stage1 || !a.stage2 || !a.final_plan) {
    return (
      <div className="py-3" data-testid="pa-result">
        {header}
        <p className={cn("mt-2 text-[0.875rem]", a.status === "failed" && "text-error")}>
          {a.status === "running" ? `分析中：${a.stage ?? "准备中"}` : a.status === "cancelled" ? "已取消" : `没做成：${a.error ?? "原因不明"}`}</p>
      </div>
    )
  }
  const s1 = a.stage1, s2 = a.stage2, p = a.final_plan, n = s2.next_cycle_prediction
  const changes = a.changes ?? []
  const toNo = changes.find((c) => c.field === "order_type" && c.after === "不下单")
  const t = a.trades?.final
  return (
    <div className="pb-3 pt-3" data-testid="pa-result">
      {header}
      <Section title="现在所处的阶段">
        <div>{CYCLE_ZH[s1.cycle_position] ?? s1.cycle_position} · {DIRECTION_ZH[s1.direction] ?? s1.direction}
          {s1.diagnosis_confidence != null && <span className="text-muted-foreground"> · 诊断信心 {s1.diagnosis_confidence}</span>}</div>
        {stageFacts(s1).map((f) => <Line key={f.label} label={f.label}>{f.text}</Line>)}
        {!!s1.support_levels?.length && <Line label="支撑">{s1.support_levels.join("；")}</Line>}
        {!!s1.resistance_levels?.length && <Line label="阻力">{s1.resistance_levels.join("；")}</Line>}
        {s1.htf_context && <div className="text-[0.8125rem] text-muted-foreground">{plain(s1.htf_context)}</div>}
      </Section>
      <Section title="后面可能的走势">
        {n && !n.unpredictable && n.cycle
          ? <div>{CYCLE_ZH[n.cycle] ?? n.cycle}{n.direction ? ` · ${DIRECTION_ZH[n.direction] ?? n.direction}` : ""}
              {n.probabilities?.[n.cycle] != null && <span className="text-muted-foreground">（它给的概率 {n.probabilities[n.cycle]}%）</span>}</div>
          : <div>看不出来</div>}
        {n?.reasoning && <div className="text-[0.8125rem] text-muted-foreground">{plain(n.reasoning)}</div>}
        <div className="text-[0.75rem] text-subtle-foreground">它自己说：这个方向判断实测只比抛硬币好一两个百分点，不用来决定买卖</div>
      </Section>
      <Section title="买点和卖点">
        <PlanBox p={p} />
        {isOrder(p) && changes.length > 0 && (
          <div className="mt-2 text-[0.8125rem]" data-testid="pa-changes">
            <div className="text-subtle-foreground">这是程序检查后的方案，和 AI 原来写的有 {changes.length} 处不同：</div>
            {changes.map((c, i) => (
              <div key={i} className="mt-1 leading-5">· {FIELD_ZH[c.field] ?? c.field}：AI 写的是 {shown(c.before)}，程序改成 {shown(c.after)}。{plain(c.reason)}</div>
            ))}
          </div>
        )}
        {!isOrder(p) && (
          <div className="mt-2">
            {toNo && a.ai_plan && <Line label={`AI 原想${side(a.ai_plan)}`}>{planText(a.ai_plan)}</Line>}
            <Line label="为什么不买">{toNo ? `程序没让${a.ai_plan ? side(a.ai_plan) : "买"}。${plain(toNo.reason)}` : plain(p.reasoning ?? "—")}</Line>
            {buyCondition(s1) && <Line label="怎样才买">{plain(buyCondition(s1)!)}</Line>}
            <div className="mt-1 text-[0.8125rem] text-muted-foreground">这次不买，图上只画最近的支撑和阻力；到哪儿、出现什么信号才考虑买，见「要盯的地方」。</div>
          </div>
        )}
        {isOrder(p) && p.invalidation_condition && <div className="mt-1"><Line label="失效条件">{plain(p.invalidation_condition)}</Line></div>}
        {isOrder(p) && t && (
          <div className="mt-1 text-[0.8125rem]">
            <Line label="对账">第一止盈：{tradeText(t.tp1)}；第二止盈：{tradeText(t.tp2)}</Line>
          </div>
        )}
      </Section>
      {p.risk_assessment && <Section title="风险提示">{plain(p.risk_assessment)}</Section>}
      {!!p.watch_points?.length && <Section title="要盯的地方">{p.watch_points.map((w) => <div key={w}>· {plain(w)}</div>)}</Section>}
      <Path nodes={[...(s1.gate_trace ?? []), ...(s2.decision_trace ?? [])]} note={s1.risk_warning} />
    </div>
  )
}

function Running({ a }: { a: PAAnalysis }) {
  const cancel = useMutation({ mutationFn: paEndpoints.cancel, onError: (e) => toast.error(`取消不了：${String(e)}`) })
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return (
    <div data-testid="pa-running">
      <div className="flex items-center gap-2 text-[0.8125rem]">
        <Loader2 className="size-3.5 animate-spin" />
        <span className="flex-1">{a.stage ?? "准备中"} · 已用 {duration(now - Date.parse(a.created_at))}</span>
        <Button size="sm" variant="ghost" className="h-7 px-2" disabled={cancel.isPending || !!a.stage?.startsWith("取消中")}
          onClick={() => cancel.mutate(a.id)}>
          <Square className="size-3.5" />取消</Button>
      </div>
      <p className="mt-1 text-[0.75rem] text-subtle-foreground">两步要几分钟；可以先去看别的，做完会存在这里。</p>
    </div>
  )
}

/** `onPlanFor`: told the analysis on screen (its plan's lines go on the chart), or null when none is shown. */
export function PriceActionPanel({ listingId, onPlanFor }: { listingId: string; onPlanFor?: (a: PAAnalysis | null) => void }) {
  const preview = usePAPreview(listingId)
  const history = usePAHistory(listingId)
  const start = useStartPA()
  const [pick, setPick] = useState<string | null>(null)
  const list = history.data ?? []
  const a = list.find((x) => x.id === pick) ?? list[0]
  const live = list.find((x) => x.status === "running")
  const p = preview.data
  useEffect(() => { onPlanFor?.(a?.status === "ok" ? a : null) }, [a, onPlanFor])
  useEffect(() => () => onPlanFor?.(null), [onPlanFor])
  const go = () => start.mutate(listingId, {
    onSuccess: (r) => {
      setPick(r.id)   // the new one, or the one already made from this input (final review 6)
      if (r.reused) toast.info("这一天的 K 线、规则、模型和上一次方案都没变，结果就是已有的那次，没有再调用 AI")   // (10-21)
    },
    onError: (e) => toast.error(`开始不了：${String(e)}`),
  })
  if (p && !p.supported) return <p className="px-4 py-8 text-center text-[0.875rem] text-muted-foreground">价格行为分析只支持 A 股</p>
  const fix = p?.reason?.includes("数据使用") ? "/settings?s=data" : p?.reason && /API key|预算|价格表|模型/.test(p.reason) ? "/settings?s=ai" : null
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-4" data-testid="pa-panel">
        {a ? <Result a={a} /> : (
          <div className="py-8 text-center text-[0.875rem] text-muted-foreground">
            {history.isLoading ? "读取…" : "还没有分析过这只股票。它读最近 100 根日线，先判断现在处在哪个阶段、后面可能怎么走，再给出买点和卖点（不买也说为什么）。"}
          </div>
        )}
        {list.length > 1 && (
          <div className="border-t border-border py-2">
            <div className="text-[0.8125rem] text-subtle-foreground">这只股票的分析 <span className="num">{list.length}</span> 次（同一天只有第一次进评估）</div>
            {list.slice(0, 20).map((x) => (
              <button key={x.id} type="button" onClick={() => setPick(x.id)}
                className={cn("flex h-7 w-full items-center gap-2 rounded px-1 text-left text-[0.8125rem] hover:bg-accent", x.id === a?.id && "bg-accent")}>
                <span className="num">{when(x.created_at)}</span>
                <span>{x.status === "ok" && x.final_plan
                  ? `${CYCLE_ZH[x.stage1?.cycle_position ?? "unknown"] ?? ""} · ${SHORT_ORDER[x.final_plan.order_type]}` : STATUS_ZH[x.status]}</span>
                <span className="num ml-auto truncate text-muted-foreground">{x.status === "ok" && isOrder(x.final_plan) ? tradeText(x.trades?.final.tp1) : ""}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 border-t border-border px-4 py-2">
        {live ? <Running a={live} /> : (
          <>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={go} disabled={start.isPending || !p?.ready}>
                {start.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}{a ? "再分析一次" : "分析"}</Button>
              <span className="text-[0.75rem] text-subtle-foreground">
                {p ? `${p.model} · 高峰时约 ¥1–1.5 一次，夜里和周末减半（实际花费见结果）· 用的是 ${shortDate(p.day)} 收盘的数据` : "读取…"}</span>
            </div>
            {p && !p.ready && (
              <p className="mt-1 text-[0.75rem] text-warning" data-testid="pa-not-ready">{p.reason?.replace(/（设置 → [^）]*）$/, "")}
                {fix && <>（<Link to={fix} className="underline">{fix.endsWith("ai") ? "设置 → AI" : "设置 → 数据使用"}</Link>）</>}</p>
            )}
            <p className="mt-1.5 text-[0.75rem] text-subtle-foreground">
              会把这 100 根日线的数字（开高低收、成交量、涨跌停）发给模型；「AI 研究」只发整理过的事实，这里不一样。
              {p?.demo ? "演示数据上的结果不对账。" : p && !p.adjusted ? "测试档没有复权，用的是不复权价，结果不对账。" : ""}</p>
          </>
        )}
      </div>
    </div>
  )
}
