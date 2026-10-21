// K 线工作台 (spec §8, design 03). Data, indicators, structures and the rule's judgements all come from
// the backend; this page arranges them: the rule overlay with the clicked bar's explanation (§6.3 单股
// 调试), the replay (§8.3), comparison with another listing or index, sample boxes for the label set
// (§5.10), drawings, and keyboard navigation (§8.4).
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  BellPlus, Bot, ChevronDown, Eye, EyeOff, GitCompare, History, Layers as LayersIcon, LineChart, Minus, MousePointer2, MoveUpRight,
  PanelRightClose, PanelRightOpen, Rows3, Ruler, Search, Square, Trash2, TrendingUp, Type,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { ask } from "@/lib/ask"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ReplayBar } from "@/components/rules/ReplayBar"
import { dayIndex } from "@/components/rules/replay"
import { RulePicker } from "@/components/rules/RulePicker"
import { useLabelNotes } from "@/components/rules/labelNotes"
import { ChartView } from "@/chart/ChartView"
import { type ChartController, DEFAULT_PANES, type LibEntry, type LoadInfo, type Panes } from "@/chart/controller"
import { GROUP_NAME, type IndicatorGroup, ordered, useIndicators } from "@/lib/indicators"
import { isoDate, settled, type WbBar } from "@/chart/data"
import type { Tool } from "@/chart/overlays"
import { asOf, barLength, boxText, dayLines as dayLinesOf, dayTop as dayTopOf, DEFAULT_LAYERS, LAYER_HINTS, LAYER_LABELS, LAYER_SWATCH, type Layers, type Rung } from "@/chart/structures"
import {
  type Basis, endpoints, isMinute, type MinutePeriod, type Period, type Quote, type SearchHit, useDataIn, useInstrument, useRecent,
  useToggleWatch, useWatchlist,
} from "@/lib/api"
import { toggleVariants } from "@/components/ui/toggle"
import { changeClass, codeOf, fmtPct, fmtPrice, indexName, inUnit, marketOf } from "@/lib/format"
import { type PAAnalysis, planLines } from "@/lib/pa"
import {
  BOX_CATEGORIES, boxesOf, type LabelDraft, NO_STRUCTURES, NOT_BOX_CATEGORIES, ruleEndpoints, type RuleRef, useLabels, usePressure,
  useRules, useStructures,
} from "@/lib/rules"
import { lateText, liveAt, liveDay, priceAge, useLive, useNow } from "@/lib/live"
import { useBoardAt } from "@/lib/market"
import { usePersisted } from "@/lib/usePersisted"
import { desktop } from "@/app/platform"
import { cn } from "@/lib/utils"
import { leaveToFocus } from "@/lib/keys"
import { LabelDialog } from "./kline/LabelDialog"
import { draftFromRect } from "./kline/labelDraft"
import { TagEditor } from "@/components/TagEditor"
import { DangerTags } from "@/components/DangerTags"
import { type Danger, useDanger, useDangerEvents } from "@/lib/danger"
import { DangerBlock } from "./kline/DangerBlock"
import { IndustryChip } from "./market/IndustryChip"
import { ResearchPanel } from "./kline/ResearchPanel"
import { Found, HitList } from "./kline/RuleSide"
import { IntradayPane } from "./kline/IntradayPane"
import { NO_VIEW, scannedKey, screenRows, type ScreenView, useJobs, useScreen } from "@/lib/jobs"

const PERIODS: [Period, string][] = [["day", "日"], ["week", "周"], ["month", "月"]]
const MINUTES: [MinutePeriod, string][] = [["m1", "1分"], ["m5", "5分"], ["m15", "15分"], ["m30", "30分"], ["m60", "60分"]]
const BASES: [Basis, string][] = [["qfq", "前复权"], ["hfq", "后复权"], ["raw", "不复权"]]
const PANE_LABELS: [keyof Panes, string, string][] = [
  ["ma", "均线 MA5/10/20/60", "主图"], ["boll", "布林 BOLL", "主图"], ["course", "看盘标记", "主图"], ["chips", "筹码分布", "主图"],
  ["vol", "倍量", "副图"], ["macd", "MACD", "副图"], ["kdj", "KDJ", "副图"], ["index", "大盘", "副图"], ["yearLeft", "年内向左", "副图"],
]
const CATEGORY_NAME: Record<string, string> = { ...BOX_CATEGORIES, ...NOT_BOX_CATEGORIES }
const OPS: Record<string, string> = { ">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠" }
const fmtNum = (v: unknown) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(4)))) : "—")

/** The 看盘 layout (spec §8.6): MAs, 看盘 marks, 倍量, MACD, the board index, 年内向左. */
const COURSE_PANES: Panes = { ma: true, boll: false, course: true, chips: true, vol: true, macd: true, kdj: false, index: true, yearLeft: true }

/** A toolbar toggle: its label shows when the toolbar has room (it measures itself: the research panel narrows it), and
 *  a tooltip names it when only the icon shows. */
function BarToggle({ icon: Icon, label, active, onClick, disabled }: {
  icon: typeof Square; label: string; active: boolean; onClick: () => void; disabled?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant={active ? "secondary" : "ghost"} size="sm" className="no-drag" onClick={onClick} disabled={disabled}
          aria-pressed={active} aria-label={label}>
          <Icon className="size-4" strokeWidth={1.75} /><span className="hidden @min-[88.46rem]:inline">{label}</span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function ToolButton({ icon: Icon, label, active, onClick, disabled }: {
  icon: typeof Square; label: string; active?: boolean; onClick: () => void; disabled?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} aria-pressed={active ?? false} onClick={onClick} disabled={disabled}
          className={cn("text-muted-foreground", active && "bg-accent text-foreground")}>
          <Icon className="size-4" strokeWidth={1.75} />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  )
}

const CHECK_TEXT: Record<string, string> = {
  adj_identity: "复权因子与交易所昨收对不上，复权价可能失真；可切换到「不复权」核对",
  limit_range: "收盘价超出当天涨跌停价",
  price_range: "价格超出当天涨跌停、又和成交额对不上：这一天的价格按未知处理",
  ohlc: "开高低收不自洽",
  missing_day: "有交易日缺数据",
  adj_jump: "复权后单日变动超过 60%",
  vwap_range: "成交额与成交量对不上",
  missing_price: "交易日缺少收盘价",
}

function QualityBadge({ issues }: { issues: { check_name: string; n: number; first: string | null; last: string | null }[] }) {
  const serious = issues.filter((i) => i.check_name !== "missing_day")
  if (serious.length === 0) return null
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link to="/data"><Badge variant="outline" className="no-drag border-error/60 text-error">数据可疑 {serious.reduce((n, i) => n + i.n, 0)} 处</Badge></Link>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm">
        {serious.map((i) => (
          <div key={i.check_name} className="py-0.5">{CHECK_TEXT[i.check_name] ?? i.check_name} · {i.n} 处（{i.first ?? "—"} → {i.last ?? "—"}）</div>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

function QuoteRow({ q, active, onOpen, ws }: { q: Quote; active: boolean; onOpen: () => void; ws?: Danger[] }) {
  const market = marketOf(q.listing_id)
  return (
    <button type="button" onClick={onOpen} data-active={active || undefined} title={`${q.name ?? ""} ${codeOf(q.listing_id)}`.trim()}
      className={cn("flex h-8 w-full items-center gap-2 rounded-md px-2 text-left hover:bg-accent", active && "bg-accent")}>
      {/* the figures keep their width (a column, at least); the name gives way, and in the narrow panel the code gives
          way to the name (it is in the hover): squeezed by long figures, a name read two characters */}
      <span className="min-w-0 flex-1 truncate">{q.name ?? codeOf(q.listing_id)}</span>
      <DangerTags ws={ws} compact />
      <span className="num shrink-0 text-[0.75rem] text-subtle-foreground @max-[22rem]:hidden">{codeOf(q.listing_id)}</span>
      <span className="num min-w-[4.5rem] shrink-0 text-right">{fmtPrice(q.close, market)}</span>
      <span className={cn("num min-w-[3.75rem] shrink-0 text-right", changeClass(q.change))}>{fmtPct(q.change)}</span>
    </button>
  )
}

/** 对比: the board index of the stock, or any listing found by search. */
function CompareMenu({ indexId, active, onPick, disabled }: {
  indexId: string | null; active: { id: string; name: string } | null; onPick: (c: { id: string; name: string; index: boolean } | null) => void
  disabled?: boolean
}) {
  const [q, setQ] = useState("")
  const { data: found = [] } = useQuery({ queryKey: ["search", q], queryFn: () => endpoints.search(q, 6), enabled: q.trim().length > 0 })
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button variant={active ? "secondary" : "ghost"} size="sm" className="no-drag" aria-label={active ? `对比 ${active.name}` : "对比"}
              disabled={disabled}>
              <GitCompare className="size-4" strokeWidth={1.75} /><span className="hidden @min-[88.46rem]:inline">{active ? `对比 ${active.name}` : "对比"}</span>
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{active ? `对比 ${active.name}（点开换一个或取消）` : "对比：叠一条大盘或另一只股票"}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="start" className="w-64">
        {indexId && <DropdownMenuItem onSelect={() => onPick({ id: indexId, name: indexName(indexId), index: true })}>大盘 · {indexName(indexId)}</DropdownMenuItem>}
        <div className="px-2 py-1.5" onKeyDown={(e) => e.stopPropagation()}>
          <Input aria-label="对比的股票" value={q} onChange={(e) => setQ(e.target.value)} placeholder="代码或名称" className="h-7" />
        </div>
        {(found as SearchHit[]).map((h) => (
          <DropdownMenuItem key={h.listing_id} onSelect={() => onPick({ id: h.listing_id, name: h.name ?? h.code, index: false })}>
            <span className="min-w-0 flex-1 truncate">{h.name ?? h.code}</span><span className="num text-[0.75rem] text-subtle-foreground">{h.code}</span>
          </DropdownMenuItem>
        ))}
        {active && <><DropdownMenuSeparator /><DropdownMenuItem onSelect={() => onPick(null)}>取消对比</DropdownMenuItem></>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default function KLinePage() {
  const { listingId } = useParams()
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [last, setLast] = usePersisted<string>("kline.last", "")
  const [period, setPeriod] = usePersisted<Period>("kline.period", "day")
  const [lastMinute, setLastMinute] = usePersisted<MinutePeriod>("kline.minute", "m5")   // the 分钟 menu's last pick (key 5)
  const [intraday, setIntraday] = usePersisted<boolean>("kline.intraday", false)   // 分时 in place of the K-line
  const [basis, setBasis] = usePersisted<Basis>("kline.basis", "qfq")
  const [panes, setPanes] = usePersisted<Panes>("kline.panes", DEFAULT_PANES)
  const [layouts, setLayouts] = usePersisted<{ name: string; panes: Panes; lib?: string[] }[]>("kline.layouts", [])
  const [libOn, setLibOn] = usePersisted<string[]>("kline.lib", [])   // the 指标库 indicators drawn, in the order put on
  const [drawingsVisible, setDrawingsVisible] = usePersisted<boolean>("kline.drawings", true)
  const [tab, setTab] = usePersisted<"watch" | "hits" | "recent">("kline.tab", "watch")
  const [side, setSide] = usePersisted<boolean>("kline.side", true)   // the right panel; ] hides it, the chart takes its room (10-17)
  const [ruleOn, setRuleOn] = usePersisted<boolean>("kline.rule", false)
  const [ruleId, setRuleId] = usePersisted<string | null>("kline.ruleId", null)
  const [savedLayers, setLayers] = usePersisted<Layers>("kline.layers", DEFAULT_LAYERS)
  const layers = useMemo(() => ({ ...DEFAULT_LAYERS, ...savedLayers }), [savedLayers])   // a layer added since: its default
  const [crossMode, setCrossMode] = usePersisted<"zero" | "ma60">("kline.cross", "zero")
  const [ghost, setGhost] = usePersisted<boolean>("kline.live", true) // 盘中当天那根: today's bar while it forms (spec §8.5), drawn as any candle
  const [tool, setTool] = useState<Tool | null>(null)
  const [alertDraw, setAlertDraw] = useState(false) // the 提醒线 tool: a horizontal line with 价格提醒 on
  const [lineMenu, setLineMenu] = useState<{ id: string; alert: boolean; x: number; y: number } | null>(null)
  // a structure picked in 识别到的结构 (its box, 金品 or 假底): drawn in full, the rest faint; the pointer resting on a row
  // shows one for as long as it rests there
  const [focus, setFocus] = useState<string | null>(null)
  const [peek, setPeek] = useState<string | null>(null)
  const [rung, setRung] = useState<Rung | null>(null)   // a 价位梯 row: its line is drawn as itself while the pointer rests there
  const [noteOpen, setNoteOpen] = useState(false)
  const [note, setNote] = useState("")
  const [layoutOpen, setLayoutOpen] = useState(false)
  const [layoutName, setLayoutName] = useState("")
  const [info, setInfo] = useState<LoadInfo | null>(null)
  const [replayOn, setReplayOn] = useState(false)
  const [replay, setReplay] = useState<string | null>(null)       // the day the slider shows
  const [replayDay, setReplayDay] = useState<string | null>(null) // the same, settled (the chart reloads on it)
  const [picked, setPicked] = useState<string | null>(null)       // the clicked bar whose explanation is shown
  const [compare, setCompare] = useState<{ id: string; name: string; index: boolean } | null>(null)
  const [labelDraft, setLabelDraft] = useState<{ draft: LabelDraft; rectId: string; bars: WbBar[] } | null>(null)
  const ctl = useRef<ChartController | null>(null)
  // the bars on the chart as they load and as the day's bar forms: the 价位梯's 现价 and 失效线 read them
  const [bars, setBars] = useState<WbBar[]>([])
  const takeBars = useCallback(() => setBars(ctl.current?.loadedBars().slice() ?? []), [])

  const { data: inst, error: instError } = useInstrument(listingId)
  const dataIn = useDataIn()
  const { data: groups = [] } = useWatchlist()
  const { data: recent = [] } = useRecent()
  const { data: rules = [] } = useRules()
  const { data: library } = useIndicators()
  const libList: LibEntry[] = useMemo(() => libOn.flatMap((id) => {
    const r = library?.indicators.find((x) => x.id === id && !x.archived && !x.error)
    return r ? [{ id: r.id, version: r.version, name: r.name }] : []
  }), [libOn, library])
  const toggleWatch = useToggleWatch()
  const daily = period === "day"
  const minute = isMinute(period) && !intraday   // a minute K-line (10-12): today's bars as they form, whatever 盘中当天那根 says
  const live = ghost || minute
  const market = listingId ? marketOf(listingId) : null
  const { data: liveData } = useLive()
  const liveMinute = live ? liveAt(liveData?.markets, market) : null // a new minute moves the forming bar
  // the structures count today's bar while the chart shows it — through a 行情中断 too (the bar of the last good poll stays)
  const liveBar = live ? liveDay(liveData?.markets, market) : null
  const ruleName = rules.find((r) => r.id === ruleId)?.name ?? null
  /** 规则叠加 on or off; turned on with no rule chosen (or one that is not for this market), it takes your enabled rule
   *  for the market, else one of yours, else any that fits — never an empty picker over an unchanged chart. */
  const setOverlay = (on: boolean) => {
    setRuleOn(on)
    setPicked(null)
    const fits = (r: (typeof rules)[number]) => !r.error && (!market || r.markets.includes(market))
    if (on && !rules.some((r) => r.id === ruleId && fits(r))) {
      const pick = rules.find((r) => r.enabled && fits(r)) ?? rules.find((r) => !r.builtin && fits(r)) ?? rules.find(fits)
      if (pick) setRuleId(pick.id)
    }
  }
  const ref: RuleRef | null = useMemo(() => (ruleOn && ruleId ? { rule_id: ruleId } : null), [ruleOn, ruleId])

  // another stock: the previous one's load info, explanation and replay day go with it
  const [shown, setShown] = useState(listingId)
  if (shown !== listingId) {
    setShown(listingId)
    setInfo(null)
    setPicked(null)
    setFocus(null)
    setPeek(null)
    setRung(null)
    setReplay(null)
    setReplayDay(null)
  }
  // a link can open the chart on a day (?day=, e.g. from the label set) or for a blind redraw of one of your
  // samples (?redraw=: the chart stops at its cutoff and your samples on this stock are hidden while you draw)
  const dayParam = search.get("day")
  const redrawId = search.get("redraw")
  // 标注模式: drawing a sample (a redraw always), the algorithm's boxes, signals and pressure stay out of sight
  const blind = !!redrawId || search.get("blind") === "1"
  const researchOn = search.get("panel") === "research"   // 个股研究 (spec §10.3): the side panel shows 资料 and AI 研究
  const tabParam = search.get("tab")
  const researchTab = tabParam === "ai" || tabParam === "pa" ? tabParam : undefined   // a link to AI 研究 or 价格行为 opens on it
  // 价格行为分析's analysis on screen in the panel: its plan's lines on the chart, on the chart's own price basis
  const [paShown, setPaShown] = useState<PAAnalysis | null>(null)
  const paPlan = paShown?.listing_id === listingId ? paShown : null
  const toggleResearch = () => setSearch((s) => {
    const n = new URLSearchParams(s)
    if (researchOn) { n.delete("panel"); n.delete("tab") }
    else n.set("panel", "research")
    return n
  }, { replace: true })
  useEffect(() => { if (researchOn) setSide(true) }, [researchOn])   // eslint-disable-line react-hooks/exhaustive-deps -- AI 研究 lives in the panel
  const [openedAt, setOpenedAt] = useState<string | null>(null)
  if (dayParam && openedAt !== `${listingId}|${dayParam}`) {
    setOpenedAt(`${listingId}|${dayParam}`)
    setReplayOn(true)
    setReplay(dayParam)
    setReplayDay(dayParam)
  }
  useEffect(() => {
    if (!listingId) return
    setLast(listingId)
    void endpoints.touchRecent(listingId).then(() => qc.invalidateQueries({ queryKey: ["recent"] })).catch(() => undefined)
  }, [listingId, setLast, qc])

  useEffect(() => {
    if (dayParam) setSearch((s) => { const n = new URLSearchParams(s); n.delete("day"); return n }, { replace: true })
  }, [dayParam, setSearch])

  // the slider moves freely; the chart reloads once it settles
  useEffect(() => {
    const id = window.setTimeout(() => setReplayDay(replay), 120)
    return () => window.clearTimeout(id)
  }, [replay])

  const view = useMemo(() => (inst && listingId === inst.listing_id
    ? { listingId: inst.listing_id, market: inst.market, period, basis, name: inst.name ?? inst.code, indexId: inst.index_id, live }
    : null), [inst, listingId, period, basis, live])
  // each new minute, and once more when the day's prices stop (the official bar published: it takes the 虚影's place)
  // and each time the whole-market quote comes in (any A share's forming bar is cut from it: design 2026-10-12 §9)
  const boardAt = useBoardAt(live && market === "CN" ? "CN" : null).data
  useEffect(() => { void ctl.current?.liveTick().then(takeBars) }, [liveMinute, boardAt, takeBars])
  const [barsOf, setBarsOf] = useState(view)   // another stock, period or basis: no bars until its own load
  if (barsOf !== view) {
    setBarsOf(view)
    setBars([])
  }

  // ---- what the backend computed for this listing --------------------------------------------
  const anyStructure = layers.boxLong || layers.boxMid || layers.boxShort || layers.slant || layers.pivots || layers.swings || layers.gold || layers.falseBottoms
  // every period's own structures (design 2026-10-12-structures-every-period); a rule's preview brings its own, a day's
  // 分时: the panel reads the daily 当前形态 (the box top the 分时 draws; 盯盘 alerts on its lines)
  const panelPeriod: Period = intraday ? "day" : period
  const structuresQ = useStructures(listingId, basis, !(ref && daily && !intraday) && anyStructure, replayOn ? null : liveBar, panelPeriod)
  const previewQ = useQuery({
    queryKey: ["preview", ruleId, listingId, basis], queryFn: () => ruleEndpoints.preview(ref!, listingId!, basis),
    enabled: !!ref && !!listingId && daily, retry: false,
  })
  const pressureQ = usePressure(listingId, basis, replayOn ? replayDay : null, (daily || intraday) && layers.pressure)
  const labelsQ = useLabels(listingId, basis, !!listingId)
  const redrawOf = redrawId ? (labelsQ.data ?? []).find((l) => l.id === redrawId) ?? null : null
  if (redrawOf && openedAt !== `${listingId}|redraw|${redrawOf.id}`) {
    setOpenedAt(`${listingId}|redraw|${redrawOf.id}`)
    setReplayOn(true)
    setReplay(redrawOf.cutoff)
    setReplayDay(redrawOf.cutoff)
  }
  const datesQ = useQuery({ queryKey: ["dates", listingId], queryFn: () => ruleEndpoints.dates(listingId!), enabled: replayOn && !!listingId })
  // in the replay a day after the replay day is the future: its explanation is not shown
  const shownPick = picked && !(replayOn && replayDay && picked > replayDay) ? picked : null
  const detailQ = useQuery({
    queryKey: ["detail", ruleId, listingId, shownPick], queryFn: () => ruleEndpoints.detail(ref!, listingId!, shownPick!),
    enabled: !!ref && !!listingId && !!shownPick, retry: false,
  })
  const compareQ = useQuery({
    queryKey: ["compare-series", compare?.id, period, basis],
    queryFn: async () => {
      const r = compare!.index
        ? await endpoints.indexBars({ index_id: compare!.id, period, limit: 5000 })
        : await endpoints.bars({ listing_id: compare!.id, period, basis, limit: 5000 })
      return { name: compare!.name, closes: Object.fromEntries(r.rows.map((b) => [b.t, b.close])) }
    },
    enabled: !!compare && !minute,   // 对比: a day's bars and longer
  })
  useEffect(() => {
    if (previewQ.error) toast.error(`规则预览失败：${String(previewQ.error)}`)
  }, [previewQ.error])
  const signals = useMemo(() => (ref ? previewQ.data?.signals ?? [] : []), [ref, previewQ.data])
  const signalDays = useMemo(() => new Set(signals.map((s) => s.signal_date)), [signals])
  // 危险提醒 (design 2026-10-15 §3): the stock's days on the chart, its warnings on the chart's day in the panel
  const dangerEventsQ = useDangerEvents(daily ? listingId : null)
  const dangerMarks = useMemo(() => (blind ? [] : (dangerEventsQ.data?.events ?? []).map((e) => ({ date: e.date, label: e.label }))), [blind, dangerEventsQ.data])
  const dangerDays = useMemo(() => new Set(dangerMarks.map((d) => d.date)), [dangerMarks])
  const dangerQ = useDanger(inst?.market, listingId ? [listingId] : [], replayOn ? replayDay : null)

  // ---- hand it to the chart --------------------------------------------------------------------
  const structures = (ref && daily && !intraday ? previewQ.data?.structures : structuresQ.data) ?? NO_STRUCTURES
  // 日线价位 on a minute chart: the daily 当前形态's lines (its structures and its settled bars, as the daily 价位梯 reads them)
  const dayLinesOn = (minute && layers.dayLines || intraday) && !blind   // 分时 draws the daily box's top too
  const dayStructQ = useStructures(listingId, basis, dayLinesOn, liveBar, "day")
  const dayBarsQ = useQuery({ queryKey: ["day-bars", listingId, basis, liveBar], enabled: !!listingId && dayLinesOn,
    queryFn: () => endpoints.bars({ listing_id: listingId!, period: "day", basis, limit: 600, live: true }) })
  const close = bars.at(-1)?.close ?? null
  const digits = market === "CN" ? 2 : 1
  const dayLines = useMemo(() => (dayLinesOn && dayStructQ.data && dayBarsQ.data && close != null
    ? dayLinesOf(dayStructQ.data, close, settled(dayBarsQ.data.rows), digits) : []),
  [dayLinesOn, dayStructQ.data, dayBarsQ.data, close, digits])
  // 分时: the daily box's top nearest above the price (else the highest), as 盯盘's 分时 draws a stock's
  const dayTop = useMemo(() => (intraday && dayStructQ.data && dayBarsQ.data ? dayTopOf(dayStructQ.data, settled(dayBarsQ.data.rows), digits) : null),
  [intraday, dayStructQ.data, dayBarsQ.data, digits])
  // your sample boxes; in the replay only those drawn by that day (the chart and the panel both use this list)
  const labels = useMemo(() => {
    const all = layers.labels && !redrawId ? labelsQ.data ?? [] : [] // a blind redraw: no old line in sight
    return replayOn && replayDay ? all.filter((l) => l.cutoff <= replayDay) : all
  }, [layers.labels, labelsQ.data, replayOn, replayDay, redrawId])
  const labelNotes = useLabelNotes(blind ? [] : labels)
  useEffect(() => {
    ctl.current?.setStructures({
      // your sample boxes are a day's (the daily boxes' yardstick): on the daily chart only
      structures: blind || intraday ? NO_STRUCTURES : structures, layers, pressure: blind || !daily ? null : pressureQ.data ?? null,
      labels: daily ? labels : [], labelNotes, dayLines,
      highlight: shownPick ? (detailQ.data?.detail.bindings ?? []).map((b) => b.id).filter((x): x is string => !!x) : [],
      focus: peek ?? focus, rung, dates: replayOn ? datesQ.data : undefined,
    })
  }, [structures, layers, pressureQ.data, labels, labelNotes, detailQ.data, shownPick, blind, focus, peek, rung, daily, dayLines, intraday, replayOn, datesQ.data])
  useEffect(() => {
    ctl.current?.setRule({ signals: blind ? [] : signals, bands: ref && shownPick && !blind ? detailQ.data?.bands ?? [] : [] })
  }, [ref, signals, detailQ.data, shownPick, blind])
  useEffect(() => { ctl.current?.setRule({ danger: dangerMarks }) }, [dangerMarks])
  useEffect(() => {
    const close = paPlan ? bars.find((b) => b.date === paPlan.day)?.close ?? null : null
    ctl.current?.setRule({ plan: paPlan ? planLines(paPlan, close) : [] })
  }, [paPlan, bars])
  useEffect(() => { ctl.current?.setReplay(replayOn ? replayDay : null) }, [replayOn, replayDay])
  useEffect(() => { ctl.current?.setCrossMode(crossMode) }, [crossMode])
  useEffect(() => { ctl.current?.setCompare(compare && compareQ.data && !minute ? compareQ.data : null) }, [compare, compareQ.data, minute])
  useEffect(() => { void ctl.current?.setLibrary(libList) }, [libList, view])   // after the chart opens the view (a child's effect)
  // §8.2 信号标记: resting the pointer on a signal shows when it was known, the rule version and its key numbers
  const [hover, setHover] = useState<{ day: string; left: number } | null>(null) // left: px in the chart box
  const hoverRef = useRef<string | null>(null)
  const chartBox = useRef<HTMLDivElement>(null)
  const hoverDay = hover?.day ?? null
  const hoverQ = useQuery({
    queryKey: ["detail", ruleId, listingId, hoverDay], queryFn: () => ruleEndpoints.detail(ref!, listingId!, hoverDay!),
    enabled: !!ref && !!listingId && !!hoverDay, retry: false, staleTime: 5 * 60_000,
  })
  useEffect(() => {
    const c = ctl.current
    if (!c) return
    c.onCrosshair = (bar) => { // only a change of the signal under the pointer re-renders the page
      const d = bar && daily && !blind && (signalDays.has(bar.date) || dangerDays.has(bar.date)) && !(replayOn && replayDay && bar.date > replayDay) ? bar.date : null
      if (d === hoverRef.current) return
      hoverRef.current = d
      const at = d ? c.pixelOf(d) : null
      const box = chartBox.current?.getBoundingClientRect()
      // beside the bar, on whichever side has room
      setHover(d && at && box ? { day: d, left: at.x - box.left < box.width - 420 ? at.x - box.left + 16 : at.x - box.left - 396 } : null)
    }
    c.onBarClick = (bar) => { if (ref && tool === null) setPicked(isoDate(bar.timestamp)) } // not while drawing
    c.onLineMenu = (m) => setLineMenu(m)
    c.onLibraryError = (name, message) => toast(`${name} 画不出来：${message}`)
    c.onDrawingDone = () => setTool(null)
    c.onUserRemoved = (gone) => toast(gone.length > 1 ? `已清除 ${gone.length} 条标注` : "已删除这条标注", {   // a moment to take it back
      action: { label: "撤销", onClick: () => void c.restoreDrawings(gone) }, duration: 8000 })
    c.onRectDrawn = (rectId, corners) => { // a sample box is daily (spec §5.10): elsewhere a rectangle stays a rectangle
      if (!listingId || corners.length < 2 || !daily) return
      const bars = settled(c.loadedBars())   // today's bar still forming is no bar to stand a sample on
      const cutoff = replayOn && replayDay ? replayDay : (bars.at(-1)?.date ?? corners[1].date)
      const draft = draftFromRect(listingId, corners, cutoff, bars)   // its dates on bars up to the cutoff
      if (!draft) { toast(`这个框里没有 ${cutoff} 及之前的 K 线，不能当样例框：它留作普通矩形`); return }
      setLabelDraft({ rectId, bars, draft: redrawOf // a redraw keeps the sample's kind and points at it
        ? { ...draft, redraw_of: redrawOf.id, is_box: redrawOf.is_box, category: redrawOf.category, scale: redrawOf.scale ?? null } : draft })
    }
  })

  const hoverSignal = hoverDay ? signals.find((s) => s.signal_date === hoverDay) : undefined
  const hoverDanger = hoverDay ? dangerMarks.find((d) => d.date === hoverDay) : undefined
  const hoverNames = hoverQ.data?.detail.names
  const hoverCard = hoverSignal && previewQ.data ? (
    <div className="pointer-events-none absolute top-[120px] z-10 w-[23.75rem] rounded-md border border-border bg-popover/95 px-3 py-2 text-[0.8125rem] shadow-md"
      style={{ left: Math.max(4, hover?.left ?? 0) }} data-testid="signal-hover">
      <div className="font-medium">信号 {hoverSignal.signal_date} · 收盘确认</div>
      <div className="text-muted-foreground">{previewQ.data.rule.name} · v{previewQ.data.rule.version}</div>
      {Object.entries(hoverSignal).filter(([k, v]) => k !== "listing_id" && k !== "signal_date" && typeof v === "string").map(([k, id]) => {
        const small = boxesOf(String(id)).at(-1)   // a 金品 reads as its small box
        const b = (replayOn && replayDay ? asOf(structures, replayDay) : structures).boxes.find((x) => x.id === small) // as known on the replay day
        return <div key={k}>{hoverNames?.stages[k] ?? k}：{b ? boxText(b) : String(id)}</div>
      })}
      {(hoverQ.data?.detail.conditions ?? []).filter((c) => c.status === "pass" && typeof c.value === "number" && c.op && typeof c.threshold === "number").slice(0, 4).map((c) => {
        const key = `${c.stage}.${c.id}`, unit = hoverNames?.units[key] ?? ""   // by name, in its unit
        const show = (v: number) => (unit ? inUnit(v, unit) : fmtNum(v))
        return <div key={key} className="num">{hoverNames?.conds[key] ?? key}：{show(c.value as number)} {OPS[c.op ?? ""] ?? ""} {show(c.threshold as number)}</div>
      })}
    </div>
  ) : null

  // ---- lists J/K walks through -----------------------------------------------------------------
  const watchRows = useMemo(() => {
    const seen = new Set<string>()
    return groups.flatMap((g) => g.items).filter((i) => !seen.has(i.listing_id) && seen.add(i.listing_id))
  }, [groups])
  // 命中: the list the 选股 page shows (its rule, 同时满足, 附加条件 and filters), kept by it as it changes
  const [screenView] = usePersisted<ScreenView>("screen.view", NO_VIEW)
  const { data: jobs } = useJobs()
  const screen = useScreen(tab === "hits" ? screenView.rule ?? undefined : undefined, scannedKey(jobs), screenView.also, screenView.where, screenView.industry ?? null)
  const hitRows = useMemo(() => screenRows(screen.data, screenView), [screen.data, screenView])
  const rows: Quote[] = tab === "watch" ? watchRows : tab === "hits" ? hitRows.map((r) => ({ listing_id: r.listing_id, name: r.name })) : recent
  const listDanger = useDanger("CN", rows.map((q) => q.listing_id).filter((id) => id.startsWith("CN:")))   // 危险提醒's tags
  // 观察 and 最近 in the session: their prices' time, amber when not updated for 3 minutes (the 10-12 rule)
  const now = useNow(), cnLive = liveData?.markets.find((m) => m.market === "CN")
  const freshest = rows.map((q) => q.at).filter((x): x is string => !!x).sort().at(-1)
  const listAge = tab !== "hits" && cnLive && freshest ? priceAge(freshest, "CN", cnLive.session, now) : null
  // the stock shown kept in view in the list: J / K walk a list of hundreds, its row was left behind out of sight
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    listRef.current?.querySelector("[data-active]")?.scrollIntoView({ block: "nearest" })
  }, [listingId, tab, rows.length])
  const inWatch = !!inst && inst.watch_groups.length > 0
  const defaultGroup = groups[0]?.id

  const openListing = useCallback((id: string) => navigate(`/k/${id}`), [navigate])

  const toggleCurrentWatch = useCallback(() => {
    if (!inst || defaultGroup === undefined) return
    const add = !inWatch
    const groupId = add ? defaultGroup : inst.watch_groups[0]
    toggleWatch.mutate({ groupId, listingId: inst.listing_id, add, source: "K 线页" }, {
      onSuccess: () => toast(add ? `已加入「${groups.find((g) => g.id === groupId)?.name}」` : "已移出观察"),
    })
  }, [inst, inWatch, defaultGroup, groups, toggleWatch])

  const toggleReplay = useCallback(() => {
    if (replayOn) { setReplay(null); setReplayDay(null) }
    setReplayOn(!replayOn)
  }, [replayOn])
  /** A period picked: the K-line in place of 分时; a minute one is remembered for key 5 and has no replay. */
  const pickPeriod = useCallback((p: Period) => {
    setIntraday(false)
    setPeriod(p)
    if (isMinute(p)) {
      setLastMinute(p)
      if (replayOn) toggleReplay()
    }
  }, [setIntraday, setPeriod, setLastMinute, replayOn, toggleReplay])

  // keyboard: J/K next/previous, W watch, 1/2/3 day/week/month, 4 分时, 5 分钟, space replay, ←/→ step it, ] the right panel, Esc (spec §8.4)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (leaveToFocus(e)) return   // a focused control keeps its keys (lib/keys)
      const k = e.key.toLowerCase()
      if ((k === "j" || k === "k") && rows.length) {
        const i = rows.findIndex((r) => r.listing_id === listingId)
        const next = rows[(i + (k === "j" ? 1 : -1) + rows.length) % rows.length]
        if (next) openListing(next.listing_id)
      } else if (k === "w") toggleCurrentWatch()
      else if (k === "1" || k === "2" || k === "3") pickPeriod(PERIODS[Number(k) - 1][0])
      else if (k === "4") setIntraday(true)
      else if (k === "5") pickPeriod(lastMinute)
      else if (k === "]") setSide(!side)
      else if (k === " ") { e.preventDefault(); if (!minute) toggleReplay() }
      else if ((k === "arrowleft" || k === "arrowright") && replayOn && datesQ.data?.length) {
        e.preventDefault()
        const d = datesQ.data
        setReplay((day) => {   // from the day the last key left, not the last render's: keys faster than the chart each count
          const j = Math.max(0, Math.min(d.length - 1, dayIndex(d, day) + (k === "arrowright" ? 1 : -1)))
          return j >= d.length - 1 ? null : d[j]
        })
      } else if (k === "escape") {   // drops a drawing begun; a structure picked: back to 当前形态
        ctl.current?.cancelDrawing(); setTool(null); setPicked(null); setLineMenu(null)
        if (focus) { setFocus(null); ctl.current?.toNow() }
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [rows, listingId, openListing, toggleCurrentWatch, pickPeriod, lastMinute, minute, setIntraday, toggleReplay, replayOn, datesQ.data, focus, side, setSide])

  if (!listingId) {
    const to = last || watchRows[0]?.listing_id
    if (to) {
      navigate(`/k/${to}`, { replace: true })
      return null
    }
    return (
      <div className="flex h-full items-center justify-center text-[0.875rem] text-subtle-foreground">
        按 {desktop.mod}K 搜一只股票，K 线就从它开始
      </div>
    )
  }

  /** 光标: the drawing started is dropped, so the next click does not place it */
  const leaveTool = () => { ctl.current?.cancelDrawing(); setTool(null) }
  // the text tool asks for its words first (window.prompt does not exist in Electron)
  const startTool = (t: Tool, text?: string, alert = false) => {
    if (t === "text" && text === undefined) {
      setNote("")
      setNoteOpen(true)
      return
    }
    setTool(t)
    setAlertDraw(alert)
    ctl.current?.startDrawing(t, text, alert)
  }

  // 分时's panel reads the daily 当前形态 (panelPeriod): on the daily bars, not those of the period the chart was left on
  const dayRows = intraday && !daily ? dayBarsQ.data?.rows ?? [] : null
  const settledBars = dayRows ? settled(dayRows) : settled(bars)
  const panelPrice = dayRows ? dayRows.at(-1)?.close ?? null : bars.at(-1)?.close ?? null
  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        {/* the panel hidden, this row reaches the window's top right: on Windows its buttons are there (index.css) */}
        <header data-testid="kline-toolbar" className={cn("drag @container flex min-h-11 min-w-0 shrink-0 flex-wrap items-center gap-x-2.5 gap-y-1.5 border-b border-border px-3 py-1.5",
          !side && "clear-window-buttons")}>
          {/* wraps: the industry chip can take a line of its own in a narrow chart column rather than run into the list */}
          <div className="flex min-w-0 flex-wrap items-center gap-1">
          <button type="button" onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
            title={`${inst?.name ? `${inst.name} ${codeOf(listingId)} · ` : ""}搜索或跳转（${desktop.mod}K）`}
            className="no-drag flex h-8 w-[13.75rem] shrink-0 items-center gap-2 rounded-md border border-input px-2.5 hover:bg-accent @max-[67.69rem]:w-[9.375rem] @max-[53.85rem]:w-[7rem]">
            <Search className="size-4 shrink-0 text-subtle-foreground" strokeWidth={1.75} />
            {/* the narrower box keeps the name whole (the code is in the hover): name and code both in it cut a four-character
                name to two at 1440 px */}
            <span className="truncate font-medium">{inst?.name ?? (instError ? "未找到" : "…")}</span>
            <span className="num text-[0.8125rem] text-subtle-foreground @max-[67.69rem]:hidden">{codeOf(listingId)}</span>
            <Badge variant="outline" className="ml-auto h-5 px-1.5 text-[0.75rem] font-normal text-muted-foreground @max-[67.69rem]:hidden">{market}</Badge>
          </button>
          {/* its industry and that industry's standing on the 市场 page (design 2026-10-12-market-page-redesign §6.2) */}
          {inst && market && <IndustryChip market={market} l1={inst.ind_l1} l2={inst.ind_l2} />}
          <ToggleGroup type="single" value={intraday ? "intraday" : minute ? "" : period} className="no-drag" size="sm" variant="outline"
            onValueChange={(v) => { if (v === "intraday") setIntraday(true); else if (v) pickPeriod(v as Period) }}>
            {PERIODS.map(([p, l], i) => <ToggleGroupItem key={p} value={p} className="px-3 @max-[63.08rem]:px-2" title={`${l}线（${i + 1}）`}>{l}</ToggleGroupItem>)}
            <ToggleGroupItem value="intraday" className="px-3 @max-[63.08rem]:px-2" title="分时：最近交易日的 1 分钟线（4）">分时</ToggleGroupItem>
          </ToggleGroup>
          {/* 分钟 K 线 (10-12): one menu beside the periods, showing the one on the chart */}
          <DropdownMenu>
            <DropdownMenuTrigger title="分钟 K 线：1、5、15、30、60 分钟（5）" data-testid="kline-minute"
              className={cn(toggleVariants({ variant: "outline", size: "sm" }), "no-drag px-3 @max-[63.08rem]:px-2",
                minute && "bg-accent text-foreground shadow-[inset_0_0_0_1px_var(--foreground)]")}>
              {minute ? MINUTES.find(([p]) => p === period)?.[1] : "分钟"}<ChevronDown className="size-3.5 text-subtle-foreground" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-60">
              <DropdownMenuRadioGroup value={minute ? period : ""} onValueChange={(v) => pickPeriod(v as MinutePeriod)}>
                {MINUTES.map(([p, l]) => <DropdownMenuRadioItem key={p} value={p}>{l}钟</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
              <div className="px-2 py-1.5 text-[0.75rem] text-subtle-foreground">
                最近几天的分钟 K 线，开盘期间最后一根跟着行情变。结构按分钟线自己的算；画线、规则信号、对比、回放和指标库都在日线上</div>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="no-drag" aria-label="指标" title="指标、结构图层与布局"><LineChart className="size-4" strokeWidth={1.75} />
                <span className="@max-[63.08rem]:hidden">指标</span><ChevronDown className="size-3.5 text-subtle-foreground" /></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-72">
              <DropdownMenuLabel className="text-[0.75rem] text-subtle-foreground">布局</DropdownMenuLabel>
              <DropdownMenuItem onSelect={() => setPanes(COURSE_PANES)}>看盘布局（预设）</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setPanes(DEFAULT_PANES)}>默认布局</DropdownMenuItem>
              {layouts.map((l) => (
                <DropdownMenuItem key={l.name} onSelect={() => { setPanes({ ...DEFAULT_PANES, ...l.panes }); if (l.lib) setLibOn(l.lib) }}>{l.name}</DropdownMenuItem>
              ))}
              <DropdownMenuItem onSelect={() => { setLayoutName(""); setLayoutOpen(true) }}>存为我的布局…</DropdownMenuItem>
              <DropdownMenuSeparator />
              {(["主图", "副图"] as const).map((group) => (
                <div key={group}>
                  <DropdownMenuLabel className="text-[0.75rem] text-subtle-foreground">{group}</DropdownMenuLabel>
                  {PANE_LABELS.filter(([, , g]) => g === group).map(([key, label]) => (
                    <DropdownMenuCheckboxItem key={key} checked={panes[key]} onSelect={(e) => e.preventDefault()}
                      onCheckedChange={(c) => setPanes({ ...panes, [key]: !!c })}>{label}</DropdownMenuCheckboxItem>
                  ))}
                </div>
              ))}
              <DropdownMenuSeparator />
              <DropdownMenuCheckboxItem checked={crossMode === "ma60"} onSelect={(e) => e.preventDefault()}
                onCheckedChange={(c) => setCrossMode(c ? "ma60" : "zero")}>MACD 金叉按 60 日线上下区分</DropdownMenuCheckboxItem>
              <DropdownMenuSeparator />
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>指标库<span className="num ml-auto pl-3 text-[0.75rem] text-subtle-foreground">{libList.length || ""}</span></DropdownMenuSubTrigger>
                <DropdownMenuSubContent className="max-h-[70vh] w-60 overflow-y-auto">
                  {(["mine", "common", "setup", "ict"] as IndicatorGroup[]).map((g) => {
                    const rows = ordered((library?.indicators ?? []).filter((r) => !r.archived && !r.error), g)
                    return rows.length ? (
                      <div key={g} role="group" aria-label={GROUP_NAME[g]}>
                        <DropdownMenuLabel className="text-[0.75rem] text-subtle-foreground">{GROUP_NAME[g]}</DropdownMenuLabel>
                        {rows.map((r) => (
                          <DropdownMenuCheckboxItem key={r.id} checked={libOn.includes(r.id)} title={r.doc} onSelect={(e) => e.preventDefault()}
                            disabled={!!market && !r.markets.includes(market)}
                            onCheckedChange={(c) => setLibOn(c ? [...libOn.filter((x) => x !== r.id), r.id] : libOn.filter((x) => x !== r.id))}>
                            {r.name}</DropdownMenuCheckboxItem>
                        ))}
                      </div>
                    ) : null
                  })}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={() => navigate("/indicators")}>管理指标库…</DropdownMenuItem>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
              <DropdownMenuSeparator />
              <DropdownMenuLabel className="flex text-[0.75rem] text-subtle-foreground">结构图层
                <span className="num ml-auto">{LAYER_LABELS.filter(([k]) => layers[k]).length} / {LAYER_LABELS.length}</span></DropdownMenuLabel>
              <div role="group" aria-label="结构图层" className="grid grid-cols-2">
                {LAYER_LABELS.map(([k, l]) => (
                  <DropdownMenuCheckboxItem key={k} checked={layers[k]} title={LAYER_HINTS[k]} onSelect={(e) => e.preventDefault()}
                    onCheckedChange={(c) => setLayers({ ...layers, [k]: !!c })}>
                    {LAYER_SWATCH[k] && <span aria-hidden className="inline-block w-3 shrink-0"
                      style={{ borderTop: `2px ${LAYER_SWATCH[k]!.dash ? "dashed" : "solid"} var(${LAYER_SWATCH[k]!.color})` }} />}
                    {l}
                  </DropdownMenuCheckboxItem>
                ))}
              </div>
              <DropdownMenuSeparator />
              <DropdownMenuCheckboxItem checked={ghost} onSelect={(e) => e.preventDefault()} onCheckedChange={(c) => setGhost(!!c)}
                title="开盘期间，最后一根是今天正在走的 K 线，和别的 K 线一样画，跟着行情实时变（K 线页开着时约 10 秒一次）；晚上日线更新后换成正式的那根。开盘期间打开过的 A 股都有（取自全市场行情）；日股只有盯盘的股票（自选）有">
                盘中当天那根</DropdownMenuCheckboxItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="no-drag">{BASES.find(([b]) => b === basis)?.[1]}<ChevronDown className="size-3.5 text-subtle-foreground @max-[53.85rem]:hidden" /></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuRadioGroup value={basis} onValueChange={(v) => setBasis(v as Basis)}>
                {BASES.map(([b, l]) => <DropdownMenuRadioItem key={b} value={b}>{l}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
              <div className="px-2 py-1.5 text-[0.75rem] text-subtle-foreground">计算一律用后复权；显示口径只差一个常数倍</div>
            </DropdownMenuContent>
          </DropdownMenu>
          </div>
          <div className="flex items-center gap-1">
          <BarToggle icon={LayersIcon} label="规则叠加" active={ruleOn} onClick={() => setOverlay(!ruleOn)} />
          {ruleOn && <RulePicker value={ruleId} market={market} className="@max-[69.23rem]:max-w-[8.75rem] @max-[53.85rem]:max-w-[6.5rem]" onChange={(id) => { setRuleId(id); setPicked(null) }} />}
          <CompareMenu indexId={inst?.index_id ?? null} active={compare} onPick={setCompare} disabled={minute} />
          <BarToggle icon={History} label="回放" active={replayOn} onClick={toggleReplay} disabled={minute} />
          {info && !info.adjusted && (
            <Tooltip>
              <TooltipTrigger asChild><Badge variant="outline" className="border-warning/50 text-warning">未复权</Badge></TooltipTrigger>
              <TooltipContent className="max-w-xs">这只股票在当前数据档里没有复权因子，图上是交易所原始价；除权前后的价格不能直接比较</TooltipContent>
            </Tooltip>
          )}
          <QualityBadge issues={inst?.quality ?? []} />
          </div>
          <div className="ml-auto flex items-center gap-1.5">
          <BarToggle icon={Bot} label="AI 研究" active={researchOn} onClick={toggleResearch} disabled={!listingId} />
          <Button variant={inWatch ? "secondary" : "outline"} size="sm" className="no-drag shrink-0" onClick={toggleCurrentWatch} disabled={!inst}
            title={inWatch ? "已在观察（W 移出）" : "加入观察（W）"} aria-label={inWatch ? "已在观察" : "加入观察"}>
            <Eye className="size-4" strokeWidth={1.75} /><span className="@max-[63.08rem]:hidden">{inWatch ? "已在观察" : "加入观察"}</span>
          </Button>
          <Button variant="ghost" size="sm" className="no-drag shrink-0" onClick={() => setSide(!side)}
            title={`${side ? "隐藏" : "显示"}右栏（]）`} aria-label={side ? "隐藏右栏" : "显示右栏"}>
            {side ? <PanelRightClose className="size-4" strokeWidth={1.75} /> : <PanelRightOpen className="size-4" strokeWidth={1.75} />}
          </Button>
          </div>
        </header>
        <div className="flex min-h-0 flex-1">
          <div className="flex w-10 shrink-0 flex-col items-center gap-1 border-r border-border py-2">
            <ToolButton icon={MousePointer2} label="光标 (Esc)" active={tool === null} onClick={leaveTool} />
            <ToolButton disabled={minute} icon={TrendingUp} label="趋势线" active={tool === "segment"} onClick={() => startTool("segment")} />
            <ToolButton disabled={minute} icon={MoveUpRight} label="射线" active={tool === "ray"} onClick={() => startTool("ray")} />
            <ToolButton disabled={minute} icon={Minus} label="水平线" active={tool === "hline" && !alertDraw} onClick={() => startTool("hline")} />
            <ToolButton disabled={minute} icon={BellPlus} label="提醒线（盘中价格穿过时提醒；右键任何线条可开关提醒）" active={tool === "hline" && alertDraw}
              onClick={() => startTool("hline", undefined, true)} />
            <ToolButton disabled={minute} icon={Square} label="矩形 / 样例框" active={tool === "rect"} onClick={() => startTool("rect")} />
            <ToolButton disabled={minute} icon={Rows3} label="斐波那契回撤" active={tool === "fib"} onClick={() => startTool("fib")} />
            <ToolButton disabled={minute} icon={Type} label="文字标注" active={tool === "text"} onClick={() => startTool("text")} />
            <ToolButton disabled={minute} icon={Ruler} label="测量（涨跌幅与根数）" active={tool === "measure"} onClick={() => startTool("measure")} />
            <div className="my-1 h-px w-6 bg-border" />
            <ToolButton disabled={minute} icon={drawingsVisible ? Eye : EyeOff} label={drawingsVisible ? "隐藏我的标注" : "显示我的标注"} onClick={() => setDrawingsVisible(!drawingsVisible)} />
            <ToolButton disabled={minute} icon={Trash2} label="清除这只股票的全部标注" onClick={() => {
              void ask({ title: "清除这只股票的全部标注？", text: "画的线、框和文字都删掉，线上的价格提醒也一起没有。删完几秒内可以撤销。",
                action: "全部清除" }).then((yes) => { if (yes) ctl.current?.clearDrawings() })
            }} />
          </div>
          <div className="flex min-w-0 flex-1 flex-col">
            {blind && (
              <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border bg-structure/10 px-3 text-[0.8125rem]" data-testid="blind-banner">
                <span className="flex-1">{redrawId
                  ? `盲画：重画样例${redrawOf ? `（${CATEGORY_NAME[redrawOf.category] ?? redrawOf.category}，截止 ${redrawOf.cutoff}）` : ""}。旧线和算法画的箱体都藏起来了；用矩形把箱画出来，存为样例就是你自己的重画基线。`
                  : "标注模式：算法画的箱体、信号和压力带先藏起来，只看 K 线自己画箱；用矩形画好后存为样例。"}</span>
                <Button variant="ghost" size="sm" className="h-6 px-2 text-[0.8125rem]"
                  onClick={() => setSearch((s) => { const n = new URLSearchParams(s); n.delete("redraw"); n.delete("blind"); return n }, { replace: true })}>
                  {redrawId ? "不画了" : "退出标注模式"}</Button>
              </div>
            )}
            <div ref={chartBox} className="relative min-h-0 flex-1">
              <ChartView view={view} panes={panes} drawingsVisible={drawingsVisible}
                onReady={(c) => { ctl.current = c }}
                onLoad={(i) => { setInfo(i); leaveTool(); takeBars() }}
                onError={(e) => toast.error(`图表数据加载失败：${String(e)}`)} />
              {hoverCard}
              {hoverDanger && <div data-testid="danger-hover" className="pointer-events-none absolute top-[88px] z-10 rounded-md border border-border bg-popover/95 px-2 py-1 text-[0.8125rem] text-warning shadow-md"
                style={{ left: Math.max(4, hover?.left ?? 0) }}>危险提醒 · {hoverDanger.date} {hoverDanger.label}</div>}
              {instError && listingId && (   // not the last stock's chart under this one's name: why there is none
                <div className="absolute inset-0 z-10 grid place-items-center bg-background px-8 text-center text-[0.875rem] text-muted-foreground" data-testid="kline-missing">
                  {!dataIn.has(marketOf(listingId)) ? dataIn.none(marketOf(listingId))
                    : /not found/.test(String(instError)) ? `找不到 ${listingId}：现在的数据档里没有这只股票` : `读不出这只股票：${String(instError)}`}
                </div>
              )}
              {minute && info?.bars === 0 && !instError && (   // a minute chart with no minutes: why, not an empty canvas
                <div className="absolute inset-x-0 top-1/3 z-10 mx-auto max-w-md px-8 text-center text-[0.875rem] text-muted-foreground"
                  data-testid="kline-minute-empty">
                  {market === "CN"
                    ? "还没有这只股票的分钟线。正式档配了 TickFlow 时，打开就取最近 20 个交易日；没配时只有盯盘的股票和开盘期间打开过的股票记下的那几天。"
                    : "还没有这只股票的分钟线：日股只有盯盘的股票（观察分组里的、画了提醒线的）和开盘期间打开过的股票，记下的那几天才有。"}
                </div>
              )}
              {intraday && inst && <IntradayPane listingId={listingId} market={inst.market} watched={inWatch} onWatch={toggleCurrentWatch} top={dayTop} />}
            </div>
            {replayOn && (
              <ReplayBar dates={datesQ.data ?? []} value={replay} onChange={setReplay} onClose={toggleReplay} />
            )}
          </div>
        </div>
      </div>
      {side && <aside data-testid="kline-side" className={cn("@container flex shrink-0 flex-col border-l border-border", researchOn && listingId ? "w-[26.25rem]" : "w-[18.75rem]")}>
        {/* Windows: this panel reaches the window's top right corner, where its buttons are: their row, the panel below */}
        {desktop.isWindows && <div className="drag h-11 shrink-0 border-b border-border" />}
        {researchOn && listingId ? <ResearchPanel listingId={listingId} inWatch={inWatch} initial={researchTab} onPlanFor={setPaShown} /> : <>
        <div className="flex h-11 items-center px-3">
          <Tabs value={tab} onValueChange={(v) => setTab(v as "watch" | "hits" | "recent")} className="w-full">
            <TabsList className="w-full">
              <TabsTrigger value="watch">观察 <span className="num ml-1 text-subtle-foreground">{watchRows.length}</span></TabsTrigger>
              <TabsTrigger value="hits" title="选股页列出的股票（规则、同时满足、附加条件都算上）">命中 <span className="num ml-1 text-subtle-foreground">{hitRows.length || ""}</span></TabsTrigger>
              <TabsTrigger value="recent">最近</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-2" data-testid="kline-list">
          {tab === "hits" ? (
            <HitList view={screenView} screen={screen} rows={hitRows} listingId={listingId} onOpen={openListing} danger={listDanger.data?.warnings} />
          ) : (
            <>
              {listAge && <div data-testid="kline-list-age" className={cn("px-2 pt-1 pb-0.5 text-[0.6875rem]", listAge.late ? "text-warning" : "text-subtle-foreground")}>
                {listAge.late ? lateText(listAge) : `价格 ${freshest!.slice(11, 19)}`}</div>}
              {rows.length === 0 && <div className="px-2 py-6 text-center text-[0.8125rem] text-subtle-foreground">
                {tab === "watch" ? "还没有观察的股票。按 W 加入当前这只。" : "还没有浏览记录"}</div>}
              {rows.map((q) => <QuoteRow key={q.listing_id} q={q} active={q.listing_id === listingId} onOpen={() => openListing(q.listing_id)}
                ws={listDanger.data?.warnings[q.listing_id]} />)}
            </>
          )}
        </div>
        {listingId && !blind && <DangerBlock listingId={listingId} answer={dangerQ.data} />}
        <div className="max-h-[48%] overflow-y-auto border-t border-border px-3 py-2">
          <div className="flex items-center pb-1 text-[0.75rem] text-subtle-foreground">
            <span className="flex-1">识别到的结构{ruleOn && ruleName ? ` · ${ruleName}` : ""}</span>
            {shownPick && <button type="button" className="hover:text-foreground" onClick={() => setPicked(null)}>收起判定（Esc）</button>}
          </div>
          {blind ? <div className="pb-2 text-[0.8125rem] text-subtle-foreground">标注模式里不显示算法识别的结构和规则判定，画完退出标注模式再看。</div> : (
            <Found key={listingId} structures={structures} day={replayOn ? replayDay : null} pressure={daily || intraday ? pressureQ.data ?? null : null} labels={labels}
              barLen={(from, to, beforeTo) => barLength(dayRows ? settledBars as { date: string; high: number; low: number; close: number }[] : settled(ctl.current?.loadedBars() ?? []), from, to, beforeTo)}
              signals={signals} detail={ref && shownPick ? detailQ.data ?? null : null}
              ruleName={ref ? ruleName : null} digits={digits} period={panelPeriod} price={panelPrice} bars={settledBars} dates={replayOn ? datesQ.data : undefined}
              focus={focus} onFocus={(id) => { setFocus(id); if (id === null) ctl.current?.toNow() }} onPeek={(id, r) => { setPeek(id); setRung(r ?? null) }}
              onReveal={(from, to) => { if (listingId) void ctl.current?.reveal(listingId, from, to) }} />
          )}
          {listingId && <TagEditor listingId={listingId} />}
        </div>
        </>}
      </aside>}
      {lineMenu && ( // a line's right-click (spec §7.2: 在画线上勾选提醒)
        // the right-click's own contextmenu event lands on this backdrop just after it opens: it must not close the menu
        <div className="fixed inset-0 z-50" onClick={() => setLineMenu(null)} onContextMenu={(e) => e.preventDefault()}>
          <div role="menu" aria-label="画线" className="absolute min-w-40 rounded-md border border-border bg-popover p-1 text-[0.875rem] shadow-md"
            style={{ left: lineMenu.x, top: lineMenu.y }} onClick={(e) => e.stopPropagation()}>
            <button type="button" role="menuitemcheckbox" aria-checked={lineMenu.alert}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent"
              onClick={() => { ctl.current?.setLineAlert(lineMenu.id, !lineMenu.alert); setLineMenu(null) }}>
              <BellPlus className="size-4" />{lineMenu.alert ? "关闭价格提醒" : "开启价格提醒"}</button>
            <button type="button" role="menuitem" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-error hover:bg-accent"
              onClick={() => { ctl.current?.removeDrawing(lineMenu.id); setLineMenu(null) }}>
              <Trash2 className="size-4" />删除这条线</button>
          </div>
        </div>
      )}
      <Dialog open={noteOpen} onOpenChange={setNoteOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>文字标注</DialogTitle>
            <DialogDescription>写好后在图上点一下放置。</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => {
            e.preventDefault()
            const text = note.trim()
            if (!text) return
            setNoteOpen(false)
            startTool("text", text)
          }}>
            <Input aria-label="标注文字" autoFocus value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：箱顶放量" />
            <DialogFooter className="mt-4"><Button type="submit" disabled={!note.trim()}>放置</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={layoutOpen} onOpenChange={setLayoutOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>存为我的布局</DialogTitle>
            <DialogDescription>保存现在打开的主图、副图和指标库里勾上的指标，以后在「指标」里一键切换。</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => {
            e.preventDefault()
            const name = layoutName.trim()
            if (!name) return
            setLayouts([...layouts.filter((l) => l.name !== name), { name, panes, lib: libOn }])
            setLayoutOpen(false)
            toast(`已存为「${name}」`)
          }}>
            <Input aria-label="布局名称" autoFocus value={layoutName} onChange={(e) => setLayoutName(e.target.value)} placeholder="例如：看箱体" />
            {layouts.length > 0 && (
              <div className="mt-3">
                <div className="text-[0.75rem] text-subtle-foreground">已存的布局（同名保存会覆盖）</div>
                {layouts.map((l) => (
                  <div key={l.name} className="flex h-7 items-center gap-2 text-[0.8125rem]">
                    <span className="min-w-0 flex-1 truncate">{l.name}</span>
                    <Button type="button" variant="ghost" size="sm" className="h-6 px-1.5 text-[0.75rem]" aria-label={`删除布局「${l.name}」`}
                      onClick={() => {
                        const before = layouts
                        setLayouts(layouts.filter((x) => x.name !== l.name))
                        toast(`已删除布局「${l.name}」`, { action: { label: "撤销", onClick: () => setLayouts(before) }, duration: 8000 })
                      }}>删除</Button>
                  </div>
                ))}
              </div>
            )}
            <DialogFooter className="mt-4"><Button type="submit" disabled={!layoutName.trim()}>保存</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {labelDraft && (
        <LabelDialog draft={labelDraft.draft} bars={labelDraft.bars} basis={basis} digits={digits}
          onClose={() => setLabelDraft(null)}
          onSaved={() => {
            ctl.current?.removeDrawing(labelDraft.rectId) // the sample box is drawn by the labels layer now
            setLabelDraft(null)
            void qc.invalidateQueries({ queryKey: ["labels"] })
            void qc.invalidateQueries({ queryKey: ["agreement"] })
            if (labelDraft.draft.redraw_of) setSearch((s) => { const n = new URLSearchParams(s); n.delete("redraw"); return n }, { replace: true })
          }} />
      )}
    </div>
  )
}
