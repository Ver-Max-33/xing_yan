// 组合回测 and 稳健性 (spec §9.2, §9.3): the book a run asks for (positions, exits, each market's cash and costs), what it
// made against the market's benchmark with its drawdown, 样本内 / 样本外, the trades and what could not be done; the
// §6.5 grid as a heatmap of books, and the costs raised. Each market is a book of its own, in its own currency.
import { useQuery } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { Link } from "react-router"
import { ChevronUp, SlidersHorizontal } from "lucide-react"
import { Button } from "@/components/ui/button"
import { NumberField } from "@/components/NumberField"
import { Switch } from "@/components/ui/switch"
import type { Market } from "@/lib/api"
import { changeClass, codeOf, fmtPct, indexName, MARKET_NAME } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  type BookBody, bookChanged, type BookReport, type BookSettings, type Brief, type Costs, formSettings, type Part, type Rate, REASON, type Robust,
  settingsText, validateEndpoints,
} from "@/lib/validate"
import { Empty, Kpi, KpiRow } from "../common"

const CURRENCY: Record<Market, string> = { CN: "元", JP: "円" }
const pct = (v: number | null | undefined, d = 1) => fmtPct(v, d)
const rate = (v: number | null | undefined) => fmtPct(v, 1, false)
const money = (v: number, m: Market) => `${Math.round(v).toLocaleString()} ${CURRENCY[m]}`
/** A sentence whose dates never break across lines (「2023-08-」 / 「28」 reads as two). */
const Dated = ({ text }: { text: string }) => <>{text.split(/(\d{4}-\d{2}-\d{2})/).map((p, i) => (i % 2 ? <span key={i} className="whitespace-nowrap">{p}</span> : p))}</>

const COSTS: [Rate, string, boolean][] = [   // field, label, a share (shown in %)
  ["commission", "佣金", true], ["min_commission", "最低佣金", false], ["transfer", "过户费", true], ["stamp", "印花税（卖出）", true], ["slippage", "滑点", true],
]

/** 交易设定: positions and exits for every market, each market's cash and costs (its defaults unless changed). */
export function BookForm({ value, onChange, markets, defaults, onErrors }: {
  value: BookBody; onChange: (v: BookBody) => void; markets: Market[]; defaults: Partial<Record<Market, BookSettings>>
  onErrors?: (errors: string[]) => void   // a field holding a number it cannot take: what the run must say
}) {
  const b = value.book
  const set = (p: Partial<BookBody["book"]>) => onChange({ ...value, book: { ...b, ...p } })
  const wrong = useRef(new Map<string, string>())
  const report = (label: string) => (error: string | null) => {
    if (error) wrong.current.set(label, error)
    else wrong.current.delete(label)
    onErrors?.([...wrong.current].map(([k, e]) => `${k}${e}`))
  }
  return (
    <div className="flex flex-wrap items-start gap-x-6 gap-y-2 border-b border-border px-6 py-3 text-[0.8125rem]" data-testid="book-form">
      <label className="flex items-center gap-1.5">最多持仓
        <NumberField aria-label="最多持仓" rule={{ int: true, min: 1, max: 50 }} value={b.max_positions} className="h-7 w-16 text-[0.8125rem]"
          onValidity={report("最多持仓")} onChange={(mp) => set({ max_positions: mp!,
            weak_positions: b.weak_positions == null ? null : Math.min(b.weak_positions, mp!) })} /> 只（等权）</label>
      <label className="flex items-center gap-1.5" title="规则写了自己的判断（失效线、目标位、观察天数，规则页和事件研究里能看到）就按它离场：收盘跌破失效线次日开盘卖，盘中碰到目标位就卖，到天数收盘卖；这时下面的持有天数、跌破箱底和止盈不用。规则没写的按下面的设定">按规则自己的判断离场
        <Switch aria-label="按规则自己的判断离场" checked={b.own ?? true} onCheckedChange={(c) => set({ own: c })} /></label>
      <label className="flex items-center gap-1.5">持有
        <NumberField aria-label="持有天数" rule={{ int: true, min: 1, max: 250, optional: true }} value={b.hold} placeholder="不限"
          className="h-7 w-16 text-[0.8125rem]" onValidity={report("持有天数")} onChange={(hold) => set({ hold })} /> 个交易日</label>
      <label className="flex items-center gap-1.5">跌破箱底止损<Switch aria-label="跌破箱底止损" checked={b.box_stop} onCheckedChange={(c) => set({ box_stop: c })} /></label>
      <label className="flex items-center gap-1.5">移动止损
        <NumberField aria-label="移动止损" rule={{ above: 0, below: 100, scale: 100, optional: true }} value={b.trail} placeholder="关"
          className="h-7 w-16 text-[0.8125rem]" onValidity={report("移动止损")} onChange={(trail) => set({ trail })} />%</label>
      <label className="flex items-center gap-1.5">止盈
        <NumberField aria-label="止盈" rule={{ above: 0, below: 1000, scale: 100, optional: true }} value={b.target} placeholder="关"
          className="h-7 w-16 text-[0.8125rem]" onValidity={report("止盈")} onChange={(target) => set({ target })} />%</label>
      <label className="flex items-center gap-1.5" title="有新高再看高，直到没有高在日线上的研究读法：连续这么多个它自己的交易日收盘没创入场以来新高，次日开盘卖">不创新高
        <NumberField aria-label="不创新高天数" rule={{ int: true, min: 1, max: 250, optional: true }} value={b.stall} placeholder="关"
          className="h-7 w-16 text-[0.8125rem]" onValidity={report("不创新高天数")} onChange={(stall) => set({ stall })} /> 天就卖</label>
      <label className="flex items-center gap-1.5" title="随大势（简化成研究设定）：基准前一天收在 200 日线下时，新开仓最多到这个数，已经持有的不卖">大盘弱时最多
        <NumberField aria-label="大盘弱时的持仓上限" rule={{ int: true, min: 0, max: b.max_positions, optional: true }} value={b.weak_positions}
          placeholder="不变" className="h-7 w-16 text-[0.8125rem]" onValidity={report("大盘弱时的持仓上限")}
          onChange={(weak_positions) => set({ weak_positions })} /> 只</label>
      {markets.map((m) => {
        const d = defaults[m]
        if (!d) return null
        const c = { ...d.costs, ...value.costs[m] }
        const setCost = (k: Rate, v: number | null) => {
          const mine = { ...value.costs[m] } as Partial<Costs>
          if (v == null) delete mine[k]
          else mine[k] = v
          onChange({ ...value, costs: { ...value.costs, [m]: mine as Costs } })
        }
        return (
          <div key={m} className="flex basis-full flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground" data-testid={`book-costs-${m}`}>
            <span className="w-10 text-foreground">{MARKET_NAME[m]}</span>
            <label className="flex items-center gap-1.5">初始资金
              <NumberField aria-label={`${m} 初始资金`} rule={{ min: 10000, max: 1e12, optional: true }} value={value.cash[m] ?? d.cash}
                placeholder={String(d.cash)} className="h-7 w-28 text-[0.8125rem]" onValidity={report(`${MARKET_NAME[m]}初始资金`)}
                onChange={(v) => onChange({ ...value, cash: { ...value.cash, [m]: v ?? undefined } })} />{CURRENCY[m]}</label>
            {COSTS.map(([k, label, isShare]) => (
              <label key={k} className="flex items-center gap-1">{label}
                <NumberField aria-label={`${m} ${label}`} className="h-7 w-20 text-[0.8125rem]" value={c[k]}
                  rule={isShare ? { min: 0, max: 5, scale: 100, optional: true } : { min: 0, max: 1000, optional: true }}
                  onValidity={report(`${MARKET_NAME[m]}${label}`)} onChange={(v) => setCost(k, v)} />
                {isShare ? "%" : CURRENCY[m]}</label>
            ))}
            <span className="text-subtle-foreground">{d.t_plus_1 ? "T+1，" : ""}一手 {d.lot} 股</span>
          </div>
        )
      })}
      <p className="basis-full text-[0.75rem] text-subtle-foreground">
        信号次日开盘买入（从不按信号当天收盘成交）；跌破箱底、跌破失效线按收盘判断、次日开盘卖出；移动止损、止盈和目标位盘中触及就成交，开盘就越过的按开盘价；
        同一根同时碰到止损和止盈按止损算。涨跌停按日线判断：A 股按当天的涨跌停价，日股开盘即涨停（跌停）的那天买（卖）不进（出），是近似。</p>
    </div>
  )
}

// ---- 组合回测的结果 ---------------------------------------------------------------------------------------------------
function MarketTabs({ markets, value, onChange }: { markets: Market[]; value: Market; onChange: (m: Market) => void }) {
  if (markets.length < 2) return null
  return (
    <span role="tablist" aria-label="市场" className="flex items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem]">
      {markets.map((m) => (
        <button key={m} type="button" role="tab" aria-selected={value === m} onClick={() => onChange(m)}
          className={cn("rounded px-2.5 py-0.5", value === m ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>{MARKET_NAME[m]}</button>
      ))}
    </span>
  )
}

/** What the result below was computed with (the form above is what the next run asks for), and whether they differ. */
/** 交易设定, folded to what they say (the form opens with 修改): the results are what you came for. Open at first
 *  before a kind's first run; the form changed since the result shown below is said, with what that result used. */
export function BookBar({ value, onChange, markets, defaults, ran, open, onOpen, onErrors }: {
  value: BookBody; onChange: (v: BookBody) => void; markets: Market[]; defaults: Partial<Record<Market, BookSettings>>
  ran?: Partial<Record<Market, BookSettings>> | null; open: boolean; onOpen: (open: boolean) => void
  onErrors?: (errors: string[]) => void
}) {
  const ms = markets.filter((m) => defaults[m])
  const changed = !!ran && ms.some((m) => ran[m] && bookChanged(ran[m]!, value, m))
  return (
    <div className="border-b border-border" data-testid="book-bar">
      <div className="flex items-start gap-3 px-6 py-2 text-[0.8125rem]">
        <span className="shrink-0 font-medium">交易设定</span>
        {!open && (
          <span className="min-w-0 flex-1 text-muted-foreground">
            {ms.map((m) => <span key={m} className="block">{MARKET_NAME[m]}：<Dated text={settingsText(formSettings(value, m, defaults[m]!), m)} /></span>)}
            {changed && <span className="block text-warning">改过了，还没运行：下面的结果还是按上次的设定算的</span>}
          </span>
        )}
        {open && <span className="flex-1" />}
        <Button size="sm" variant="ghost" className="h-7 shrink-0" onClick={() => onOpen(!open)} aria-expanded={open}>
          {open ? <><ChevronUp className="size-3.5" />收起</> : <><SlidersHorizontal className="size-3.5" />修改</>}</Button>
      </div>
      {open && <BookForm value={value} onChange={onChange} markets={markets} defaults={defaults} onErrors={onErrors} />}
      {open && ran && <RunSettings settings={ran} form={value} />}
    </div>
  )
}

export function RunSettings({ settings, form }: { settings: Partial<Record<Market, BookSettings>>; form: BookBody }) {
  const ms = (Object.keys(settings) as Market[]).filter((m) => settings[m])
  const changed = ms.some((m) => bookChanged(settings[m]!, form, m))
  return (
    <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-subtle-foreground" data-testid="run-settings">
      {ms.map((m) => <div key={m}>这次运行 · {MARKET_NAME[m]}：<Dated text={settingsText(settings[m]!, m)} /></div>)}
      {changed && <div className="text-warning">上面的交易设定改过了：下面还是按这次运行的设定算的，重新运行才按新的算。</div>}
    </div>
  )
}

export function BookView({ books, runId }: { books: Partial<Record<Market, BookReport>>; runId: string }) {
  const markets = (Object.keys(books) as Market[]).filter((m) => books[m])
  const [pick, setPick] = useState<Market | null>(null)
  const m = pick && books[pick] ? pick : markets[0]
  const r = m ? books[m] : undefined
  if (!m || !r) return <Empty>没有结果</Empty>
  const s = r.stats
  return (
    <div data-testid="book">
      {markets.length > 1 && <div className="flex items-center gap-3 border-b border-border px-6 py-2"><MarketTabs markets={markets} value={m} onChange={setPick} />
        <span className="text-[0.8125rem] text-subtle-foreground">每个市场是一本独立的账（各自的币种、规则和费用）</span></div>}
      <KpiRow>
        <Kpi label="总收益" value={pct(s.total)} tone={changeClass(s.total)}
          sub={<>基准 {indexName(r.benchmark_id)} {pct(r.benchmark.total)} · 超额 <span className={changeClass(r.benchmark.excess)}>{pct(r.benchmark.excess)}</span></>} />
        <Kpi label="年化收益" value={pct(s.cagr)} tone={changeClass(s.cagr)} sub={`${s.days} 个交易日（按每年 252 天折算）`} />
        <Kpi label="最大回撤" value={pct(s.max_drawdown)} tone="text-down" sub={s.drawdown_end ? <>最深在 <span className="whitespace-nowrap">{s.drawdown_end}</span></> : undefined} />
        <Kpi label="夏普" value={s.sharpe == null ? "—" : s.sharpe.toFixed(2)} sub={`年化波动 ${rate(s.vol)}`} />
        <Kpi label="交易" value={s.trades.toLocaleString()} sub={`胜率 ${rate(s.win)} · 平均持有 ${s.avg_days == null ? "—" : s.avg_days.toFixed(1)} 天${s.open ? ` · 期末还持有 ${s.open} 只` : ""}`} />
        <Kpi label="平均仓位" value={rate(s.exposure)} sub={`期末净值 ${money(s.end_nav, m)}`} />
      </KpiRow>
      {r.notes.length > 0 && <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground">{r.notes.join("；")}</div>}
      <div className="grid grid-cols-[minmax(0,1fr)_23.75rem] border-b border-border">
        <div className="border-r border-border px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">净值 <span className="font-normal text-subtle-foreground">对比基准 · 下方是回撤 · 虚线是样本外开始</span></h3>
          <Curve r={r} />
        </div>
        <div className="px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">样本内 / 样本外 <span className="font-normal text-subtle-foreground">分界 {r.split.oos_start}（运行前固定）</span></h3>
          <SplitTable r={r} />
          <h3 className="mt-4 text-[0.875rem] font-semibold">没能照规则做的</h3>
          <Missed r={r} />
          <h3 className="mt-4 text-[0.875rem] font-semibold">成本与收入</h3>
          <div className="mt-1 grid grid-cols-3 text-[0.8125rem]">
            <span className="text-muted-foreground">费用<br /><span className="num text-foreground">{money(s.fees, m)}</span></span>
            <span className="text-muted-foreground">滑点<br /><span className="num text-foreground">{money(s.slippage, m)}</span></span>
            <span className="text-muted-foreground">分红<br /><span className="num text-foreground">{money(s.dividends, m)}</span></span>
          </div>
        </div>
      </div>
      <Trades runId={runId} market={m} />
    </div>
  )
}

function Curve({ r }: { r: BookReport }) {
  const pts = r.curve
  if (pts.length < 2) return <Empty>区间太短，画不出净值</Empty>
  const W = 600, TOP = 170, DD = 60, n = pts.length
  const vals = pts.flatMap(([, v, b]) => (b == null ? [v] : [v, b]))
  const lo = Math.min(1, ...vals), hi = Math.max(1, ...vals)
  const x = (i: number) => 40 + (i / (n - 1)) * (W - 50)
  const y = (v: number) => 10 + (1 - (v - lo) / (hi - lo || 1)) * (TOP - 20)
  const dd: number[] = []
  for (let i = 0, top = 0; i < n; i++) {   // the drawdown from the highest NAV so far
    top = Math.max(top, pts[i][1])
    dd.push(pts[i][1] / top - 1)
  }
  const worst = Math.min(-0.0001, ...dd)
  const yd = (v: number) => TOP + 10 + (v / worst) * (DD - 10)
  const line = (k: 1 | 2) => pts.map((p, i) => (p[k] == null ? null : `${x(i).toFixed(1)},${y(p[k]!).toFixed(1)}`)).filter(Boolean).join(" ")
  const cut = pts.findIndex(([d]) => d >= r.split.oos_start)
  return (
    <svg viewBox={`0 0 ${W} ${TOP + DD + 24}`} className="mt-2 w-full max-w-[60rem]" role="img" aria-label="净值和回撤">
      {[lo, 1, hi].map((v) => (
        <g key={v}>
          <line x1="40" x2={W - 10} y1={y(v)} y2={y(v)} className="stroke-border" strokeDasharray={v === 1 ? "3 3" : undefined} />
          <text x="36" y={y(v) + 4} textAnchor="end" className="num fill-subtle-foreground text-[0.6875rem]">{pct(v - 1, 0)}</text>
        </g>
      ))}
      {cut > 0 && <line x1={x(cut)} x2={x(cut)} y1="6" y2={TOP + DD} className="stroke-muted-foreground" strokeDasharray="4 3" />}
      <polyline points={line(2)} fill="none" className="stroke-muted-foreground" strokeWidth="1.2" />
      <polyline points={line(1)} fill="none" className="stroke-foreground" strokeWidth="1.6" />
      <path d={`M${x(0)},${yd(0)} ${dd.map((v, i) => `L${x(i).toFixed(1)},${yd(v).toFixed(1)}`).join(" ")} L${x(n - 1)},${yd(0)} Z`} className="fill-down/30" />
      <text x="36" y={yd(worst) + 4} textAnchor="end" className="num fill-subtle-foreground text-[0.6875rem]">{pct(worst, 0)}</text>
      {[0, Math.floor((n - 1) / 2), n - 1].map((i) => (
        <text key={i} x={x(i)} y={TOP + DD + 18} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} className="num fill-subtle-foreground text-[0.6875rem]">{pts[i][0]}</text>
      ))}
      <g className="text-[0.75rem]">
        <line x1="50" x2="66" y1="14" y2="14" className="stroke-foreground" strokeWidth="1.6" /><text x="70" y="18" className="fill-muted-foreground">净值</text>
        <line x1="104" x2="120" y1="14" y2="14" className="stroke-muted-foreground" strokeWidth="1.2" /><text x="124" y="18" className="fill-muted-foreground">基准 {indexName(r.benchmark_id)}</text>
      </g>
    </svg>
  )
}

function SplitTable({ r }: { r: BookReport }) {
  const rows: [string, Part][] = [["样本内", r.split.in], ["样本外", r.split.out], ["全区间", r.stats]]
  return (
    <table className="mt-2 w-full text-[0.8125rem]" data-testid="book-split">
      <thead className="text-muted-foreground">
        <tr className="h-7 border-b border-border [&>th]:font-normal [&>th]:whitespace-nowrap"><th className="text-left" /><th className="text-right">收益</th><th className="text-right">年化</th>
          <th className="text-right">最大回撤</th><th className="text-right">夏普</th><th className="text-right">交易</th><th className="text-right">胜率</th></tr>
      </thead>
      <tbody>
        {rows.map(([label, p]) => (
          <tr key={label} className="h-7 border-b border-border/60">
            <td>{label}</td><td className={cn("num text-right", changeClass(p.total))}>{pct(p.total)}</td>
            <td className={cn("num text-right", changeClass(p.cagr))}>{pct(p.cagr)}</td><td className="num text-right">{pct(p.max_drawdown)}</td>
            <td className="num text-right">{p.sharpe == null ? "—" : p.sharpe.toFixed(2)}</td><td className="num text-right">{p.trades}</td>
            <td className="num text-right">{rate(p.win)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Missed({ r }: { r: BookReport }) {
  const c = r.counts
  const lines: [number, string][] = [
    [c.blocked_buy.limit ?? 0, "次开盘涨停，没买到"], [c.blocked_buy.halt ?? 0, "次入场那天停牌，没买"],
    [c.skipped.capacity ?? 0, `次已经满仓（${r.settings.max_positions} 只），跳过`],
    [c.skipped.weak ?? 0, `次大盘弱（基准前一天收在 200 日线下），按设定最多 ${r.settings.weak_positions ?? "—"} 只，跳过`],
    [c.skipped.unaffordable ?? 0, "次一手都买不起，跳过"],
    [c.skipped.held ?? 0, "次已经持有这只，跳过"], [c.blocked_sell.limit ?? 0, "次跌停卖不出，顺延到下一个开盘"],
    [c.same_bar, "次同一根 K 线同时碰到止损和止盈，按止损算"], [c.gap, "次开盘就越过止损或止盈，按开盘价成交"],
    [c.corporate, "次分红、送转或拆股：股数按因子的变化记，A 股的现金按分红记录"],
    [c.unexplained ?? 0, "次复权因子的变化和分红记录对不上（配股、缺了记录的送转等），差额没有记现金"],
  ]
  const shown = lines.filter(([n]) => n > 0)
  return shown.length === 0 ? <p className="mt-1 text-[0.8125rem] text-subtle-foreground">全部照规则成交</p> : (
    <ul className="mt-1 space-y-0.5 text-[0.8125rem] text-muted-foreground" data-testid="book-missed">
      {shown.map(([n, text]) => <li key={text}><span className="num text-foreground">{n}</span> {text}</li>)}
    </ul>
  )
}

function Trades({ runId, market }: { runId: string; market: Market }) {
  const { data = [], isLoading } = useQuery({ queryKey: ["validate", runId, "trades"], queryFn: () => validateEndpoints.trades(runId) })
  const mine = data.filter((t) => t.market === market)
  const shown = [...mine].sort((a, b) => b.entry_date.localeCompare(a.entry_date)).slice(0, 40)
  return (
    <div className="px-6 py-4">
      <h3 className="text-[0.875rem] font-semibold">成交 <span className="font-normal text-subtle-foreground">最近 {shown.length} 笔 · 共 {mine.length} 笔，全部在导出的成交里</span></h3>
      {isLoading ? <Empty>读取中…</Empty> : shown.length === 0 ? <Empty>这段区间没有成交</Empty> : (
        <table className="mt-2 w-full text-[0.8125rem]" data-testid="book-trades">
          <thead className="text-muted-foreground">
            <tr className="h-7 border-b border-border [&>th]:font-normal"><th className="text-left">股票</th><th className="text-left">信号日</th><th className="text-left">入场</th>
              <th className="text-right">入场价</th><th className="text-right">股数</th><th className="text-left pl-4">离场</th><th className="text-right">离场价</th>
              <th className="text-left pl-4">原因</th><th className="text-right">收益</th><th className="text-right">天数</th></tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={`${t.listing_id}|${t.entry_date}`} className="h-7 border-b border-border/60">
                <td><Link to={`/k/${t.listing_id}?day=${t.signal_date}`} className="hover:underline">{t.name ?? codeOf(t.listing_id)}</Link>
                  <span className="num ml-2 text-subtle-foreground">{codeOf(t.listing_id)}</span></td>
                <td className="num">{t.signal_date}</td><td className="num">{t.entry_date}</td><td className="num text-right">{t.entry_price}</td>
                <td className="num text-right">{t.shares.toLocaleString()}</td><td className="num pl-4">{t.exit_date ?? "—"}</td>
                <td className="num text-right">{t.exit_price ?? "—"}</td><td className="pl-4">{t.exit_date ? REASON[t.reason ?? ""] ?? t.reason : "期末持有"}</td>
                <td className={cn("num text-right", changeClass(t.ret))}>{pct(t.ret)}</td><td className="num text-right">{t.days ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ---- 稳健性 ---------------------------------------------------------------------------------------------------------
const METRICS: [keyof Brief, string, "ret" | "dd" | "ratio" | "count"][] = [
  ["total", "全区间收益", "ret"], ["out_total", "样本外收益", "ret"], ["max_drawdown", "最大回撤", "dd"], ["sharpe", "夏普", "ratio"], ["trades", "交易数", "count"],
]

export function RobustView({ r, markets }: { r: Robust; markets: Market[] }) {
  const [metric, setMetric] = useState<keyof Brief>("total")   // the holdout is for checking a choice, not for making it
  const [pick, setPick] = useState<Market | null>(null)
  const m = pick ?? markets[0]
  const kind = METRICS.find(([k]) => k === metric)![2]
  const fmt = (v: number | null | undefined) => (v == null ? "—" : kind === "ratio" ? v.toFixed(2) : kind === "count" ? String(v) : pct(v))
  const [ax, ay] = r.axes
  const cell = (at: (number | boolean)[]) => r.cells.find((c) => c.at.every((v, i) => v === at[i]))?.[m]
  const values = r.cells.map((c) => c[m]?.[metric]).filter((v): v is number => typeof v === "number")
  const scale = Math.max(...values.map(Math.abs), 1e-9)
  const tint = (v: number | null | undefined) => {
    if (v == null) return undefined
    if (kind === "count") return `color-mix(in oklab, var(--structure) ${Math.round((v / scale) * 35)}%, transparent)`
    const good = kind === "dd" ? v > -scale / 2 : v > 0
    return `color-mix(in oklab, var(${good ? "--up" : "--down"}) ${Math.round((Math.abs(v) / scale) * 40)}%, transparent)`
  }
  return (
    <div data-testid="robust">
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-6 py-2 text-[0.8125rem]">
        <MarketTabs markets={markets} value={m} onChange={setPick} />
        <span role="radiogroup" aria-label="指标" className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
          {METRICS.map(([k, label]) => (
            <button key={k} type="button" role="radio" aria-checked={metric === k} onClick={() => setMetric(k)}
              className={cn("rounded px-2 py-0.5", metric === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>{label}</button>
          ))}
        </span>
        <span className="text-subtle-foreground">本次比较了 {r.compared} 组参数：比较得越多，越容易挑到碰巧好的一组——按全区间挑，样本外只用来核对，别按样本外挑参数</span>
      </div>
      {r.notes.length > 0 && <div className="border-b border-border px-6 py-2 text-[0.8125rem] text-muted-foreground">{r.notes.join("；")}</div>}
      <div className="grid grid-cols-[minmax(0,1fr)_23.75rem]">
        <div className="border-r border-border px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">参数敏感性 <span className="font-normal text-subtle-foreground">
            {ax ? `${ax.param}${ay ? ` × ${ay.param}` : ""} · 框出的是现在的参数 · 看它在平台上还是孤立的尖峰` : ""}</span></h3>
          {!ax ? <Empty>规则没有标 tune: true 的参数，没有热力图</Empty> : (
            <div className="mt-3 overflow-x-auto">
              <table className="border-collapse text-[0.8125rem]" data-testid="heatmap">
                <thead>
                  <tr><th className="px-2 text-right font-normal text-subtle-foreground">{ay ? `${ay.param} \\ ${ax.param}` : ax.param}</th>
                    {ax.values.map((v) => <th key={String(v)} className="num px-2 py-1 font-normal text-muted-foreground">{String(v)}</th>)}</tr>
                </thead>
                <tbody>
                  {(ay ? ay.values : [null]).map((yv) => (
                    <tr key={String(yv)}>
                      <th className="num px-2 text-right font-normal text-muted-foreground">{yv == null ? "" : String(yv)}</th>
                      {ax.values.map((xv) => {
                        const at = yv == null ? [xv] : [xv, yv]
                        const v = cell(at)?.[metric] as number | null | undefined
                        const here = xv === ax.current && (ay == null || yv === ay.current)
                        return (
                          <td key={String(xv)} className={cn("num h-9 min-w-16 border border-border/60 px-2 text-center", here && "outline outline-2 -outline-offset-2 outline-foreground")}
                            style={{ background: tint(v) }} title={`${ax.param}=${xv}${ay ? `，${ay.param}=${yv}` : ""}`} aria-current={here || undefined}>{fmt(v)}</td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="px-6 py-4">
          <h3 className="text-[0.875rem] font-semibold">成本上调 <span className="font-normal text-subtle-foreground">现在的参数，费用和滑点放大</span></h3>
          <table className="mt-2 w-full text-[0.8125rem]" data-testid="cost-stress">
            <thead className="text-muted-foreground">
              <tr className="h-7 border-b border-border [&>th]:font-normal"><th className="text-left">成本</th><th className="text-right">收益</th><th className="text-right">样本外</th>
                <th className="text-right">最大回撤</th><th className="text-right">夏普</th></tr>
            </thead>
            <tbody>
              {r.costs.map((c) => {
                const b = c[m]
                return (
                  <tr key={c.k} className="h-7 border-b border-border/60">
                    <td>× {c.k}</td><td className={cn("num text-right", changeClass(b?.total))}>{pct(b?.total)}</td>
                    <td className={cn("num text-right", changeClass(b?.out_total))}>{pct(b?.out_total)}</td><td className="num text-right">{pct(b?.max_drawdown)}</td>
                    <td className="num text-right">{b?.sharpe == null ? "—" : b.sharpe.toFixed(2)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <p className="mt-2 text-[0.75rem] text-subtle-foreground">结果只是参考：这里不会改规则。</p>
        </div>
      </div>
    </div>
  )
}
