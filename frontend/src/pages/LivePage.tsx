// 盯盘 (design 04, spec §8.5): the stocks you follow — your watch groups and the 潜伏 lists of the enabled rules'
// latest daily scans — with each market's session and the index strip. While a market trades its rows move with
// every poll of its realtime source; otherwise they are the latest close. The panel shows a stock's 分时图.
// Nothing on this page is a signal: the scan on the close is the record. Keys: J/K move, Enter opens the K-line page.
import { BarChart3, BellOff, BellRing, Info } from "lucide-react"
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react"
import { Link, useNavigate, useNavigationType, useSearchParams } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { ask } from "@/lib/ask"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { NumberField } from "@/components/NumberField"
import { fromEdge } from "@/components/rules/distance"
import { Kbd } from "@/components/ui/kbd"
import { Switch } from "@/components/ui/switch"
import { endpoints, usePutSetting, useSettings, useStatus } from "@/lib/api"
import { changeClass, codeOf, fmtPct, fmtPrice, indexName, shortDate } from "@/lib/format"
import { useNotify } from "@/lib/jobs"
import {
  factsOf, lateInDay, latentOf, type LiveAlert, type LiveMarket, type LiveTab, type MicroPeriod, type MinuteDays, nearestLevels, priceAge, resonance, type Row, rowsOf,
  feedAge, lateText, sessionText, statusOf, useAlerts, useLatent, useLive, useMicro, useMinutes, useMute, useNow, vwapNote,
} from "@/lib/live"
import { useBoardAt } from "@/lib/market"
import { useStructures } from "@/lib/rules"
import { usePersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import { leaveToFocus } from "@/lib/keys"
import { PageHeader } from "./common"
import { IntradayChart } from "./live/IntradayChart"

const TABS: [LiveTab, string][] = [["watched", "自选"], ["latent", "潜伏"]]   // 盯盘 follows yours; the 潜伏 stocks you do not watch are listed (10-12)
const TONE = { warn: "text-warning", fg: "text-foreground", muted: "text-muted-foreground" }
/** The intraday volume ratio; "≈" when the source's minute volumes are only approximate (the test profile's). */
const ratio = (v: number | null, approx: boolean) => (v == null ? "—" : `${approx ? "≈" : ""}${v.toFixed(1)}`)
// the figures never give way (squeezed, they would run into each other); the name and the status truncate
const GRID = "grid grid-cols-[minmax(0,1.7fr)_minmax(min-content,0.9fr)_minmax(min-content,0.8fr)_minmax(min-content,0.6fr)_minmax(min-content,0.8fr)_minmax(0,1.7fr)] items-center gap-2 px-4"

const PICKED = "live.picked"   // the row you are on, for this session

export default function LivePage() {
  const navigate = useNavigate()
  const { data, error } = useLive()
  const markets = useMemo(() => data?.markets ?? [], [data])
  const [tab, setTab] = useState<LiveTab>("watched")
  const latentCN = useLatent("CN").data, latentJP = useLatent("JP").data
  const latent = useMemo(() => latentOf([latentCN, latentJP]), [latentCN, latentJP])
  const now = useNow()
  const latentAt = (latentCN?.rows ?? []).map((r) => r.at).filter((x): x is string => !!x).sort().at(-1)   // the A 股 quote's time
  const latentAge = latentAt && latentCN ? priceAge(latentAt, "CN", latentCN.session, now) : null
  // the row you are on, remembered for the session (not in the address: a router navigation per key starved the
  // selection's own updates). K 线's 分时 lands on its stock (?lid=); coming back (← from K 线) returns to your row
  const [search] = useSearchParams()
  const asked = search.get("lid")   // from K 线's 分时
  const back = useNavigationType() === "POP"
  const [picked, setPickedState] = useState(() => {
    let kept: string | null = null
    try { kept = sessionStorage.getItem(PICKED) } catch { /* none */ }
    return back ? kept ?? asked : asked ?? kept
  })
  const setPicked = useCallback((lid: string | null) => {
    setPickedState(lid)
    try { if (lid) sessionStorage.setItem(PICKED, lid); else sessionStorage.removeItem(PICKED) } catch { /* kept in the page */ }
  }, [])
  const followed = useMemo(() => rowsOf(markets), [markets])
  const rows = tab === "latent" ? latent : followed
  const current = rows.find((r) => r.listing_id === picked) ?? rows[0]
  const approx = new Set(markets.filter((m) => m.source?.approx_volume).map((m) => m.market))

  const currentRef = useRef<string | null>(null)   // the row you are on, as the keys see it
  useLayoutEffect(() => { // before paint: a key pressed as soon as the list shows reaches the handler that knows it
    const onKey = (ev: KeyboardEvent) => {
      if (leaveToFocus(ev)) return   // a focused control keeps its keys (lib/keys)
      const k = ev.key.toLowerCase()
      const at = rows.findIndex((r) => r.listing_id === currentRef.current)   // set by each key at once: a held J outruns the renders
      const i = at >= 0 ? at : rows.length ? 0 : -1
      if ((k === "j" || k === "k") && rows.length) {
        const next = rows[(i + (k === "j" ? 1 : -1) + rows.length) % rows.length].listing_id
        currentRef.current = next
        setPicked(next)
      } else if (k === "enter" && rows[i]) navigate(`/k/${rows[i].listing_id}`)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [rows, navigate, setPicked])
  const listRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {   // in step with what the page shows (no key slips in between), and in view (J / K, back from K 线)
    currentRef.current = current?.listing_id ?? null
    if (current) listRef.current?.querySelector(`[data-lid="${CSS.escape(current.listing_id)}"]`)?.scrollIntoView({ block: "nearest" })
  }, [current])

  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-4"><span className="shrink-0">盯盘</span><Sessions markets={markets} /><Indexes markets={markets} /></span>}>
        <AlertSettings />
      </PageHeader>
      <div className="flex min-h-0 flex-1">
        <div className="flex w-[52%] min-w-[27.5rem] flex-col border-r border-border">
          <div className="flex h-12 shrink-0 items-center gap-3 px-4">
            <span role="tablist" className="flex items-center gap-0.5 rounded-md bg-muted p-0.5 text-[0.8125rem]">
              {TABS.map(([k, label]) => (
                <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
                  className={cn("rounded px-2.5 py-1", tab === k ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}>
                  {label} <span className="num text-subtle-foreground">{(k === "latent" ? latent : followed).length}</span>
                </button>
              ))}
            </span>
            <span className="flex-1" />
            {tab === "latent" && latentAge ? (   // the whole-market quote's time; amber when it has not moved for 3 minutes
              <span data-testid="live-latent-age" className={cn("text-[0.8125rem]", latentAge.late ? "text-warning" : "text-subtle-foreground")}>
                {latentAge.late ? lateText(latentAge) : `价格 ${latentAt!.slice(11, 19)}`}</span>
            ) : <span className="text-[0.8125rem] text-subtle-foreground">{tab === "latent" ? "按离箱顶远近排" : "按距触发排序"}</span>}
          </div>
          <div className={cn(GRID, "h-8 shrink-0 border-y border-border text-[0.8125rem] text-muted-foreground")}>
            <span>名称</span><span className="text-right">现价</span><span className="text-right">涨跌</span>
            <span className="text-right">量比</span><span className="text-right">距上沿</span><span>状态</span>
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto">
            {error && <div className="px-6 py-16 text-center text-[0.875rem] text-error">读不出盯盘数据：{String(error)}</div>}
            {data && !rows.length && <EmptyList tab={tab} />}
            {rows.map((r) => <LiveRowView key={r.listing_id} r={r} approx={approx.has(r.market)} active={r === current}
              onPick={() => setPicked(r.listing_id)} />)}
          </div>
          <Note markets={markets} />
        </div>
        <div className="min-w-0 flex-1 overflow-y-auto">
          {asked && picked === asked && data && !followed.some((r) => r.listing_id === asked) && (   // asked from K 线's 分时
            <div className="mx-5 mt-4 rounded-md border border-border px-3 py-2 text-[0.875rem] text-muted-foreground" data-testid="live-not-listed">
              {codeOf(asked)} 不在你的自选里：盯盘只盯自选（和你勾了提醒的线），它的分时只在你看着时取。在 K 线页按 W 加入观察，就一直盯着。</div>
          )}
          {current && <Detail r={current} m={markets.find((x) => x.market === current.market)!} />}
          <AlertCenter onPick={(lid) => { setTab("watched"); setPicked(lid) }} />
        </div>
      </div>
    </div>
  )
}

function Sessions({ markets }: { markets: LiveMarket[] }) {
  const now = useNow(), boardAt = useBoardAt("CN").data
  return (
    <span className="flex shrink-0 items-center gap-2 text-[0.8125rem] font-normal">
      {markets.map((m) => {
        const on = m.session.state === "trading" && !m.error
        const s = sessionText(m)
        return (
          <span key={m.market} data-testid={`live-session-${m.market}`}
            className={cn("flex h-[1.375rem] items-center gap-1.5 rounded-sm px-2", s.broken ? "bg-error/15 text-error" : "bg-accent")}
            title={m.error ?? (m.source ? `${m.source.name} · 约晚 ${m.source.delay} 分钟` : "收盘数据")}>
            <span className={cn("size-1.5 rounded-full", s.broken ? "bg-error" : on ? "bg-foreground" : "bg-subtle-foreground")} />
            <span>{s.text}</span>
            {on && <span className="num text-muted-foreground">{m.at.slice(11, 19)}</span>}
            {on && (() => { const age = feedAge(m, m.market === "CN" ? boardAt : null, now); return age?.late ? <span className="text-warning">{lateText(age)}</span> : null })()}
          </span>
        )
      })}
    </span>
  )
}

function Indexes({ markets }: { markets: LiveMarket[] }) {
  return (
    // one line: the indexes that fit are shown whole, the rest wrap to a line that is cut off — never squeezed
    <span className="flex h-[1.25rem] min-w-0 flex-wrap items-baseline gap-x-4 overflow-hidden border-l border-border pl-4 text-[0.8125rem] font-normal"
      data-testid="live-indexes">
      {markets.flatMap((m) => m.indexes.map((x) => (
        <span key={x.listing_id} className="flex items-baseline gap-1.5" title={x.live ? "盘中" : "收盘"}>
          <span className="text-muted-foreground">{indexName(x.listing_id)}</span>
          <span className={cn("num", x.live ? "text-foreground" : "text-muted-foreground")}>{fmtPrice(x.last, m.market)}</span>
          <span className={cn("num", changeClass(x.chg))}>{fmtPct(x.chg)}</span>
        </span>
      )))}
    </span>
  )
}

function LiveRowView({ r, approx, active, onPick }: { r: Row; approx: boolean; active: boolean; onPick: () => void }) {
  const s = statusOf(r)
  return (
    <button type="button" onClick={onPick} data-testid="live-row" data-row data-lid={r.listing_id} aria-current={active || undefined}
      className={cn(GRID, "h-10 w-full text-left text-[0.875rem]", active ? "bg-accent" : "hover:bg-accent/60")}>
      <span className="min-w-0">
        <span className="block truncate">{r.name ?? codeOf(r.listing_id)}</span>
        <span className="num block text-[0.75rem] leading-tight text-subtle-foreground">{codeOf(r.listing_id)} {r.market}</span>
      </span>
      <span className={cn("num text-right", r.live ? "text-foreground" : "text-muted-foreground")}>{fmtPrice(r.last, r.market)}</span>
      <span className={cn("num text-right", changeClass(r.chg))}>{fmtPct(r.chg)}</span>
      <span className={cn("num text-right", r.vol_ratio != null && r.vol_ratio >= 1.5 ? "text-foreground" : "text-muted-foreground")}
        title={approx ? "测试源的分钟成交量只是近似" : undefined}>{ratio(r.vol_ratio, approx)}</span>
      {/* 距上沿 as everywhere (components/rules/distance): the rise still needed; over the edge, by how much */}
      <span className={cn("num text-right", r.to_top != null && r.to_top >= 0 ? "text-warning" : "text-muted-foreground")}>
        {r.to_top == null ? "—" : r.to_top < 0 ? fmtPct(1 / (1 + r.to_top) - 1, 1, false) : `越过 ${fmtPct(r.to_top, 1, false)}`}</span>
      <span className={cn("truncate text-[0.8125rem]", TONE[s.tone])}>{s.text}
        {r.live && r.vwap_gap != null && r.vwap_gap >= 0.05 && (   // far above the day's average
          <span className="ml-1.5 text-warning" title="离均价线 5 个点以上，尤其尾盘急拉，次日容易回落（经验说法，没有经过检验）">
            · 离均价 +{(r.vwap_gap * 100).toFixed(1)} 点</span>)}</span>
    </button>
  )
}

function EmptyList({ tab }: { tab: LiveTab }) {
  return (
    <div className="px-6 py-16 text-center text-[0.875rem] text-muted-foreground" data-testid="live-empty">
      {tab === "watched" ? "自选里还没有股票：在 K 线页按 W 加入观察，盯盘就盯着它。" : "已启用规则的最新扫描里，没有你没观察的潜伏股票。"}
      <div className="mt-2 text-[0.8125rem] text-subtle-foreground">
        这里列出 <Link to="/watch" className="underline">观察</Link> 分组里的股票，和 <Link to="/rules" className="underline">规则库</Link> 里已启用规则的潜伏名单。
      </div>
    </div>
  )
}

function Note({ markets }: { markets: LiveMarket[] }) {
  const { data } = useStatus()
  const test = (data?.markets ?? []).some((m) => m.profile === "test")
  const delays = markets.filter((m) => m.source).map((m) => `${m.market} ${m.source!.name} 约晚 ${m.source!.delay} 分钟`)
  return (
    <div className="flex shrink-0 items-start gap-2 border-t border-border px-4 py-3 text-[0.8125rem] text-subtle-foreground">
      <Info className="mt-0.5 size-3.5 shrink-0" />
      <span>
        盘中的数字只用来看，信号以收盘后的日线扫描为准；交易时段外显示收盘数据。
        {test && " 测试档的盘中行情来自 yfinance 1 分钟线（延迟行情，约晚 20 分钟，每 2 分钟一次，最多约 50 只）；它的分钟成交量只是近似（实测深市偏大 1.3–1.5 倍，东证缺开盘和收盘竞价），量比只用来看链路。"}
        {delays.length > 0 && ` 现在：${delays.join("，")}。`}
        {markets.filter((m) => m.error).map((m) => (
          <span key={m.market} className="block text-error">{m.market} 盘中行情中断：{m.error}（显示的是收盘数据）</span>
        ))}
        <span className="ml-2 inline-flex items-center gap-1"><Kbd>J</Kbd><Kbd>K</Kbd> 上下 <Kbd>↵</Kbd> 打开 K 线</span>
      </span>
    </div>
  )
}

/** A row of choices (a radio group under its label). */
function Choice<T extends number | null>({ label, options, value, onChange, text }: {
  label: string; options: T[]; value: T; onChange: (v: T) => void; text: (v: T) => string
}) {
  return (
    <span className="flex items-center gap-1 whitespace-nowrap">{label}
      <span role="radiogroup" aria-label={label} className="flex items-center gap-0.5 rounded-sm bg-muted p-0.5">
        {options.map((o) => (
          <button key={o ?? "off"} type="button" role="radio" aria-checked={value === o} onClick={() => onChange(o)}
            className={cn("whitespace-nowrap rounded-sm px-1.5", value === o ? "bg-background text-foreground" : "hover:text-foreground")}>{text(o)}</button>
        ))}
      </span>
    </span>
  )
}

function Detail({ r, m }: { r: Row; m: LiveMarket }) {
  const [keptDays, setDays] = usePersisted<MinuteDays>("live.days", 1) // the last days side by side
  const days = ([1, 5, 10, 20] as MinuteDays[]).includes(keptDays) ? keptDays : 1
  const [waves, setWaves] = usePersisted<boolean>("live.waves", true)
  const { data: minutes, error } = useMinutes(r.listing_id, days)
  const [kept, setPeriod] = usePersisted<MicroPeriod | null>("live.micro", 5) // 分时小箱: 1 / 5 / 15 minutes, or off
  const period = kept === null || [1, 5, 15].includes(kept) ? kept : 5
  const { data: micro } = useMicro(r.listing_id, period)
  const { data: structures } = useStructures(r.listing_id, "qfq", period !== null)
  const res = period !== null && minutes?.day ? resonance(structures, micro?.events, period, minutes.day) : null
  const mute = useMute()
  const { data: alerts } = useAlerts()
  const muted = alerts?.muted.includes(r.listing_id) ?? false
  const change = r.last != null && r.prev != null ? r.last - r.prev : null
  const facts = [
    r.vol_ratio != null && `盘中量比 ${ratio(r.vol_ratio, !!m.source?.approx_volume)}`,
    r.to_top != null && fromEdge(r.to_top),
    m.error ? "行情中断" : r.live && m.source ? `延迟 ${m.source.delay} 分` : !r.live && "收盘数据",
  ].filter(Boolean)
  const vwap = minutes?.minutes.at(-1)?.vwap
  const note = r.live ? vwapNote(r.vwap_gap, lateInDay(m.session)) : null
  const near = period !== null && micro && r.last != null ? nearestLevels(micro.boxes, r.last) : null
  return (
    <div className="p-5" data-testid="live-detail">
      <div className="flex flex-wrap items-center gap-2">   {/* narrow: the buttons go below, the name keeps its room */}
        <span className="min-w-0 truncate whitespace-nowrap text-[1.0625rem] font-semibold" title={r.name ?? codeOf(r.listing_id)}>{r.name ?? codeOf(r.listing_id)}</span>
        <span className="num shrink-0 text-[0.875rem] text-subtle-foreground">{codeOf(r.listing_id)}</span>
        <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[0.75rem] font-normal">{r.market}</Badge>
        <span className="ml-auto flex shrink-0 gap-2">
          <Button size="sm" variant="outline" asChild><Link to={`/k/${r.listing_id}`}><BarChart3 className="size-4" />打开 K 线</Link></Button>
          <Button size="sm" variant="outline" disabled={muted || mute.isPending} onClick={() => mute.mutate(r.listing_id, {
            onSuccess: () => toast(`${r.name ?? codeOf(r.listing_id)} 今天不再提醒`), onError: (e) => toast.error(String(e)),
          })}><BellOff className="size-4" />{muted ? "今天已不提醒" : "今天不再提醒"}</Button>
        </span>
      </div>
      <div className="mt-2 flex items-baseline gap-3">
        <span className="num text-[1.3125rem] font-semibold">{fmtPrice(r.last, r.market)}</span>
        <span className={cn("num text-[0.875rem]", changeClass(r.chg))}>
          {change == null ? "" : `${change > 0 ? "+" : ""}${fmtPrice(change, r.market)}`}  {fmtPct(r.chg)}</span>
        <span className="text-[0.8125rem] text-subtle-foreground">{facts.join(" · ")}</span>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.75rem]">
        <span className="text-muted-foreground">分时 · 1 分钟{m.source?.approx_minutes ? "（由快照近似合成）" : ""}{minutes?.day ? ` · ${shortDate(minutes.day)}` : ""}</span>
        <span className="flex-1" />
        <span className="flex items-center gap-1 whitespace-nowrap"><span className="h-0.5 w-3 bg-foreground" />价格</span>
        {vwap != null && <span className="num flex items-center gap-1 whitespace-nowrap text-ma-1"><span className="h-0.5 w-3 bg-ma-1" />均价 {fmtPrice(vwap, r.market)}</span>}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.75rem] text-muted-foreground">
        <Choice label="几天" options={[1, 5, 10, 20] as MinuteDays[]} value={days} onChange={setDays} text={(d) => (d === 1 ? "当天" : `${d} 日`)} />
        <Choice label="分时小箱" options={[1, 5, 15, null] as (MicroPeriod | null)[]} value={period} onChange={setPeriod}
          text={(p) => (p === null ? "关" : `${p} 分`)} />
        <label className="flex items-center gap-1 whitespace-nowrap">
          <input type="checkbox" checked={waves} onChange={(e) => setWaves(e.target.checked)} />波段量</label>
      </div>
      <div className="mt-1">
        {minutes?.minutes.length ? <IntradayChart data={minutes} market={r.market} top={r.top} micro={period !== null ? micro : undefined}
          waves={waves && period !== null} />
          : <div className="py-16 text-center text-[0.8125rem] text-subtle-foreground">
            {error ? `读不出分时数据：${String(error)}` : !minutes ? "读取中…"
              : "还没有这只股票的分时数据：开盘后，它的市场每次取盘中行情时一起记下（保留最近 20 个交易日）。"}
          </div>}
      </div>
      {(note || near?.above != null || near?.below != null) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.8125rem]" data-testid="intraday-notes">
          {note && <span className={note.warn ? "text-warning" : "text-muted-foreground"} title="上升时白线（价）在上、黄线（均价）在下；离均价线 5 个点以上，尤其尾盘急拉，次日容易回落（经验说法，没有经过检验）">
            {note.text}</span>}
          {near?.above != null && r.last != null && <span className="text-muted-foreground">上方阻力 <span className="num">{fmtPrice(near.above, r.market)}</span>（{fmtPct(near.above / r.last - 1, 1)}）</span>}
          {near?.below != null && r.last != null && <span className="text-muted-foreground">下方支撑 <span className="num">{fmtPrice(near.below, r.market)}</span>（{fmtPct(near.below / r.last - 1, 1)}）</span>}
        </div>
      )}
      {res && (res.macro || res.meso || res.micro) && (
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.8125rem]" data-testid="resonance">
          {[res.macro, res.meso, res.micro].filter(Boolean).map((t) => <span key={t} className="text-muted-foreground">{t}</span>)}
          {res.label && <span className="font-medium text-warning">{res.label}</span>}
        </div>
      )}
      {period !== null && (
        <p className="mt-1 text-[0.75rem] text-subtle-foreground">
          分时小箱只用当天和最近几天保留的分钟线：只能实时看，不能回测。还没收完的那根标“量≈”，不按时间外推。
          波段是按价格回撤 {period === 1 ? "0.6" : period === 5 ? "1" : "1.5"}% 划的（研究定义，数浪不必较真），量比的是这一波最大的一根和上一同向波最大的一根。</p>
      )}
      <div className="mt-4 space-y-1 border-t border-border pt-3 text-[0.8125rem] text-muted-foreground">
        {r.rule && <div>潜伏 · {r.rule}{r.stage ? `（最新扫描走到 ${r.stage}）` : ""}{r.top != null ? ` · 箱顶 ${fmtPrice(r.top, r.market)}` : ""}</div>}
        {r.watched && <div>在你的观察分组里</div>}
      </div>
    </div>
  )
}

/** 预警中心 (spec §8.5, design 04 今日提醒): the day's alerts of both markets, newest first, with what the close said. */
function AlertCenter({ onPick }: { onPick: (listingId: string) => void }) {
  const { data } = useAlerts()
  const [open, setOpen] = useState<number | null>(null) // the alert whose judgement on today's bar is shown
  const [gone, setGone] = useState<Set<string>>(new Set()) // lines deleted from here
  const deleteLine = async (a: LiveAlert) => {
    if (!(await ask({ title: "删除这条线？", text: `价位 ${fmtPrice(a.detail?.line ?? a.price, a.market)}。K 线页上也一起删掉，它的提醒也没有了。`, action: "删除这条线" }))) return
    void endpoints.deleteDrawing(a.key).then(() => setGone(new Set(gone).add(a.key)), (e) => toast.error(String(e)))
  }
  const { data: settings } = useSettings()
  const { data: notify } = useNotify()
  const mute = useMute()
  const alerts = [...(data?.alerts ?? [])].reverse()
  const muted = new Set(data?.muted ?? [])
  const pushed = !!settings?.["notify.live"] && !!notify?.channels.feishu?.ready
  return (
    <section className="mx-5 mb-5 border-t border-border pt-4" data-testid="alert-center">
      <div className="flex items-baseline gap-2">
        <h2 className="text-[0.875rem] font-semibold">今日提醒</h2>
        <span className="num text-[0.8125rem] text-subtle-foreground">{alerts.length}</span>
        <span className="flex-1" />
        <span className="text-[0.8125rem] text-subtle-foreground">{pushed ? "已推送飞书（仅本人）" : "没有推送飞书：只在这里和弹窗里"}</span>
      </div>
      {alerts.length === 0 && (
        <div className="py-6 text-center text-[0.8125rem] text-subtle-foreground">
          今天还没有提醒。交易时段里，观察中的股票接近箱顶、持有中的股票接近失效线、价格接近你画的线，都会先提醒你注意；潜伏股盘中满足规则也列在这里。</div>
      )}
      {alerts.map((a) => (
        <div key={a.id} className="flex items-start gap-3 border-b border-border py-2.5 last:border-0" data-testid="alert">
          <span className="num w-10 shrink-0 pt-px text-[0.8125rem] text-subtle-foreground">{a.at.slice(11, 16)}</span>
          <div className="min-w-0 flex-1">
            <button type="button" className="block w-full text-left" onClick={() => onPick(a.listing_id)}>
              <span className="block text-[0.875rem]">{a.title.split(" · ")[0]} · {a.text}</span>
              <span className={cn("block text-[0.8125rem]", a.status === "pending" ? "text-warning" : "text-muted-foreground")}>{alertLine(a, pushed)}</span>
            </button>
            {open === a.id && a.detail?.lines && (
              <pre className="mt-1 whitespace-pre-wrap font-sans text-[0.8125rem] leading-relaxed text-muted-foreground" data-testid="alert-lines">
                {a.detail.lines.join("\n")}</pre>
            )}
          </div>
          {a.detail?.lines && (
            <Button size="sm" variant="ghost" className="h-6 shrink-0 px-2 text-[0.8125rem]" aria-expanded={open === a.id}
              onClick={() => setOpen(open === a.id ? null : a.id)}>判定</Button>
          )}
          {a.kind === "ex" || a.kind === "market" || a.kind === "feed" ? null
            : muted.has(a.listing_id) ? <span className="shrink-0 pt-px text-[0.8125rem] text-subtle-foreground">今天不再提醒</span>
            : a.kind === "line" || a.detail?.kind === "line" ? (gone.has(a.key) ? <span className="shrink-0 pt-px text-[0.8125rem] text-subtle-foreground">线已删除</span> : (
              <Button size="sm" variant="ghost" className="h-6 shrink-0 px-2 text-[0.8125rem]" onClick={() => void deleteLine(a)}>删除这条线</Button>
            )) : (
              <Button size="sm" variant="ghost" className="h-6 shrink-0 px-2 text-[0.8125rem]" disabled={mute.isPending}
                onClick={() => mute.mutate(a.listing_id, { onError: (e) => toast.error(String(e)) })}>忽略今天</Button>
            )}
        </div>
      ))}
    </section>
  )
}

/** An alert's second line: its price, then what it is — a rule alert's verdict, or a line's moment (its facts) and
 *  whether Feishu heard it (`pushed`: the push is on and the bot is set up). */
function alertLine(a: LiveAlert, pushed: boolean): string {
  const price = a.price == null ? "" : `${fmtPrice(a.price, a.market)} · `
  if (a.kind === "rule") {
    return price + (a.status === "pending" ? "未确认：收盘后按正式规则确认或撤销" : a.status === "confirmed" ? "收盘确认：正式规则出了信号" : "收盘撤销：收盘时没有都满足")
  }
  if (a.kind === "ex") return "超出正常波动：今天盘中不做判断，等收盘数据"
  if (a.kind === "box" || a.kind === "line") return price + (a.kind === "box" ? "潜伏箱顶的价格提醒" : "你画的线")
  const heard = !a.detail?.notify ? ["只在应用里"] : pushed ? ["已发飞书"] : []
  return [...(a.price == null ? [] : [fmtPrice(a.price, a.market)]), ...factsOf(a.detail), ...heard].join(" · ")
}

type LiveSetting = "live.quiet_minutes" | "notify.live" | "live.zone" | "live.crash_pct" | (typeof LAYERS)[number][0]
const LAYERS = [
  ["live.auction", "竞价", "9:25 集合竞价后，开盘价已到线附近（1% 以内）或已越过"],
  ["live.near", "逼近", "离线 1% 以内、创当日新高（卖出侧新低）、在均价线上方（下方）"],
  ["live.touch", "触线", "价格碰到线；离线近的股票每 3 秒看一次快照"],
  ["live.hold", "持有中收盘前", "收盘前 10 分钟仍在失效线下：按你的计划收盘走"],
  ["live.crash", "大盘急跌", "所有人都发"],
  ["live.feed", "行情中断", "行情落后 3 分钟以上时说一声，恢复时也说"],
] as const

/** 提醒设置 (design 04, 2026-10-10 §8): which layers alert, the approach's zone and the market's fall, the first minutes
 *  without volume-type alerts, 潜伏 box tops of stocks you do not watch, the Feishu push. */
function AlertSettings() {
  const { data: s } = useSettings()
  const put = usePutSetting()
  const [open, setOpen] = useState(false)
  const save = (key: LiveSetting, value: number | boolean) => put.mutate({ key, value }, { onError: (e) => toast.error(String(e)) })
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}><BellRing className="size-4" />提醒设置</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
          <DialogHeader>
            <DialogTitle>提醒设置</DialogTitle>
            <DialogDescription>盘中提醒在价格碰到线之前提醒你注意：竞价开到线附近、逼近线、碰到线，由你判断要不要交易。潜伏股的规则在当天的临时 K 线上满足时也提醒，收盘后按正式规则确认或撤销。</DialogDescription>
          </DialogHeader>
          {s && (
            <div className="flex flex-col gap-4 text-[0.875rem]">
              <div className="flex flex-col gap-2 border-b border-border pb-4">
                <span className="text-[0.8125rem] text-muted-foreground">提醒哪几种（线：观察中的箱顶、持有中的失效线、你画的线）</span>
                {LAYERS.map(([key, name, hint]) => (
                  <label key={key} className="flex items-center justify-between gap-4">
                    <span>{name}<span className="block text-[0.8125rem] text-subtle-foreground">{hint}</span></span>
                    <Switch checked={s[key]} aria-label={name} onCheckedChange={(v) => save(key, v)} />
                  </label>
                ))}
                <label className="flex items-center justify-between gap-4">
                  <span>逼近：离线多少以内<span className="block text-[0.8125rem] text-subtle-foreground">越宽越早，也越容易落空（1% 时约六成当天碰线）</span></span>
                  <span className="flex items-center gap-1"><NumberField commitOn="blur" rule={{ min: 0.2, max: 3 }} className="w-16" aria-label="逼近区间（%）"
                    value={s["live.zone"]} onChange={(v) => save("live.zone", v!)} />%</span>
                </label>
                <label className="flex items-center justify-between gap-4">
                  <span>大盘急跌：指数跌到多少<span className="block text-[0.8125rem] text-subtle-foreground">上证、深成、创业板指（日股：日经 225、TOPIX）任一，每天一次</span></span>
                  <span className="flex items-center gap-1"><NumberField commitOn="blur" rule={{ min: 0.5, max: 5 }} className="w-16" aria-label="大盘急跌幅度（%）"
                    value={s["live.crash_pct"]} onChange={(v) => save("live.crash_pct", v!)} />%</span>
                </label>
              </div>
              <label className="flex items-center justify-between gap-4">
                <span>开盘后多少分钟内不发量能类预警<span className="block text-[0.8125rem] text-subtle-foreground">开盘头几分钟成交又多又乱，全天量还估不准</span></span>
                <NumberField commitOn="blur" rule={{ int: true, min: 0, max: 120 }} className="w-20" aria-label="开盘后不发量能类预警的分钟数"
                  value={s["live.quiet_minutes"]} onChange={(v) => save("live.quiet_minutes", v!)} />
              </label>
              <label className="flex items-center justify-between gap-4">
                <span>盘中提醒推送到飞书<span className="block text-[0.8125rem] text-subtle-foreground">观察中、待研究的箱顶，持有中的失效线，你画的线，大盘急跌和行情中断；规则预警和收盘确认只在这里</span></span>
                <Switch checked={s["notify.live"]} aria-label="盘中提醒推送到飞书" onCheckedChange={(v) => save("notify.live", v)} />
              </label>
              <p className="text-[0.8125rem] text-subtle-foreground">自己画的提醒线：在 K 线页用「提醒线」画，或右键已有的线开启提醒。</p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
