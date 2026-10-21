// 选股 (design 02, spec §7.1): a rule's latest whole-market scan — how far the listings got, stage by stage (the
// funnel; click a step to list the ones that reached it), and every listing that got anywhere, with its quote and
// the last 60 closes. 重新扫描 runs the rule on the latest snapshot; the table exports as CSV.
import { useQueryClient } from "@tanstack/react-query"
import { ChevronRight, Download, Eye, RefreshCw, X } from "lucide-react"
import { useEffect, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"
import { breakDistance } from "@/components/rules/distance"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { endpoints, type Market, useDataIn, useWatchlist } from "@/lib/api"
import { exportRows } from "@/lib/datause"
import { changeClass, codeOf, fmtBig, fmtPct, fmtPrice, inUnit, MARKET_NAME } from "@/lib/format"
import {
  type AlsoStatus, inBrief, inGroup, jobEndpoints, NO_VIEW, SCREEN_GROUPS, type ScreenGroup, type ScreenRow, screenRows, type ScreenView, scannedKey,
  stageSummary, toCSV, useBriefItems, useJobs, useScreen, WHERE_PRESETS, whereName,
} from "@/lib/jobs"
import { type Standings, useIndustries } from "@/lib/market"
import { industryName, stockIndustry } from "@/lib/marketWords"
import { pickedOf, toggleAll } from "@/lib/pick"
import { usePersisted, writePersisted } from "@/lib/usePersisted"
import { ruleEndpoints, useRules } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { DangerTags } from "@/components/DangerTags"
import { useDanger } from "@/lib/danger"
import { Empty, PageHeader } from "./common"
import { tagTone } from "./market/order"
import { standingLabel, standingOf } from "./market/standing"

const STATUS: Record<ScreenRow["status"], [string, string]> = {
  signal: ["命中", "text-structure"], watch: ["潜伏", "text-foreground"], unknown: ["无法判断", "text-warning"], fail: ["走到", "text-subtle-foreground"],   // got as far as the stage named after it, no signal
}

function Spark({ values, up }: { values: number[]; up: boolean }) {
  if (values.length < 2) return null
  const lo = Math.min(...values), hi = Math.max(...values)
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 76 + 2},${22 - ((v - lo) / (hi - lo || 1)) * 20}`).join(" ")
  // 80 px wide, narrower where the table has less room (10-17: fixed at 80 it took the table past the window just after
  // the column came on, 1250–1290 px windows at 16 px text) — it gives way, never the window
  return <svg viewBox="0 0 80 24" preserveAspectRatio="none" className="h-6 w-full max-w-20" aria-hidden>
    <polyline points={pts} fill="none" strokeWidth="1.2" vectorEffect="non-scaling-stroke" className={up ? "stroke-up" : "stroke-down"} /></svg>
}

/** A 附加条件 being written: Enter takes it (emptied: it goes), Esc leaves it as it was. */
function WhereInput({ value, onDone }: { value: string; onDone: (v: string | null) => void }) {
  return (
    <Input aria-label="附加条件公式" defaultValue={value} autoFocus placeholder="例如 MA(BAR_LEN, 10) <= 0.025"
      title="和规则里一样写公式，回车生效（清空再回车就去掉），Esc 不改；在扫描那天成立的才列出来。两个市场分开写：CN: …; JP: …"
      onKeyDown={(e) => { if (e.key === "Enter") onDone(e.currentTarget.value); else if (e.key === "Escape") onDone(null) }}
      className="num h-7 w-72 font-mono text-[0.8125rem]" />
  )
}

/** 行业 / 题材 (design 2026-10-12-market-page-redesign §6.1): the stock's 申万 一级 · 二级 (日股 33 業種) — a link to that
 *  industry on the 市场 page — with its standing there (change, rank, tag); then the 题材 tags. Wraps, never cut. */
function IndustryCell({ row, standings }: { row: ScreenRow; standings: Standings | undefined }) {
  const words = stockIndustry(row.industry, row.industry2)
  const level: "l1" | "l2" = row.industry2 ? "l2" : "l1"
  const name = row.industry2 ?? row.industry
  const s = standingOf(standings, level, name)
  return (
    // a narrow table (Windows at 1200 × 760, large text) keeps the name and the change, each whole; the rank and the tag
    // come with room (the cell's hover says them) — squeezed to 84 px they took six lines a row, three rows a screen
    <span className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 leading-snug" data-testid="screen-industry-cell"
      title={s && standings ? `${words ?? ""} ${fmtPct(s.chg)}${s.rank != null ? ` · 第 ${s.rank} / ${standings.of[level]}` : ""}${s.tag ? ` · ${s.tag}` : ""}` : undefined}>
      {words && name ? <Link to={`/market?level=${level}&industry=${encodeURIComponent(name)}`} className="hover:underline"
        title="在市场页看这个行业">{words}</Link> : <span>—</span>}
      {s && standings && <span className={cn("num whitespace-nowrap", changeClass(s.chg))}>{fmtPct(s.chg)}</span>}
      {s && standings && s.rank != null && <span className="num hidden text-[0.75rem] whitespace-nowrap text-subtle-foreground @min-[60rem]:inline">第 {s.rank} / {standings.of[level]}</span>}
      {s?.tag && <span className={cn("hidden rounded px-1 text-[0.6875rem] font-medium whitespace-nowrap @min-[60rem]:inline", tagTone(s.tag))}>{s.tag}</span>}
      {row.tags.map((t) => <span key={t} className="rounded border border-border px-1 text-[0.75rem]">{t}</span>)}
    </span>
  )
}

export default function ScreenPage() {
  const [params, setParams] = useSearchParams()
  const qc = useQueryClient()
  const { data: rules = [] } = useRules()
  const usable = rules.filter((r) => !r.error && !r.archived).sort((a, b) => Number(b.enabled) - Number(a.enabled))
  const ruleId = params.get("rule") ?? usable.find((r) => r.enabled)?.id ?? usable[0]?.id
  // 同时满足另一条规则: the listings the other rule's latest scan also hit (or has in 潜伏), in the address so it can be kept
  const alsoId = params.get("also") && params.get("also") !== ruleId ? params.get("also") : null
  const alsoStatus: AlsoStatus = params.get("also_status") === "watch" ? "watch" : "signal"
  const also = alsoId ? { id: alsoId, status: alsoStatus } : null
  const setAlso = (id: string | null, status: AlsoStatus = alsoStatus) => {
    const next = new URLSearchParams(params)
    if (id) { next.set("also", id); next.set("also_status", status) } else { next.delete("also"); next.delete("also_status") }
    setParams(next)
    setStep(null)
  }
  // 附加条件 (any number): formulas the listed stocks must all hold for on the scan's day — any rule, no rule to write
  // (in the address too, one `where` each)
  const wheres = [...new Set(params.getAll("where").map((w) => w.trim()).filter(Boolean))]
  const [editing, setEditing] = useState<number | "new" | null>(null)   // the one being written: its place, or a new one
  const setWheres = (list: string[]) => {
    const next = new URLSearchParams(params)
    next.delete("where")
    for (const w of new Set(list.map((x) => x.trim()).filter(Boolean))) next.append("where", w)
    setParams(next)
    setStep(null)
    setEditing(null)
  }
  const { data: jobs } = useJobs()
  // 行业 (from the 市场 page, design 2026-10-12-market-page-redesign §6.1): only the rows in that industry on the scan day
  const industry = params.get("industry")
  const setIndustry = (v: string | null) => {
    const next = new URLSearchParams(params)
    if (v) next.set("industry", v); else next.delete("industry")
    setParams(next)
    setStep(null)
  }
  const screen = useScreen(ruleId, scannedKey(jobs), also, wheres, industry)
  // 开盘前简报 (2026-10-10): this view — the rule, its 附加条件, 同时满足 — as one of the briefing's items
  const { data: items } = useBriefItems()
  const inItems = ruleId && items ? inBrief(items, ruleId, also, wheres) : undefined
  const notOn = [ruleId, alsoId].map((id) => usable.find((r) => r.id === id)).find((r) => r && !r.enabled)
  const putIn = async () => {
    try {
      qc.setQueryData(["brief-items"], await jobEndpoints.addBriefItem({ rule_id: ruleId!, wheres, also: alsoId ?? null,
        also_status: alsoId ? alsoStatus : null, mode: "list" }))
      toast.success("已放进开盘前简报：每个交易日开盘前发；在 设置 → 通知 里排顺序、改成只说数量")
    } catch (e) {
      toast.error(String(e))
    }
  }
  const s = screen.data
  const [market, setMarket] = useState<Market | "all">("all")
  const dataIn = useDataIn()
  const [step, setStep] = useState<number | null>(null)       // funnel step: rows that reached stage `step`
  const [q, setQ] = useState("")
  const [group, setGroup] = useState<ScreenGroup>("shown")
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<Market | null>(null)   // the market being scanned again
  const { data: groups = [] } = useWatchlist()

  const markets = (s?.rule.markets ?? []).filter((m) => s?.markets[m])
  const oneMarket = market !== "all" || markets.length < 2   // the rows' market goes without saying
  const inMarket = (m: Market) => market === "all" || market === m
  // the markets' funnels added up, when their scans have the same stages
  const parts = markets.filter(inMarket).map((m) => s!.markets[m]!.funnel).filter((f) => f !== null)
  const funnel = parts.length && parts.every((f) => f.length === parts[0].length)
    ? parts[0].map((_, i) => parts.reduce((n, f) => n + f[i], 0)) : null
  const stages = markets.length ? s!.markets[markets[0]]!.stages : s?.stages ?? []
  const watched = new Set(groups.flatMap((g) => g.items.map((i) => i.listing_id)))
  const ranks = (s?.ranking ?? []).slice(0, 2)
  const inScope = (s?.rows ?? []).filter((r) => inMarket(r.market))
  // each row's industry standing as the 市场 page shows it now (redesign §6.1)
  const { data: cnStanding } = useIndustries("CN", (s?.rows ?? []).some((r) => r.market === "CN"))
  const { data: jpStanding } = useIndustries("JP", (s?.rows ?? []).some((r) => r.market === "JP"))
  const industryNote = cnStanding ? standingLabel(cnStanding) : jpStanding ? standingLabel(jpStanding) : null
  const rows = screenRows(s, { market, group, step, q })
  const dangerQ = useDanger("CN", rows.filter((r) => r.market === "CN").map((r) => r.listing_id))   // 危险提醒's tags: shown, never filtering
  const shownIds = rows.map((r) => r.listing_id)
  const allPicked = pickedOf(shownIds, picked)
  // what this page shows, kept: the K 线 page's 命中 lists the same stocks (J / K walk them)
  const [, setView] = usePersisted<ScreenView>("screen.view", NO_VIEW)
  const viewKey = JSON.stringify([ruleId ?? null, also, wheres, market, group, step, q, industry])
  useEffect(() => {
    setView({ rule: ruleId ?? null, also, where: wheres, market, group, step, q, industry })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the key is all of them
  }, [viewKey])
  const stageName = (r: ScreenRow) => (r.depth != null && r.depth >= 0 ? stages[r.depth]?.label : null)

  const rescan = async () => {
    if (!s) return
    // a market without data is not run (10-13: 日股 on 测试档 with nothing in it ended every 重新扫描 with 「JP 还没有数据快照」
    // after A 股 had been scanned, and the list was not refreshed)
    const ms = s.rule.markets.filter((m) => dataIn.has(m))
    if (!ms.length) { toast.error("这条规则的市场都还没有数据"); return }
    try {
      for (const m of ms) {
        setBusy(m)
        await ruleEndpoints.run({ rule_id: s.rule.id }, m)
      }
      toast.success("扫描完成")
    } catch (e) {
      toast.error(String(e))
    } finally {
      setBusy(null)
      void qc.invalidateQueries({ queryKey: ["screen"] })   // what did run is listed, whatever came after
    }
  }
  const exportCSV = async () => {
    const kept = await exportRows(qc, rows, (r) => r.market)   // 数据使用 (§4.10): a market kept from export is left out
    if (!kept) return
    const csv = toCSV(["市场", "代码", "名称", "阶段", "信号日", "近 5 日信号", "摘要", ...ranks.map((k) => `排序：${k.label}`),
      "距离", "收盘", "涨跌幅", "成交额", "行业", "二级行业", "题材", "已观察"],
      kept.map((r) => [r.market, codeOf(r.listing_id), r.name, STATUS[r.status][0] + (stageName(r) ? ` · ${stageName(r)}` : ""),
        r.signal_date, r.recent_signal, stageSummary(r.detail), ...ranks.map((k, i) => inUnit(r[`rank${i}`], k.unit)), breakDistance(r.detail),
        r.close, r.change == null ? null : (r.change * 100).toFixed(2) + "%", r.amount, r.industry, r.industry2 ? industryName(r.industry2) : null,
        r.tags.join("、"), watched.has(r.listing_id) ? "是" : ""]))
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }))
    a.download = `选股-${s?.rule.id}-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const watchPicked = async () => {
    const g = groups[0]
    if (!g) return
    const source = s ? `选股：规则「${s.rule.name}」v${s.rule.version}` : "选股"
    try {
      const r = await endpoints.addWatchMany(g.id, [...picked], source)   // one request for them all
      toast(`已把 ${picked.size} 只加入「${g.name}」${r.added < picked.size ? `（其中 ${picked.size - r.added} 只本来就在观察）` : ""}`)
      setPicked(new Set())
    } catch (e) {
      toast.error(`加入观察失败：${String(e)}`)
    } finally {
      void qc.invalidateQueries({ queryKey: ["watchlist"] })
      void qc.invalidateQueries({ queryKey: ["watch"] })
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-3">选股
        <select aria-label="规则" value={ruleId ?? ""} onChange={(e) => {
          setParams({ rule: e.target.value, ...(alsoId && alsoId !== e.target.value ? { also: alsoId, also_status: alsoStatus } : {}) })
          setStep(null); setPicked(new Set())
        }}
          className="no-drag h-7 max-w-[20rem] min-w-[7rem] rounded-md border border-input px-2 text-[0.875rem] font-normal">
          {usable.map((r) => <option key={r.id} value={r.id}>{r.enabled ? "● " : ""}{r.name} v{r.version}</option>)}
        </select>
        <span role="tablist" className="no-drag flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {(["all", ...(s?.rule.markets ?? [])] as const).map((m) => (
            <button key={m} type="button" role="tab" aria-selected={market === m} onClick={() => setMarket(m)}
              className={cn("rounded px-2 py-0.5", market === m ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
              {m === "all" ? "全部市场" : MARKET_NAME[m]}
            </button>
          ))}
        </span>
        {/* the day is never cut: the rule's list gives way; a narrow toolbar (Windows keeps its buttons' room) says the days alone */}
        {(() => {
          const days = markets.map((m) => `${MARKET_NAME[m]} ${s!.markets[m]!.day.slice(5)}`).join(" · ")
          return (
            <span className="num shrink-0 text-[0.8125rem] font-normal text-subtle-foreground" data-testid="screen-asof"
              title={markets.length ? `截至 ${days} 收盘` : undefined}>
              {markets.length ? <><span className="@max-[56rem]:hidden">截至 </span>{days}<span className="@max-[56rem]:hidden"> 收盘</span></> : ""}
            </span>
          )
        })()}</span>}>
        {/* a narrow toolbar (Windows keeps its buttons' room): the icon alone, so the day stays whole */}
        <Button size="sm" variant="ghost" disabled={!rows.length} onClick={exportCSV} aria-label="导出 CSV" title="导出 CSV">
          <Download className="size-4" /><span className="@max-[56rem]:hidden">导出 CSV</span></Button>
        <Button size="sm" variant="outline" disabled={!s || !!busy} onClick={() => void rescan()} title="按最新数据把这条规则在每个市场重跑一次"><RefreshCw className={cn("size-4", busy && "animate-spin")} />{busy ? `正在扫描${/^[A-Za-z]/.test(MARKET_NAME[busy]) ? " " : ""}${MARKET_NAME[busy]}…` : "重新扫描"}</Button>
      </PageHeader>
      <div className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1 border-b border-border px-4 py-1.5 text-[0.8125rem] text-muted-foreground" data-testid="screen-filters">
        {industry && (
          <span className="flex h-7 items-center rounded-md border border-border bg-accent/50 text-foreground" data-testid="screen-industry"
            title={`只列${industry.startsWith("l2:") ? "申万二级" : "这个行业"}「${industry.slice(3)}」里的（按扫描那天的归属）`}>
            <span className="px-2">行业：{industryName(industry.slice(3))}</span>
            <button type="button" aria-label="去掉行业筛选" className="pr-1.5 text-subtle-foreground hover:text-foreground" onClick={() => setIndustry(null)}><X className="size-3.5" /></button>
          </span>
        )}
        <span className="flex items-center gap-1">同时满足
          <select aria-label="同时满足另一条规则" value={alsoId ?? ""} onChange={(e) => setAlso(e.target.value || null)}
            className="h-7 max-w-[16rem] min-w-[5rem] rounded-md border border-input px-2 text-[0.8125rem] text-foreground">
            <option value="">（不限）</option>
            {usable.filter((r) => r.id !== ruleId).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          {alsoId && (
            <select aria-label="另一条规则要怎样" value={alsoStatus} onChange={(e) => setAlso(alsoId, e.target.value as AlsoStatus)}
              className="h-7 rounded-md border border-input px-1.5 text-[0.8125rem] text-foreground">
              <option value="signal">命中</option>
              <option value="watch">命中或潜伏</option>
            </select>
          )}
        </span>
        <span className="flex flex-wrap items-center gap-1">附加条件
          {wheres.map((w, i) => editing === i
            ? <WhereInput key={`edit-${w}`} value={w} onDone={(v) => (v === null ? setEditing(null) : setWheres(wheres.map((x, j) => (j === i ? v : x))))} />
            : (
              <span key={w} className="flex h-7 items-center rounded-md border border-border bg-accent/50 text-foreground">
                <button type="button" className="num px-2" title={`${WHERE_PRESETS.find((p) => p.expr === w)?.hint ?? ""}（${w}）点一下改`}
                  onClick={() => setEditing(i)}>{whereName(w)}</button>
                <button type="button" aria-label={`去掉附加条件 ${whereName(w)}`} className="pr-1.5 text-subtle-foreground hover:text-foreground"
                  onClick={() => setWheres(wheres.filter((_, j) => j !== i))}><X className="size-3.5" /></button>
              </span>
            ))}
          {editing === "new" ? <WhereInput value="" onDone={(v) => (v === null ? setEditing(null) : setWheres([...wheres, v]))} /> : (
            <select aria-label="添加附加条件" value="" title="给任何规则的结果再加条件，例如近期 K 线短小、小盘股；可以加好几个，要同时满足；不用新建规则"
              onChange={(e) => { const v = e.target.value; if (v === "custom") setEditing("new"); else if (v) setWheres([...wheres, v]) }}
              className="h-7 rounded-md border border-input px-2 text-[0.8125rem] text-foreground">
              <option value="">{wheres.length ? "＋ 再加一个" : "（不限）"}</option>
              {WHERE_PRESETS.filter((p) => !wheres.includes(p.expr)).map((p) => <option key={p.expr} value={p.expr}>{p.label}</option>)}
              <option value="custom">自定义公式…</option>
            </select>
          )}
        </span>
        <span className="ml-auto">
          {inItems ? <Link to="/settings?s=notify" className="hover:text-foreground hover:underline" title="在 设置 → 通知 里排顺序、改成只说数量或去掉">已在开盘前简报里</Link>
            : <Button size="xs" variant="outline" disabled={!ruleId || !!notOn || !s} onClick={() => void putIn()}
                title={notOn ? `「${notOn.name}」没有启用：先在规则库里启用它，每天收盘后才有它的扫描`
                  : `把这个视图（规则、附加条件、同时满足）放进每天的开盘前简报${industry ? "（行业筛选不放进简报）" : ""}`}>
                放进开盘前简报</Button>}
        </span>
      </div>
      {screen.error && <div className="px-6 py-10 text-center text-[0.875rem] text-error" data-testid="screen-error">读不出来：{String(screen.error)}</div>}
      {s?.also && (
        <div className="shrink-0 border-b border-border px-4 py-1.5 text-[0.8125rem] text-muted-foreground" data-testid="screen-also">
          只列「{s.also.name}」也{s.also.status === "signal" ? "命中" : "命中或潜伏"}的：
          {markets.filter(inMarket).map((m) => {
            const a = s.markets[m]!.also
            if (!a) return null
            return <span key={m} className={cn("ml-2", !a.same_day && "text-warning")}>{MARKET_NAME[m]} {a.day ? `${a.day.slice(5)} 那次扫描 ${a.count} 只` : "那条规则还没在这里扫描过"}
              {a.day && !a.same_day ? `（和这条规则的 ${s.markets[m]!.day.slice(5)} 不是同一天，点两条规则各自的「重新扫描」对齐）` : ""}</span>
          })}
        </div>
      )}
      {wheres.length > 0 && (screen.isFetching || markets.length > 0) && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-4 py-1.5 text-[0.8125rem] text-muted-foreground" data-testid="screen-where">
          <span>只列{wheres.length > 1 ? "同时满足这些附加条件" : "满足附加条件"}的：</span>
          {screen.isFetching ? <span>按扫描那天的数据计算中…</span> : markets.filter(inMarket).map((m) => {
            const ws = s?.markets[m]?.where
            if (!ws) return null
            return (
              <span key={m}>{MARKET_NAME[m]} {s!.markets[m]!.day.slice(5)} 全市场：
                {ws.map((w, i) => (
                  <span key={i} className={cn(i > 0 && "ml-1.5", w.error && "text-warning")}>{i ? "· " : ""}{whereName(s!.where?.[i]?.expr ?? "")}{" "}
                    {w.error ? `读不了：${w.error}` : w.skip ? "没有给这个市场写，不筛"
                      : `${w.count?.toLocaleString()} 只${w.unknown ? `（${w.unknown} 只数据不够，不算）` : ""}`}</span>
                ))}
              </span>
            )
          })}
        </div>
      )}
      {s && !markets.length && <Empty>这条规则还没有全市场扫描。启用后每天收盘后自动扫描，或点「重新扫描」现在跑一次。</Empty>}
      {funnel && (
        <div className="flex shrink-0 items-stretch gap-1 overflow-x-auto border-b border-border px-4 py-3" data-testid="funnel">
          {[{ label: "全部", head: "全部股票" }, ...stages].map((st, i) => (
            <button key={i} type="button" title={st.head} onClick={() => setStep(i === 0 ? null : i - 1)}
              className={cn("flex min-w-[6rem] max-w-[15rem] items-center gap-2 rounded-md border px-3 py-1.5 text-left transition-colors",
                (i === 0 ? step === null : step === i - 1) ? "border-ring/40 bg-accent" : "border-border hover:bg-accent/60")}>
              <span className="min-w-0">
                <span className="block truncate text-[0.75rem] text-muted-foreground">{i === 0 ? "全部" : `${"①②③④⑤⑥⑦⑧⑨⑩"[i - 1] ?? i} ${st.label}`}</span>
                <span className="num block text-[1.1875rem]">{funnel[i]?.toLocaleString()}</span>
              </span>
              {i < stages.length && <ChevronRight className="size-4 shrink-0 text-subtle-foreground" />}
            </button>
          ))}
        </div>
      )}
      <div className="flex shrink-0 items-center gap-3 px-4 py-2">
        <Input aria-label="筛选名称或代码" value={q} onChange={(e) => setQ(e.target.value)} placeholder="筛选名称或代码" className="h-8 w-56 text-[0.875rem]" />
        <span className="flex items-center gap-1 text-[0.8125rem]">
          {SCREEN_GROUPS.map(([g, label]) => (
            <button key={g} type="button" aria-pressed={step === null && group === g} onClick={() => { setGroup(g); setStep(null) }}
              className={cn("rounded-md border px-2 py-0.5", step === null && group === g ? "border-ring text-foreground" : "border-border text-muted-foreground hover:text-foreground")}>
              {label} <span className="num text-subtle-foreground">{inScope.filter((r) => inGroup(r, g)).length}</span>
            </button>
          ))}
        </span>
        <span className="flex-1" />
        <span className="num text-[0.8125rem] text-subtle-foreground">{rows.length} 只{step === null ? "" : " · 到过这一步的"}</span>
      </div>
      {/* a container: where the table has no room for every column (a small window, large text) the 60-day line goes first */}
      <div className="@container min-h-0 flex-1 overflow-auto px-4">
        <table className="w-full text-[0.875rem]">
          <thead className="sticky top-0 bg-background text-[0.8125rem] text-muted-foreground">
            {/* the headers never break (Windows' fonts and scrollbars took 距离 to a character a line); the rankings' do */}
            <tr className="h-8 border-b border-border [&>th]:px-1.5 [&>th]:font-normal [&>th]:whitespace-nowrap">
              <th className="w-8">{rows.length > 0 && (
                <input type="checkbox" aria-label="全选列出的股票" title="全选（按现在的筛选列出的）" checked={allPicked === "all"}
                  ref={(el) => { if (el) el.indeterminate = allPicked === "some" }}
                  onChange={() => setPicked((p) => toggleAll(shownIds, p))} />)}</th><th className="text-left">名称</th><th className="text-left">阶段</th><th className="text-left">信号日</th>
              <th className="text-left">摘要</th>
              {/* a ranking by its name; one without a name says which, its formula in the hover */}
              {ranks.map((k, i) => <th key={i} className="min-w-[4.5rem] max-w-[5.5rem] text-right leading-tight whitespace-normal!"
                title={`排序 ${i + 1}：${k.label}${k.label === k.expr ? "" : `（${k.expr}）`}，${k.dir === "desc" ? "大的在前" : "小的在前"}`}>
                {k.label === k.expr ? `排序 ${i + 1}` : k.label}</th>)}
              <th className="text-right">距离</th><th className="text-right">收盘</th><th className="text-right">涨跌幅</th>
              <th className="text-right">成交额</th>
              <th className="text-left leading-tight" title="申万一级 · 二级（日股 33 業種），后面是这个行业在市场页上的涨跌、名次和强弱">
                <span className="block whitespace-nowrap">行业 / 题材</span>
                {industryNote && <span className="block text-[0.6875rem] whitespace-nowrap text-subtle-foreground">{industryNote}</span>}</th>
              <th className="hidden text-left @min-[60rem]:table-cell">近 60 日</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const [label, tone] = STATUS[r.status]
              return (
                <tr key={r.listing_id} data-testid="screen-row" className="h-9 border-b border-border/60 hover:bg-accent/40 [&>td]:px-1.5">
                  <td><input type="checkbox" aria-label={`选择 ${r.name ?? r.listing_id}`} checked={picked.has(r.listing_id)}
                    onChange={() => setPicked((p) => { const n = new Set(p); if (n.has(r.listing_id)) n.delete(r.listing_id); else n.add(r.listing_id); return n })} /></td>
                  {/* a long name gives way (cut, whole on hover) — a Japanese company's ran the column 350 px wide and squeezed 摘要;
                      the warnings' tags go under it, as wide as the name's column and no part of its width (w-0 min-w-full): the
                      column does not move when they come (10-17: three of them, beside the name, took it 450 px wide) */}
                  <td><div className="whitespace-nowrap">
                    <Link to={`/k/${r.listing_id}`} className="inline-block max-w-[9rem] truncate align-bottom hover:underline"
                    title={`${r.name ?? codeOf(r.listing_id)} — 在 K 线页打开：右边「命中」就是这份名单，J / K 逐只看`}
                    onClick={() => { writePersisted("kline.tab", "hits"); writePersisted("kline.rule", true); writePersisted("kline.ruleId", ruleId ?? null) }}>{r.name ?? codeOf(r.listing_id)}</Link>
                    <span className="num ml-2 text-[0.75rem] text-subtle-foreground">{codeOf(r.listing_id)}{oneMarket ? "" : ` ${r.market}`}</span>
                    {watched.has(r.listing_id) && <Eye className="ml-1.5 inline size-3.5 text-subtle-foreground" aria-label="已在观察" />}</div>
                    <DangerTags ws={dangerQ.data?.warnings[r.listing_id]} className="mt-0.5 flex w-0 min-w-full flex-wrap" /></td>
                  <td className={tone} title={r.status === "unknown" ? `无法判断：${(r.detail?.unknown_reasons ?? []).map((u) => u.kind).join("、") || "数据不全"}`
                    : r.status === "fail" ? "过了这一阶段，后面的没过：没出信号" : undefined}>   {/* the stage under it where the table is tight */}
                    <span className="whitespace-nowrap">{label}</span>{r.status !== "signal" && stageName(r)
                      ? <> <span className="text-[0.8125rem] whitespace-nowrap text-subtle-foreground">· {stageName(r)}</span></> : null}</td>
                  {/* the date whole; 再次 goes under it where the table is tight (18 px text at 1200 px, 10-17) */}
                  <td className="num text-muted-foreground"><span className="whitespace-nowrap">{r.signal_date?.slice(5) ?? (r.recent_signal   // an earlier one, dotted and explained on hover: the column stays narrow at 特大
                    ? <abbr title={`近 5 日的信号（${r.recent_signal}），今天没有`} className="text-subtle-foreground">{r.recent_signal.slice(5)}</abbr> : "")}</span>
                    {r.repeat && <> <abbr title="同一个结构第二次（或更多次）突破：之前突破过、回到箱里，现在又突破。回测、事件研究只算第一次"
                      className="inline-block rounded border border-border px-1 text-[0.6875rem] whitespace-nowrap text-subtle-foreground no-underline">再次</abbr></>}</td>
                  <td className="min-w-[8rem] max-w-[17.5rem] text-[0.8125rem] leading-snug text-muted-foreground" title={stageSummary(r.detail)}>
                    <div className="line-clamp-2">{stageSummary(r.detail)}</div></td>
                  {/* figures and dates never break inside */}
                  {ranks.map((k, i) => <td key={i} className="num text-right whitespace-nowrap text-muted-foreground">{inUnit(r[`rank${i}`], k.unit)}</td>)}
                  <td className="num text-right leading-snug text-muted-foreground">   {/* 待确认 on a line of its own; the figure under its words where the table is tight */}
                    {(breakDistance(r.detail) ?? "").split(" · ").map((t, j) => <span key={j} className="block">
                      {t.split(" ").map((w, k) => <span key={k}>{k > 0 && " "}<span className="whitespace-nowrap">{w}</span></span>)}</span>)}</td>
                  <td className="num text-right whitespace-nowrap">{fmtPrice(r.close, r.market)}</td>
                  <td className={cn("num text-right whitespace-nowrap", changeClass(r.change))}>{fmtPct(r.change)}</td>
                  <td className="num text-right whitespace-nowrap text-muted-foreground">{fmtBig(r.amount, r.market)}</td>
                  <td className="max-w-[11.25rem] text-muted-foreground"><IndustryCell row={r} standings={r.market === "CN" ? cnStanding : jpStanding} /></td>
                  <td className="hidden w-20 @min-[60rem]:table-cell"><Spark values={r.spark ?? []} up={(r.spark?.at(-1) ?? 0) >= (r.spark?.[0] ?? 0)} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {s && market !== "all" && !s.markets[market] ? (   // this market has no scan of the rule: why, not 没有符合的股票
          <Empty>{dataIn.has(market) ? `${MARKET_NAME[market]}还没有这条规则的扫描：每天收盘、数据就绪后扫描，也可以点「重新扫描」` : dataIn.none(market)}</Empty>
        ) : s && markets.length > 0 && rows.length === 0 && <Empty>没有符合的股票</Empty>}
      </div>
      <div className="flex h-10 shrink-0 items-center gap-3 border-t border-border px-4 text-[0.8125rem] text-subtle-foreground">
        <span className="num">已选 {picked.size} · 显示 {rows.length} / {(s?.rows ?? []).length}</span>
        {markets.filter((m) => inMarket(m) && s!.markets[m]!.total > s!.markets[m]!.listed).map((m) => (
          <span key={m} className="text-warning">{m} 只列出了前 {s!.markets[m]!.listed} 只（共 {s!.markets[m]!.total} 只走到了某个阶段，按排序取前面的）</span>
        ))}
        <span className="flex-1" />
        <Button size="sm" variant="outline" disabled={!picked.size || !groups.length} onClick={watchPicked}><Eye className="size-4" />加入观察</Button>
      </div>
    </div>
  )
}
