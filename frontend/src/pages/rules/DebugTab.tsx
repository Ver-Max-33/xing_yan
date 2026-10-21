// 单股调试 (spec §6.3, design 06): the rule on one stock — every structure it builds on, its signals, the
// judgement of any bar you click (stage by stage, with the numbers), the replay, and on the right the
// parameter sliders (they take effect at once), what the change does on the sample and to your verdicts,
// and why the box behind the judgement is drawn where it is.
import { useQuery } from "@tanstack/react-query"
import { ChevronDown, History } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { DetailCard } from "@/components/rules/DetailCard"
import { ListingPicker } from "@/components/rules/ListingPicker"
import { useLabelNotes } from "@/components/rules/labelNotes"
import { ParamSliders, type ParamValues } from "@/components/rules/ParamSliders"
import { ReplayBar } from "@/components/rules/ReplayBar"
import { ChartView } from "@/chart/ChartView"
import { type ChartController, DEFAULT_PANES } from "@/chart/controller"
import { isoDate } from "@/chart/data"
import { asOf, DEFAULT_LAYERS, LAYER_HINTS, LAYER_LABELS, LAYER_SWATCH, type Layers } from "@/chart/structures"
import { useInstrument } from "@/lib/api"
import { marketOf } from "@/lib/format"
import { type Names, NO_STRUCTURES, ruleEndpoints, type RuleParam, type RuleRef, type RuleStage, type Signal, useLabels } from "@/lib/rules"
import { usePersisted } from "@/lib/usePersisted"
import { BoxWhy } from "./BoxWhy"
import type { Focus } from "./GalleryTab"
import { Impact } from "./Impact"
import { paramUses } from "./ruleText"

interface Props {
  ruleRef: RuleRef | null       // what runs: the saved version, or the text being edited; null while the text has errors
  savedRef: RuleRef | null      // the saved version to measure a change against (null for a rule not saved yet)
  params: ParamValues
  onParams: (v: ParamValues) => void
  ruleParams: RuleParam[] | null  // the parameters as the rule's text writes them (the sliders' starting values)
  ruleStages?: RuleStage[]        // where each is read: its slider says so
  ruleNames?: Names               // … by the stages' and conditions' names
  listingId: string | null
  onListing: (id: string) => void
  markets: string[]
  sampleSeed: number
  onShowHits: (focus: Focus) => void
  pick: { listing: string; day: string } | null  // a day to open with (from the gallery, the missed-hit check, 版本对比)
}

export function DebugTab({ ruleRef, savedRef, params, onParams, ruleParams, ruleStages, ruleNames, listingId, onListing, markets, sampleSeed, onShowHits, pick }: Props) {
  const ctl = useRef<ChartController | null>(null)
  const [savedLayers, setLayers] = usePersisted<Layers>("rules.layers", DEFAULT_LAYERS)
  const layers = useMemo(() => ({ ...DEFAULT_LAYERS, ...savedLayers }), [savedLayers])   // a layer added since: its default
  const [picked, setPicked] = useState<string | null>(pick?.day ?? null) // ↵ in the gallery, a day from 漏检排查 / 版本对比
  const [replayOn, setReplayOn] = useState(false)
  const [replay, setReplay] = useState<string | null>(null)
  const { data: inst } = useInstrument(listingId ?? undefined)
  const key = JSON.stringify(ruleRef)
  // another stock: the old judgement goes with it (a moved slider or an edit judges the same day again)
  const [shown, setShown] = useState(listingId)
  if (shown !== listingId) {
    setShown(listingId)
    setPicked(null)
  }
  const [lastPick, setLastPick] = useState(pick)
  if (lastPick !== pick) {
    setLastPick(pick)
    setPicked(pick?.day ?? null)
  }
  useEffect(() => {   // a day opened from elsewhere (↵ in the gallery, 漏检排查, 版本对比) is brought into view
    if (pick) void ctl.current?.revealDay(pick.listing, pick.day)
  }, [pick])
  const previewQ = useQuery({
    queryKey: ["preview", key, listingId, "qfq"], queryFn: () => ruleEndpoints.preview(ruleRef!, listingId!, "qfq"),
    enabled: !!ruleRef && !!listingId, retry: false,
    // a moved slider keeps the chart (and the sliders) until the new result arrives — never another stock's
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[2] === listingId ? prev : undefined),
  })
  useEffect(() => {
    if (previewQ.error) toast.error(`预览失败：${String(previewQ.error)}`)
  }, [previewQ.error])
  const shownPick = picked && !(replayOn && replay && picked > replay) ? picked : null
  const detailQ = useQuery({
    queryKey: ["detail", key, listingId, shownPick], queryFn: () => ruleEndpoints.detail(ruleRef!, listingId!, shownPick!),
    enabled: !!ruleRef && !!listingId && !!shownPick, retry: false,
  })
  const datesQ = useQuery({ queryKey: ["dates", listingId], queryFn: () => ruleEndpoints.dates(listingId!), enabled: replayOn && !!listingId })

  const view = useMemo(() => (inst && listingId === inst.listing_id
    ? { listingId: inst.listing_id, market: inst.market, period: "day" as const, basis: "qfq" as const, name: inst.name ?? inst.code, indexId: inst.index_id }
    : null), [inst, listingId])
  const structures = previewQ.data?.structures ?? NO_STRUCTURES
  const signals = useMemo(() => previewQ.data?.signals ?? [], [previewQ.data])
  const bindings = useMemo(() => (shownPick ? detailQ.data?.detail.bindings ?? [] : []), [detailQ.data, shownPick])
  // your sample boxes on this stock with how the algorithm agrees with each (手画对照); in the replay, those drawn by then
  const labelsQ = useLabels(listingId ?? undefined, "qfq", !!listingId) // no stock yet: not everyone's samples
  const labels = useMemo(() => {
    const all = layers.labels ? labelsQ.data ?? [] : []
    return replayOn && replay ? all.filter((l) => l.cutoff <= replay) : all
  }, [layers.labels, labelsQ.data, replayOn, replay])
  const labelNotes = useLabelNotes(labels)
  useEffect(() => {
    ctl.current?.setStructures({ structures, layers, pressure: null, labels, labelNotes, highlight: bindings.map((b) => b.id).filter((x): x is string => !!x) })
  }, [structures, layers, bindings, labels, labelNotes])
  useEffect(() => {
    ctl.current?.setRule({ signals, bands: shownPick ? detailQ.data?.bands ?? [] : [] })
  }, [signals, detailQ.data, shownPick])
  useEffect(() => { ctl.current?.setReplay(replayOn ? replay : null) }, [replayOn, replay])
  useEffect(() => {
    const c = ctl.current
    if (c) c.onBarClick = (bar) => setPicked(isoDate(bar.timestamp))
  })

  const shownSignals = replayOn && replay ? signals.filter((s) => s.signal_date <= replay) : signals
  const box = bindings.find((b) => b.kind === "structure" && b.id)
  const known = replayOn && replay ? asOf(structures, replay) : structures // in the replay, the box as it was known that day
  const boxRow = box ? known.boxes.find((b) => b.id === box.id) ?? null : null
  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border px-3">
          <ListingPicker value={listingId} onPick={onListing} />
          {listingId && !markets.includes(marketOf(listingId)) && <span className="text-[0.8125rem] text-warning">这条规则不适用于 {marketOf(listingId)}</span>}
          <span className="flex-1" />
          <span className="text-[0.8125rem] text-subtle-foreground">{previewQ.isFetching ? "计算中…" : previewQ.data ? `${signals.length} 个信号` : ""}</span>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="ghost" size="sm">结构图层
              <span className="num text-[0.75rem] text-subtle-foreground">{LAYER_LABELS.filter(([k]) => k !== "pressure" && layers[k]).length} / {LAYER_LABELS.length - 1}</span>
              <ChevronDown className="size-3.5 text-subtle-foreground" /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              {LAYER_LABELS.filter(([k]) => k !== "pressure").map(([k, l]) => (
                <DropdownMenuCheckboxItem key={k} checked={layers[k]} title={LAYER_HINTS[k]} onSelect={(e) => e.preventDefault()}
                  onCheckedChange={(c) => setLayers({ ...layers, [k]: !!c })}>
                  {LAYER_SWATCH[k] && <span aria-hidden className="mr-1.5 inline-block w-3 align-middle"
                    style={{ borderTop: `2px ${LAYER_SWATCH[k]!.dash ? "dashed" : "solid"} var(${LAYER_SWATCH[k]!.color})` }} />}
                  {l}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <Button variant={replayOn ? "secondary" : "ghost"} size="sm" aria-pressed={replayOn} aria-label="回放"
            onClick={() => { setReplayOn(!replayOn); setReplay(null) }}><History className="size-4" />回放</Button>
        </div>
        <div className="min-h-0 flex-1">
          {listingId ? (
            <ChartView view={view} panes={{ ...DEFAULT_PANES, macd: false }} drawingsVisible={false} onReady={(c) => { ctl.current = c }}
              onError={(e) => toast.error(`图表数据加载失败：${String(e)}`)} />
          ) : <div className="flex h-full items-center justify-center text-[0.875rem] text-subtle-foreground">选一只股票，看规则在它身上怎么判</div>}
        </div>
        {replayOn && <ReplayBar dates={datesQ.data ?? []} value={replay} onChange={setReplay} onClose={() => { setReplayOn(false); setReplay(null) }} />}
        <div className="max-h-[36%] shrink-0 overflow-y-auto border-t border-border px-3 py-2" data-testid="judgement-log">
          <div className="flex items-center pb-1 text-[0.75rem] text-subtle-foreground">
            <span className="flex-1">判定日志{shownPick ? ` · ${shownPick}` : ""}</span>
            {shownPick && <button type="button" className="hover:text-foreground" onClick={() => setPicked(null)}>收起</button>}
          </div>
          {shownPick && detailQ.data ? <DetailCard data={detailQ.data} />
            : shownSignals.length ? (
              <div className="flex flex-wrap gap-1.5 text-[0.8125rem]">
                {[...shownSignals].reverse().map((s: Signal) => (
                  <button key={s.signal_date} type="button" onClick={() => { setPicked(s.signal_date); if (listingId) void ctl.current?.revealDay(listingId, s.signal_date) }}
                    className="num rounded-md border border-border px-2 py-0.5 hover:bg-accent">{s.signal_date}</button>
                ))}
              </div>
            ) : previewQ.error && !previewQ.data ? <div className="text-[0.8125rem] text-error" data-testid="preview-error">预览失败：{String(previewQ.error)}</div>
            : <div className="text-[0.8125rem] text-subtle-foreground">{previewQ.data ? "这只股票没有信号；点任意一根 K 线看那天差在哪" : "…"}</div>}
          {!shownPick && shownSignals.length > 0 && <div className="pt-1 text-[0.75rem] text-subtle-foreground">点一个信号日或任意一根 K 线，看各阶段怎么判、数值是多少</div>}
        </div>
      </div>
      <aside className="flex w-[18.75rem] shrink-0 flex-col overflow-y-auto border-l border-border">
        <section className="border-b border-border px-4 py-3">
          <div className="flex items-baseline pb-2 text-[0.875rem] font-semibold">参数<span className="ml-auto text-[0.75rem] font-normal text-subtle-foreground">拖动后图上立即重算</span></div>
          {ruleParams ? <ParamSliders params={ruleParams} values={params} onChange={onParams} uses={paramUses(ruleStages, ruleNames)} />
            : <div className="text-[0.8125rem] text-subtle-foreground">规则有错误，先在「规则」里改好</div>}
        </section>
        <Impact savedRef={savedRef} ruleRef={ruleRef} markets={markets} seed={sampleSeed} onShowHits={onShowHits} />
        <BoxWhy box={boxRow} digits={listingId && marketOf(listingId) === "JP" ? 1 : 2} />
      </aside>
    </div>
  )
}
