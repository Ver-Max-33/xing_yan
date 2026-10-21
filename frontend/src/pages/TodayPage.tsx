// 今日 (design 01, spec §7.1): what the latest scan of each enabled rule changed against its previous run — new
// signals, watch entries that moved on or fell out, new watch entries — and data problems, market by market. One
// market or both: the list and the scan button follow the choice; a running scan says which rule and step it is at.
// The panel shows the stock's chart with the structures the rule stands on and why it matched, from the rule itself.
// Keys: J/K move, Enter opens the K-line page, W watches the stock, E sets the event aside for the day.
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { BarChart3, BellRing, ChevronDown, ChevronRight, Circle, CircleAlert, CircleCheck, CircleDashed, CircleDot, CircleX, Eye, Hourglass,
  Loader2, Play } from "lucide-react"
import { memo, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { Link, useNavigate } from "react-router"
import { toast } from "sonner"
import { DetailCard } from "@/components/rules/DetailCard"
import { MiniChart } from "@/components/rules/MiniChart"
import { RunControls } from "@/components/RunControls"
import { RunStrip } from "@/components/RunStrip"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { type Market, useToggleWatch, useWatchlist } from "@/lib/api"
import { codeOf, MARKET_NAME, pricePrecision, shortDate } from "@/lib/format"
import { type ChangeKind, doing, type EventKind, eventKey, eventLine, type FirstScan, type JobState, jobEndpoints, KIND_LABEL, scanDone,
  scannedKey, type TodayEvent, useJobs, useToday } from "@/lib/jobs"
import { ruleEndpoints, useRules } from "@/lib/rules"
import { usePersisted } from "@/lib/usePersisted"
import { cn } from "@/lib/utils"
import { DangerTags } from "@/components/DangerTags"
import { type Danger, useDanger } from "@/lib/danger"
import { leaveToFocus } from "@/lib/keys"
import { PageHeader } from "./common"

const KINDS: EventKind[] = ["signal", "live", "advance", "invalid", "watch", "data"]
const ICON: Record<EventKind, [typeof Circle, string]> = {
  signal: [CircleDot, "text-structure"], live: [BellRing, "text-warning"], advance: [CircleDashed, "text-foreground"],
  invalid: [CircleX, "text-muted-foreground"],
  watch: [Hourglass, "text-structure/70"], data: [CircleAlert, "text-warning"],   // 潜伏: in the box, waiting
}
const MARKETS: Market[] = ["CN", "JP"]
const SCAN_LABEL = { all: "扫描全部市场", CN: "扫描 A 股", JP: "扫描日股" } as const
const CHANGES: [ChangeKind, string][] = [["signal", "新信号"], ["advance", "潜伏推进"], ["invalid", "失效"], ["watch", "新潜伏"]]
const ASIDE = "today.aside"
const OPENED = "today.opened"   // the rows you have looked at: drawn as read
const PICKED = "today.picked"   // the row you are on, for this session: ↵ → K 线 → back comes back to it
const PAGE = 50   // rows a kind draws at first, and each 再显示: thousands at once would freeze the page

function loadKeys(name: string): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(name) ?? "[]") as string[]) } catch { return new Set() }
}
function saveKeys(name: string, keys: Set<string>) {
  try { localStorage.setItem(name, JSON.stringify([...keys].slice(-2000))) } catch { /* private window: kept for this visit */ }
}

export default function TodayPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data: jobs } = useJobs()
  const scanned = scannedKey(jobs)
  const { data: feed, isFetched, error } = useToday(scanned)
  const [market, setMarket] = usePersisted<Market | "all">("today.market", "all")   // what the page lists
  // what the scan button runs: chosen beside it (the tabs above only filter the list), the last choice remembered
  const [scanFor, setScanFor] = usePersisted<Market | "all">("today.scan", "all")
  const [tab, setTab] = useState<EventKind | "all">("all")
  const [aside, setAside] = useState(() => loadKeys(ASIDE))
  const [opened, setOpened] = useState(() => loadKeys(OPENED))
  // the row you are on, remembered for the session (not in the address: a router navigation per key — each one a
  // transition — starved the selection's own updates, and a run of J stopped dead at row 11 of 30)
  const [picked, setPickedState] = useState(() => { try { return sessionStorage.getItem(PICKED) } catch { return null } })
  const setPicked = useCallback((key: string | null) => {
    setPickedState(key)
    try { if (key) sessionStorage.setItem(PICKED, key); else sessionStorage.removeItem(PICKED) } catch { /* kept in the page */ }
  }, [])
  const [more, setMore] = useState<Record<string, number>>({})   // how many rows a section draws (PAGE at first)
  const all = useMemo(() => (feed?.events ?? []).filter((e) => !aside.has(eventKey(e))), [feed, aside])
  const events = useMemo(() => (market === "all" ? all : all.filter((e) => e.market === market)), [all, market])
  const dangerQ = useDanger("CN", events.filter((e) => e.market === "CN" && e.listing_id).map((e) => e.listing_id!))   // 危险提醒's tags
  const states = jobs?.markets ?? []
  const targets = states.filter((j) => market === "all" || j.market === market)
  // drawn by market, then by kind, each kind a page at a time — J/K walk what is drawn, in that order. Kept apart from
  // the jobs (asked for every 2 s while a scan runs): thousands of rows are not drawn again on every answer
  const sections = useMemo(() => (market === "all" ? MARKETS : [market]).map((m) => ({
    market: m,
    first: (feed?.first ?? []).filter((f) => f.market === m),
    kinds: KINDS.filter((k) => tab === "all" || tab === k).map((k) => {
      const list = events.filter((e) => e.market === m && e.kind === k)
      return { kind: k, key: `${m}|${k}`, list, drawn: list.slice(0, more[`${m}|${k}`] ?? PAGE) }
    }).filter((x) => x.list.length),
  })).filter((x) => x.kinds.length || x.first.length), [market, feed, events, tab, more])
  const shown = useMemo(() => sections.flatMap((x) => x.kinds.flatMap((k) => k.drawn)), [sections])
  const current = shown.find((e) => eventKey(e) === picked) ?? shown[0]
  const scanTargets = (m: Market | "all") => states.filter((j) => m === "all" || j.market === m)
  const busy = scanTargets(scanFor).length > 0 && scanTargets(scanFor).every((j) => j.running)
  const paused = scanTargets(scanFor).filter((j) => j.status === "paused" && !j.running)
  const stopped = states.filter((j) => j.status === "paused" && !j.running)   // every market's, whatever is listed
  const { data: rules = [] } = useRules()
  const ready = {   // what a first use of the tab's markets still lacks: data, a rule to scan with, a scan (日股 with no
    // data said 取数据 ✓ when A 股 had it, 2026-10-11)
    data: targets.some((j) => j.latest), rules: rules.filter((r) => r.enabled).length, scanned: targets.some((j) => j.scan_at),
  }
  const noData = !scanTargets(scanFor).some((j) => j.latest)

  const scanNow = async (m: Market | "all" = scanFor) => {
    setScanFor(m)
    try {
      for (const j of scanTargets(m)) {
        if (!j.latest) {   // a market with no data yet: said when it is the one you chose
          if (m !== "all") toast(`${MARKET_NAME[j.market]}还没有数据：先到数据页补历史`)
          continue
        }
        const started = j.status === "paused" ? (await jobEndpoints.resume(j.market)).started : (await jobEndpoints.run(j.market)).started
        if (!started) toast(`${MARKET_NAME[j.market]}正在运行，等它结束再试`)
      }
    } catch (e) {
      toast.error(String(e))
    }
    void qc.invalidateQueries({ queryKey: ["jobs"] })
  }
  const pause = async (m: Market) => {
    try {
      if (!(await jobEndpoints.pause(m)).paused) toast(`${MARKET_NAME[m]}这会儿不在扫描（取数据时不能暂停）`)
    } catch (e) {
      toast.error(String(e))
    }
    void qc.invalidateQueries({ queryKey: ["jobs"] })
  }
  const cancel = async (m: Market) => {
    try {
      if (!(await jobEndpoints.cancel(m)).cancelled) toast(`${MARKET_NAME[m]}这会儿没有可以取消的扫描`)
    } catch (e) {
      toast.error(String(e))
    }
    void qc.invalidateQueries({ queryKey: ["jobs"] })
  }
  const resume = async (m: Market) => {
    try {
      if (!(await jobEndpoints.resume(m)).started) toast(`${MARKET_NAME[m]}没有接着扫：它正在运行，或者已经不在暂停`)
    } catch (e) {
      toast.error(String(e))
    }
    void qc.invalidateQueries({ queryKey: ["jobs"] })
  }
  const seen = useRef<Partial<Record<Market, { running: boolean; scanAt: string | null }>>>({})
  useEffect(() => {   // a scan that ends while the page is open says what it found
    for (const j of jobs?.markets ?? []) {
      const was = seen.current[j.market]
      if (was?.running && !j.running && j.status === "cancelled") toast(`${MARKET_NAME[j.market]}扫描已取消`, { description: "今日页还是上一次扫描的结果" })
      else if (was?.running && !j.running && j.scan_at !== was.scanAt) {
        const found = Object.values(j.scan_summary).filter((x) => !x.error && !x.waiting)
        const compared = found.filter((x) => x.previous_run)   // a first scan has nothing to compare with: no changes
        const n = (k: ChangeKind) => compared.reduce((sum, x) => sum + (x.changes?.[k] ?? 0), 0)
        const firsts = found.length - compared.length
        const counts = CHANGES.map(([k, label]) => `${label} ${n(k)}`).join(" · ") + (firsts ? ` · 首次扫描 ${firsts} 条规则` : "")
        if (j.status === "error" || j.status === "delayed") toast.warning(`${MARKET_NAME[j.market]}扫描完成，有问题`, { description: `${j.message ?? ""} · ${counts}` })
        else toast.success(`${MARKET_NAME[j.market]}扫描完成`, { description: counts })
      }
      seen.current[j.market] = { running: j.running, scanAt: was?.running && j.running ? was.scanAt : j.scan_at }
    }
  }, [jobs])
  const { data: groups = [] } = useWatchlist()
  const toggleWatch = useToggleWatch()
  const watch = useCallback((e: TodayEvent | undefined) => {
    const g = groups[0]
    if (!e?.listing_id || !g) return
    const source = `规则「${e.rule_name}」v${e.version} ${e.day ?? ""} 的${KIND_LABEL[e.kind]}`
    toggleWatch.mutate({ groupId: g.id, listingId: e.listing_id, add: true, source }, { onSuccess: () => toast(`已加入「${g.name}」`) })
  }, [groups, toggleWatch])
  // the row you are on, as the keys see it: set by each key at once — a held J outruns the renders, and a handler reading
  // the rendered row lost moves (30 presses reached row 19); the layout effect below keeps it in step with the page
  const currentRef = useRef<string | null>(null)
  /** Go to a row: the one you leave and the one you reach count as read. */
  const pick = useCallback((key: string | null) => {
    const left = currentRef.current
    currentRef.current = key
    setOpened((was) => {
      const add = [left, key].filter((k): k is string => !!k && !was.has(k))
      if (!add.length) return was
      const next = new Set([...was, ...add])
      saveKeys(OPENED, next)
      return next
    })
    setPicked(key)
  }, [setPicked])
  const setAsideFor = useCallback((e: TodayEvent | undefined) => {
    if (!e) return
    const next = new Set(aside).add(eventKey(e))
    setAside(next)
    saveKeys(ASIDE, next)
  }, [aside])

  useLayoutEffect(() => { // before paint: a key pressed as soon as the list shows reaches the handler that knows it
    const onKey = (ev: KeyboardEvent) => {
      if (leaveToFocus(ev)) return   // a focused control keeps its keys (lib/keys)
      const k = ev.key.toLowerCase()
      const at = shown.findIndex((e) => eventKey(e) === currentRef.current)
      const i = at >= 0 ? at : shown.length ? 0 : -1
      const current = shown[i]
      if ((k === "j" || k === "k") && shown.length) {
        const next = shown[(i + (k === "j" ? 1 : -1) + shown.length) % shown.length]
        pick(eventKey(next))
      } else if (k === "enter" && current?.listing_id) navigate(`/k/${current.listing_id}`)
      else if (k === "w") watch(current)
      else if (k === "e" && current) {   // set aside: the next row is the one you are on, not the first
        const next = shown[i + 1] ?? shown[i - 1]
        pick(next ? eventKey(next) : null)
        setAsideFor(current)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [shown, navigate, watch, setAsideFor, pick])

  const count = (k: EventKind) => events.filter((e) => e.kind === k).length
  const nothing = isFetched && !error && sections.length === 0 && tab === "all"
  const currentKey = current ? eventKey(current) : null
  const feedRef = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {   // in step with what the page shows (a layout effect: no key slips in between), and in view
    currentRef.current = currentKey
    if (currentKey) feedRef.current?.querySelector(`[data-key="${CSS.escape(currentKey)}"]`)?.scrollIntoView({ block: "nearest" })
  }, [currentKey])
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex min-w-0 items-center gap-3">今日
        <span role="tablist" aria-label="市场" className="no-drag flex shrink-0 items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {(["all", ...MARKETS] as const).map((m) => {
            const mine = m === "all" ? all : all.filter((e) => e.market === m)
            return (
              <button key={m} type="button" role="tab" aria-selected={market === m} onClick={() => { setMarket(m); setPicked(null) }}
                title={`${mine.length} 条变化，涉及 ${new Set(mine.map((e) => e.listing_id)).size} 只股票（同一只股票被几条规则选中就算几条）`}
                className={cn("rounded px-2 py-0.5", market === m ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
                {m === "all" ? "全部市场" : MARKET_NAME[m]} <span className="num text-subtle-foreground">{mine.length.toLocaleString()}</span>
              </button>
            )
          })}
        </span></span>}>
        <div className="flex items-center" data-testid="scan-button">
          <Button size="sm" variant="outline" className="rounded-r-none" disabled={!jobs || busy || noData} onClick={() => void scanNow()}
            title={noData ? "还没有数据：先到数据页补历史" : paused.length ? "从暂停的地方接着扫" : scanFor === "all" ? "先更新（收盘后不等就绪窗口），再把每个市场的最新数据重新扫描一遍"
              : `先更新${MARKET_NAME[scanFor]}（收盘后不等就绪窗口），再把它的最新数据重新扫描一遍`}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}{paused.length ? "继续" : ""}{SCAN_LABEL[scanFor]}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="outline" className="-ml-px rounded-l-none px-1.5" disabled={!jobs} aria-label="选择扫描哪个市场">
                <ChevronDown className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {(["all", ...MARKETS] as const).map((m) => {
                const t = scanTargets(m)
                const why = !t.some((j) => j.latest) ? "还没有数据" : t.length && t.every((j) => j.running) ? "正在扫描" : null
                return (
                  <DropdownMenuItem key={m} disabled={!!why} onSelect={() => void scanNow(m)}>
                    <span className="flex-1">{SCAN_LABEL[m]}</span>{why && <span className="text-[0.75rem] text-subtle-foreground">{why}</span>}
                  </DropdownMenuItem>
                )
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </PageHeader>
      {states.filter((j) => j.running).map((j) => <Doing key={j.market} j={j} onPause={() => void pause(j.market)} onCancel={() => void cancel(j.market)} />)}
      {stopped.map((j) => <PausedLine key={j.market} j={j} onResume={() => void resume(j.market)} onCancel={() => void cancel(j.market)} />)}
      <div className="flex min-h-0 flex-1">
        <div className="flex w-[46%] min-w-[23.75rem] flex-col border-r border-border">
          <div role="tablist" aria-label="类别" className="flex shrink-0 flex-wrap items-center gap-0.5 border-b border-border px-3 py-1.5 text-[0.8125rem]">
            {(["all", ...KINDS] as const).map((k) => (
              <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
                className={cn("rounded px-2 py-0.5 whitespace-nowrap", tab === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
                {k === "all" ? "全部" : KIND_LABEL[k]} <span className="num text-subtle-foreground">{(k === "all" ? events.length : count(k)).toLocaleString()}</span>
              </button>
            ))}
          </div>
          <div ref={feedRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="today-feed">
            {error && <div className="px-6 py-16 text-center text-[0.875rem] text-error">读不出今天的变化：{String(error)}</div>}
            {jobs && !(ready.data && ready.rules && ready.scanned) ? <FirstSteps ready={ready} onScan={() => void scanNow("all")} />
              : nothing && <Empty jobs={!!jobs} button={SCAN_LABEL[scanFor]} rules={ready.rules}
                scanned={targets.filter((j) => j.scan_at).map((j) => `${MARKET_NAME[j.market]} ${j.scan_at!.slice(5, 16).replace("T", " ")}`)} />}
            {sections.map((x) => (
              <section key={x.market} data-testid={`today-${x.market}`}>
                <MarketHead market={x.market} j={states.find((j) => j.market === x.market)} events={events.filter((e) => e.market === x.market)} />
                {x.kinds.map(({ kind, key, list, drawn }) => (
                  <div key={kind} className="pb-2">
                    <h3 className="px-4 pt-3 pb-1 text-[0.8125rem] text-muted-foreground">{KIND_LABEL[kind]} <span className="num">{list.length.toLocaleString()}</span></h3>
                    {drawn.map((e) => {
                      const k = eventKey(e)
                      return <Row key={k} e={e} active={currentKey === k} read={opened.has(k)} onPick={pick} ws={dangerQ.data?.warnings[e.listing_id ?? ""]} />
                    })}
                    {drawn.length < list.length && (
                      <button type="button" onClick={() => setMore({ ...more, [key]: drawn.length + PAGE })}
                        className="mx-4 my-1 text-[0.8125rem] text-structure hover:underline">
                        再显示 {Math.min(PAGE, list.length - drawn.length)} 条（还有 {(list.length - drawn.length).toLocaleString()} 条）</button>
                    )}
                  </div>
                ))}
                {x.first.length > 0 && <FirstScans list={x.first} />}   {/* after the changes: those come first */}
              </section>
            ))}
          </div>
          <div className="flex h-9 shrink-0 items-center gap-3 border-t border-border px-4 text-[0.75rem] text-subtle-foreground">
            <span><Kbd>J</Kbd> <Kbd>K</Kbd> 上下</span><span><Kbd>↵</Kbd> 打开 K 线</span><span><Kbd>W</Kbd> 加入观察</span><span><Kbd>E</Kbd> 今天不再显示</span>
          </div>
        </div>
        <div className="min-w-0 flex-1 overflow-y-auto">
          {current ? <EventDetail e={current} onWatch={() => watch(current)} />
            : <div className="flex h-full items-center justify-center px-6 text-center text-[0.8125rem] text-subtle-foreground">
              左边有变化时，选一条在这里看它的图和命中原因</div>}
        </div>
      </div>
    </div>
  )
}

/** A market's part of the list: its name, how many changes on how many stocks, and the scan they come from — the
 *  close it read and when it ran. */
function MarketHead({ market, j, events }: { market: Market; j?: JobState; events: TodayEvent[] }) {
  const day = Object.values(j?.scan_summary ?? {}).find((x) => x.day)?.day
  const stocks = new Set(events.map((e) => e.listing_id).filter(Boolean)).size
  return (
    <h2 className="sticky top-0 z-10 flex items-baseline gap-2 border-b border-border/60 bg-background px-4 pt-3 pb-1.5">
      <span className="shrink-0 text-[0.875rem] font-semibold">{MARKET_NAME[market]}</span>
      <span className="num shrink-0 text-[0.75rem] font-normal text-subtle-foreground">
        {events.length.toLocaleString()} 条变化{stocks ? ` · ${stocks.toLocaleString()} 只股票` : ""}</span>
      {j?.scan_at && <span className="num ml-auto min-w-0 truncate text-[0.75rem] font-normal text-subtle-foreground">
        {day ? `${shortDate(day)} 收盘 · ` : ""}{j.scan_at.slice(5, 16).replace("T", " ")} 扫描</span>}
    </h2>
  )
}

/** The rules whose scan had nothing earlier to compare with: what each found, and where to see it — 选股. */
function FirstScans({ list }: { list: FirstScan[] }) {
  return (
    <div className="border-t border-border/60 px-4 pt-3 pb-3" data-testid="first-scans">
      <h3 className="text-[0.8125rem] text-muted-foreground">首次扫描 <span className="num">{list.length}</span> 条规则</h3>
      <p className="mt-0.5 text-[0.75rem] text-subtle-foreground">
        还没有更早一天的结果可比，这次不列变化、也不推送；下一个交易日起列出新信号、潜伏推进、失效和新潜伏。这次找到的在选股页看。</p>
      <div className="mt-1">
        {list.map((f) => (
          <Link key={f.rule_id} to={`/screen?rule=${encodeURIComponent(f.rule_id)}`}
            className="-mx-2 flex items-center gap-2 rounded px-2 py-1 text-[0.8125rem] hover:bg-accent/60">
            <span className="min-w-0 flex-1 truncate">{f.rule_name}</span>
            <span className="num shrink-0 whitespace-nowrap text-subtle-foreground">
              信号 {(f.counts.signal ?? 0).toLocaleString()} · 潜伏 {(f.counts.watch ?? 0).toLocaleString()}</span>
            <ChevronRight className="size-3.5 shrink-0 text-subtle-foreground" />
          </Link>
        ))}
      </div>
    </div>
  )
}

/** A market's turn while it runs: what it is doing, how far it is, how long it has taken — and a scan's 暂停 and 取消. */
function Doing({ j, onPause, onCancel }: { j: JobState; onPause: () => void; onCancel: () => void }) {
  const p = j.progress
  return (
    <RunStrip testid={`doing-${j.market}`} title={MARKET_NAME[j.market]} text={doing(j)} since={p?.since}
      share={p?.step === "scan" && p.fraction != null ? p.fraction : null}>   {/* only a scan knows how far it is */}
      {p?.step === "scan" && (   // a fetch is not paused or cancelled: it runs to its end
        <RunControls state={p.cancelling ? "cancelling" : p.pausing ? "pausing" : "running"} what={`${MARKET_NAME[j.market]}扫描`} noun="扫描"
          lost={p.n && (p.i ?? 1) > 1 ? `${(p.i ?? 1) - 1} / ${p.n} 条规则` : undefined} after="今日页还是上一次扫描的结果"
          onPause={onPause} onCancel={onCancel} />
      )}
    </RunStrip>
  )
}

/** A scan you paused (or one cut off by quitting the app): how far it got, 继续 — or 取消, to give it up. */
function PausedLine({ j, onResume, onCancel }: { j: JobState; onResume: () => void; onCancel: () => void }) {
  return (
    <RunStrip testid={`paused-${j.market}`} paused title={MARKET_NAME[j.market]} text={j.message ?? "扫描已暂停"}>
      <RunControls state="paused" what={`${MARKET_NAME[j.market]}扫描`} noun="扫描" lost={scanDone(j)} after="今日页还是上一次扫描的结果"
        onResume={onResume} onCancel={onCancel} />
    </RunStrip>
  )
}

/** A first use: what is still missing, in order, each with where to do it — gone once there is data, a rule and a scan. */
function FirstSteps({ ready, onScan }: { ready: { data: boolean; rules: number; scanned: boolean }; onScan: () => void }) {
  const steps: { done: boolean; title: string; text: string; act: ReactNode }[] = [
    { done: ready.data, title: "取数据", text: "在 设置 → 数据 选每个市场的数据档、配好数据源的密钥，再到数据页「补历史…」。",
      act: <><Button size="sm" variant="outline" asChild><Link to="/settings?s=data">去设置</Link></Button>
        <Button size="sm" variant="outline" asChild><Link to="/data">去数据页</Link></Button></> },
    { done: ready.rules > 0, title: ready.rules ? `已启用 ${ready.rules} 条规则` : "启用规则",
      text: "在规则库里打开要每天扫描的规则：内置的箱体模板可以直接用，也可以用一句话让 AI 起草。",
      act: <Button size="sm" variant="outline" asChild><Link to="/rules">去规则库</Link></Button> },
    { done: ready.scanned, title: "扫描", text: "有了数据和规则，每个市场收盘、数据就绪后自动扫描；现在也可以先扫一次。",
      act: <Button size="sm" variant="outline" disabled={!ready.data || !ready.rules} onClick={onScan}>扫描全部市场</Button> },
  ]
  const next = steps.findIndex((x) => !x.done)
  return (
    <div className="px-6 py-8" data-testid="first-steps">
      <h2 className="text-[1rem] font-semibold">开始使用</h2>
      <p className="mt-1 text-[0.8125rem] text-muted-foreground">做完这三步，每天的变化就会列在这里。</p>
      <ol className="mt-4 flex flex-col gap-3">
        {steps.map(({ done, title, text, act }, i) => (
          <li key={i} className={cn("rounded-md border px-4 py-3", i === next ? "border-structure/50 bg-structure/5" : "border-border")}>
            <div className="flex items-center gap-2 text-[0.875rem] font-medium">
              {done ? <CircleCheck className="size-4 shrink-0 text-down" /> : <span className="num grid size-4 shrink-0 place-items-center rounded-full border border-subtle-foreground text-[0.625rem]">{i + 1}</span>}
              <span className={cn(done && "text-muted-foreground")}>{title}</span>
            </div>
            {!done && <>
              <p className="mt-1 pl-6 text-[0.8125rem] text-muted-foreground">{text}</p>
              <div className="mt-2 flex gap-2 pl-6">{act}</div>
            </>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function Empty({ jobs, scanned, button, rules }: { jobs: boolean; scanned: string[]; button: string; rules: number }) {
  return (
    <div className="px-6 py-16 text-center text-[0.875rem] text-muted-foreground" data-testid="today-empty">
      {!jobs ? "连接后端中…" : scanned.length ? `最近一次扫描（${scanned.join("，")}）没有新信号、潜伏推进、失效或新潜伏。`
        : `还没有扫描结果。每个市场的数据就绪后自动扫描已启用的规则，也可以点右上角的「${button}」。`}
      {!rules && <div className="mt-2 text-[0.8125rem] text-subtle-foreground">要扫描的规则在 <Link to="/rules" className="underline">规则库</Link> 里启用。</div>}
    </div>
  )
}

// drawn again only when its own item or its being picked changes (not on every answer about the running scan)
const Row = memo(function Row({ e, active, read, onPick, ws }: { e: TodayEvent; active: boolean; read: boolean; onPick: (key: string) => void; ws?: Danger[] }) {
  const [Icon, tone] = ICON[e.kind]
  return (   // scroll-mt: clear of the market's sticky heading when it is scrolled into view
    <button type="button" onClick={() => onPick(eventKey(e))} data-testid="event" data-row data-key={eventKey(e)} data-read={read || undefined} aria-current={active || undefined}
      className={cn("flex w-full scroll-mt-12 items-start gap-3 px-4 py-2 text-left", active ? "bg-accent" : "hover:bg-accent/60")}>
      <span title={KIND_LABEL[e.kind]} role="img" aria-label={KIND_LABEL[e.kind]} className="mt-0.5 shrink-0"><Icon className={cn("size-4", tone)} strokeWidth={1.75} /></span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className={cn("truncate text-[0.875rem]", read ? "text-muted-foreground" : "font-medium")}>{e.kind === "data" ? `${MARKET_NAME[e.market]}数据` : e.name ?? codeOf(e.listing_id ?? "")}</span>
          {e.listing_id && <span className="num text-[0.75rem] text-subtle-foreground">{codeOf(e.listing_id)} {e.market}</span>}
          <DangerTags ws={ws} compact />
        </span>
        <span className="block truncate text-[0.8125rem] text-muted-foreground">{eventLine(e)}</span>
      </span>
      <span className="num shrink-0 pt-0.5 text-[0.75rem] text-subtle-foreground">{shortDate(e.day)}</span>
    </button>
  )
})

function EventDetail({ e, onWatch }: { e: TodayEvent; onWatch: () => void }) {
  const ref = e.rule_id ? { rule_id: e.rule_id, version: e.version } : null
  const day = e.signal_date ?? e.day
  const detail = useQuery({
    queryKey: ["today-detail", e.rule_id, e.version, e.listing_id, day], enabled: !!(ref && e.listing_id && day), staleTime: Infinity,
    queryFn: () => ruleEndpoints.detail(ref!, e.listing_id!, day!),
  })
  if (e.kind === "live") { // the close's verdict on an intraday alert: what the alert saw, on today's temporary bar
    return (
      <div className="p-5" data-testid="event-detail">
        <div className="flex flex-wrap items-center gap-2">   {/* narrow: the buttons go below, the name keeps its room */}
          <span className="min-w-0 truncate whitespace-nowrap text-[1.0625rem] font-semibold" title={e.name ?? codeOf(e.listing_id ?? "")}>{e.name ?? codeOf(e.listing_id ?? "")}</span>
          <span className="num shrink-0 text-[0.8125rem] text-subtle-foreground">{codeOf(e.listing_id ?? "")}</span>
          <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[0.75rem] font-normal">{e.market}</Badge>
          <span className="ml-auto flex shrink-0 gap-2">
            <Button size="sm" variant="outline" asChild><Link to={`/k/${e.listing_id}`}><BarChart3 className="size-4" />打开 K 线</Link></Button>
            <Button size="sm" variant="outline" onClick={onWatch}><Eye className="size-4" />加入观察</Button>
          </span>
        </div>
        <div className="mt-1 text-[0.8125rem] text-muted-foreground">{eventLine(e)}</div>
        <p className="mt-3 text-[0.8125rem] text-subtle-foreground">
          {e.verdict === "confirmed" ? "收盘后的扫描按正式规则出了这个信号。" : "收盘后的扫描没有出这个信号：盘中满足的条件收盘时没有都满足。"}
          盘中的判定用的是当天的临时 K 线（成交量按全天成交节奏折算），只作预警，不进信号历史。</p>
        {e.detail?.lines?.length ? <>
          <div className="mt-4 text-[0.875rem] font-semibold">盘中的判定</div>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-[0.8125rem] leading-relaxed text-muted-foreground">{e.detail.lines.join("\n")}</pre>
        </> : null}
      </div>
    )
  }
  if (e.kind === "data") {
    return (
      <div className="p-6 text-[0.875rem]" data-testid="event-detail">
        <div className="text-[1rem] font-semibold">{MARKET_NAME[e.market]}{e.status === "interrupted" ? "补历史没取完" : `数据${e.status === "error" ? "出错" : "延迟"}`}</div>
        <p className="mt-2 whitespace-pre-wrap text-muted-foreground">{e.text}</p>
        <p className="mt-2 text-[0.8125rem] text-subtle-foreground">{e.status === "interrupted" ? <>在 <Link to="/data" className="underline">数据</Link> 页点「补历史…」接着取。</>
          : <>明早 8:30 会重新拉取前一个交易日；也可以在 <Link to="/data" className="underline">数据</Link> 页看详情。</>}</p>
      </div>
    )
  }
  // what the match stands on: every structure it bound (a 金品 with both its boxes); until that is read, the scan's own
  const bound = detail.data?.detail.bindings.filter((b) => b.kind === "structure" && b.id).map((b) => b.id!) ?? []
  const standsOn = bound.length ? bound : e.structure ? [e.structure] : []
  return (
    <div className="p-5" data-testid="event-detail">
      <div className="flex flex-wrap items-center gap-2">   {/* narrow: the buttons go below, the name keeps its room */}
        <span className="min-w-0 truncate whitespace-nowrap text-[1.0625rem] font-semibold" title={e.name ?? codeOf(e.listing_id ?? "")}>{e.name ?? codeOf(e.listing_id ?? "")}</span>
        <span className="num shrink-0 text-[0.8125rem] text-subtle-foreground">{codeOf(e.listing_id ?? "")}</span>
        <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[0.75rem] font-normal">{e.market}</Badge>
        <span className="ml-auto flex shrink-0 gap-2">
          <Button size="sm" variant="outline" asChild><Link to={`/k/${e.listing_id}`}><BarChart3 className="size-4" />打开 K 线</Link></Button>
          <Button size="sm" variant="outline" onClick={onWatch}><Eye className="size-4" />加入观察</Button>
        </span>
      </div>
      <div className="mt-1 text-[0.8125rem] text-muted-foreground">{e.rule_name} v{e.version} · {KIND_LABEL[e.kind]} · {eventLine(e).split(" · ").slice(1).join(" · ")}</div>
      {ref && e.listing_id && day && (
        <div className="mt-3 rounded-md border border-border">
          <MiniChart key={`${e.listing_id}-${day}`} ruleRef={ref} listingId={e.listing_id} day={day} ids={standsOn}
            digits={pricePrecision(e.market)} className="h-[18.75rem]" />
        </div>
      )}
      <div className="mt-4 text-[0.875rem] font-semibold">命中原因</div>
      {detail.data ? <DetailCard data={detail.data} className="mt-2" />
        : <div className="mt-2 text-[0.8125rem] text-subtle-foreground">{detail.error ? `读不出来：${String(detail.error)}` : "读取中…"}</div>}
    </div>
  )
}
