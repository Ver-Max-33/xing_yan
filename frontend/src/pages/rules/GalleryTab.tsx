// 样例图集 (spec §6.3, §6.5; design 07): the rule on a fixed sample (300 stocks, the last two years, split
// between its markets), every hit as a thumbnail, and your verdict on each. The verdicts become the rule's
// regression test: every later change says which right hits it loses and which wrong ones it brings back.
// J/K move, Y/N judge and move on, Enter opens the hit in 单股调试, U undoes the last verdict.
import { keepPreviousData, useQueries, useQueryClient } from "@tanstack/react-query"
import { Shuffle } from "lucide-react"
import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { MiniChart } from "@/components/rules/MiniChart"
import { type Market, useDataIn } from "@/lib/api"
import { codeOf, marketOf } from "@/lib/format"
import { type Hit, type HitLabel, ruleEndpoints, type RuleRef, useHitLabels, type Verdict } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { leaveToFocus } from "@/lib/keys"
import { hitKey } from "./hits"

/** Hits to look at apart from the sample's: drawn with the version they came from. */
export interface Focus { title: string; hits: Hit[]; ref: RuleRef }

interface Props {
  ruleRef: RuleRef | null; ruleId: string; version: number | null; markets: string[]; seed: number; onSeed: (s: number) => void
  focus: Focus | null; onFocus: (f: Focus | null) => void; onOpen: (listingId: string, day: string) => void
  onMissed?: () => void   // 漏检排查: why a stock you expected is not a hit
}

type Filter = "all" | "unmarked" | "right" | "wrong" | "focus"
const SAMPLE = 300
const NOT_IDS = new Set(["listing_id", "signal_date", "name", "days"])
/** The structures a hit stands on (its structure stages' ids). */
const idsOf = (h: Hit) => Object.entries(h).filter(([k, v]) => !NOT_IDS.has(k) && typeof v === "string").map(([, v]) => v as string)

export function GalleryTab({ ruleRef, ruleId, version, markets: ruleMarkets, seed, onSeed, focus, onFocus, onOpen, onMissed }: Props) {
  const qc = useQueryClient()
  const dataIn = useDataIn()   // the markets with data (all of them when none has: each then says why it has nothing)
  const markets = ruleMarkets.filter((m) => dataIn.has(m as Market)).length ? ruleMarkets.filter((m) => dataIn.has(m as Market)) : ruleMarkets
  const n = Math.round(SAMPLE / Math.max(1, markets.length))
  const refKey = JSON.stringify(ruleRef)
  const galleries = useQueries({
    queries: markets.map((m) => ({
      queryKey: ["gallery", refKey, m, n, seed], queryFn: () => ruleEndpoints.gallery(ruleRef!, m as Market, { n, seed }),
      enabled: !!ruleRef, retry: false, staleTime: 10 * 60_000,
      // a new sample or a slider's value runs again: the hits shown stay (and J/K/Y/N/Enter on them) until the new ones
      // are in, not an empty gallery that loses the one you picked
      placeholderData: keepPreviousData,
    })),
  })
  const labelsQ = useHitLabels(ruleId)
  const verdicts = new Map((labelsQ.data ?? []).map((l) => [hitKey(l), l.verdict]))
  const sampleHits = galleries.flatMap((g) => g.data?.hits ?? [])
    .sort((a, b) => b.signal_date.localeCompare(a.signal_date) || a.listing_id.localeCompare(b.listing_id))
  const [filter, setFilter] = useState<Filter>(focus ? "focus" : "all")
  const [lastFocus, setLastFocus] = useState(focus)
  if (lastFocus !== focus) {
    setLastFocus(focus)
    setFilter(focus ? "focus" : "all")
  }
  const pool = filter === "focus" && focus ? focus.hits : sampleHits
  const shown = pool.filter((h) => {
    const v = verdicts.get(hitKey(h))
    return filter === "unmarked" ? !v : filter === "right" ? v === "right" : filter === "wrong" ? v === "wrong" : true
  })
  const drawWith = filter === "focus" && focus ? focus.ref : ruleRef
  const [sel, setSel] = useState<string | null>(null)
  const undo = useRef<{ hit: Hit; prev: Verdict | null }[]>([])
  const writes = useRef<Promise<void>>(Promise.resolve()) // one at a time, in order: N then U must end as no verdict

  const setVerdict = (h: Hit, v: Verdict | null, remember = true) => {
    const k = hitKey(h)
    if (remember) undo.current.push({ hit: h, prev: verdicts.get(k) ?? null })
    qc.setQueryData<HitLabel[]>(["hit_labels", ruleId], (old = []) => {
      const rest = old.filter((x) => hitKey(x) !== k)
      return v ? [...rest, { listing_id: h.listing_id, signal_date: h.signal_date, verdict: v, rule_version: version }] : rest
    })
    writes.current = writes.current.then(async () => {
      try {
        await ruleEndpoints.setHitLabel(ruleId, h.listing_id, h.signal_date, v, version ?? undefined)
        void qc.invalidateQueries({ queryKey: ["rules"] }) // the library counts your verdicts
      } catch (e) {
        toast.error(`没记上：${String(e)}`)
        void qc.invalidateQueries({ queryKey: ["hit_labels", ruleId] })
      }
    })
  }

  // keys: J/K move, Y/N judge and move on, Enter opens, U undoes. Attached before the browser paints: a key pressed
  // as soon as the hits show must reach the handler that knows them, not the previous one (an empty list)
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (leaveToFocus(e)) return   // a focused control keeps its keys (lib/keys)
      if (shown.length === 0) return
      const k = e.key.toLowerCase()
      const i = Math.max(0, shown.findIndex((h) => hitKey(h) === sel))
      const go = (j: number) => setSel(hitKey(shown[Math.max(0, Math.min(shown.length - 1, j))]))
      if (k === "j" || k === "k") { e.preventDefault(); go(sel === null ? 0 : i + (k === "j" ? 1 : -1)) }
      else if (k === "y" || k === "n") { e.preventDefault(); setVerdict(shown[i], k === "y" ? "right" : "wrong"); go(i + 1) }
      else if (k === "enter") { e.preventDefault(); onOpen(shown[i].listing_id, shown[i].signal_date) }
      else if (k === "u") {
        const last = undo.current.pop()
        if (last) { setVerdict(last.hit, last.prev, false); setSel(hitKey(last.hit)) }
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })
  useEffect(() => {
    if (sel) document.querySelector(`[data-hit="${CSS.escape(sel)}"]`)?.scrollIntoView({ block: "nearest" })
  }, [sel])

  const count = (f: Filter) => (f === "focus" ? focus?.hits.length ?? 0 : sampleHits.filter((h) => {
    const v = verdicts.get(hitKey(h))
    return f === "unmarked" ? !v : f === "right" ? v === "right" : f === "wrong" ? v === "wrong" : true
  }).length)
  const loading = galleries.some((g) => g.isFetching)
  const failures = markets.flatMap((m, i) => (galleries[i]?.error ? [`${m}：${String(galleries[i].error)}`] : []))
  const failed = failures.length === markets.length ? failures.join("；") : null // one market failing leaves the other's hits
  const listings = galleries.reduce((s, g) => s + (g.data?.listings ?? 0), 0)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-border px-3">
        <Tabs value={filter} onValueChange={(v) => { setFilter(v as Filter); if (v !== "focus") onFocus(null) }}>
          <TabsList>
            {focus && <TabsTrigger value="focus">{focus.title} <span className="num ml-1 text-subtle-foreground">{count("focus")}</span></TabsTrigger>}
            {(["all", "unmarked", "right", "wrong"] as const).map((f) => (
              <TabsTrigger key={f} value={f}>{{ all: "全部", unmarked: "未标注", right: "对", wrong: "错" }[f]} <span className="num ml-1 text-subtle-foreground">{count(f)}</span></TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <span className="flex-1" />
        <span className="num text-[0.8125rem] text-subtle-foreground">
          抽样 {listings || SAMPLE} 只（{markets.map((m) => `${m} ${n}`).join(" / ")}）· 近 2 年 · 第 {seed + 1} 批{version === null ? " · 草稿" : ` · v${version}`}
        </span>
        <Button variant="ghost" size="sm" onClick={() => onSeed(seed + 1)}><Shuffle className="size-4" />换一批样本</Button>
      </div>
      {/* data-loading: one market's hits can come before the other's, which then reorders the list */}
      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="gallery" data-loading={loading ? "true" : "false"}>
        {ruleRef && !failed && failures.length > 0 && (
          <div className="border-b border-border px-3 py-2 text-[0.8125rem] text-error" data-testid="gallery-failed">试跑失败：{failures.join("；")}（下面只有其他市场的命中）</div>
        )}
        {!ruleRef ? <div className="p-8 text-center text-[0.875rem] text-subtle-foreground">规则有错误，先在「规则」里改好</div>
          : failed ? <div className="p-8 text-center text-[0.875rem] text-error" data-testid="gallery-failed">试跑失败：{failed}</div>
            : loading && sampleHits.length === 0 && filter !== "focus" ? <div className="p-8 text-center text-[0.875rem] text-subtle-foreground">在样本上试跑…（300 只 × 2 年，几秒）</div>
              : shown.length === 0 ? (filter !== "all" ? <div className="p-8 text-center text-[0.875rem] text-subtle-foreground">这一栏是空的</div> : (
                <div className="flex flex-col items-center gap-3 p-8 text-[0.875rem] text-subtle-foreground">
                  这批样本里没有命中。
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => onSeed(seed + 1)}><Shuffle className="size-4" />换一批样本</Button>
                    {onMissed && <Button size="sm" variant="outline" onClick={onMissed}>漏检排查：看该命中的股票差在哪</Button>}
                  </div>
                </div>)) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">
                  {shown.map((h) => {
                    const k = hitKey(h)
                    const v = verdicts.get(k)
                    return (
                      <div key={k} data-hit={k} onClick={() => setSel(k)} onDoubleClick={() => onOpen(h.listing_id, h.signal_date)}
                        className={cn("cursor-default border-r border-b border-border px-3 pt-2 pb-2", k === sel && "bg-accent/60 ring-1 ring-inset ring-structure/60")}>
                        <div className="flex items-baseline gap-2 text-[0.875rem]">
                          <span className="min-w-0 truncate font-medium">{h.name ?? codeOf(h.listing_id)}</span>
                          <span className="num shrink-0 text-[0.75rem] text-subtle-foreground">{codeOf(h.listing_id)}</span>
                          <span className="num ml-auto shrink-0 text-[0.75rem] text-subtle-foreground">{h.signal_date}</span>
                        </div>
                        <div className="h-4 text-[0.75rem] text-subtle-foreground">{h.days > 1 ? `连续 ${h.days} 天成立，只算第一天` : ""}</div>
                        {drawWith && <MiniChart ruleRef={drawWith} listingId={h.listing_id} day={h.signal_date} ids={idsOf(h)} digits={marketOf(h.listing_id) === "JP" ? 1 : 2} />}
                        <div className="flex h-8 items-center gap-2 text-[0.8125rem]">
                          {v ? <>
                            <span className={cn("flex-1", v === "right" ? "text-structure" : "text-warning")}>{v === "right" ? "✓ 已标为「对」" : "✕ 已标为「错」"}</span>
                            <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[0.75rem]" onClick={() => setVerdict(h, null)}>改</Button>
                          </> : <>
                            <span className="flex-1 text-subtle-foreground">这个判断对吗？</span>
                            <Button variant="outline" size="sm" className="h-6 px-1.5 text-[0.75rem]" aria-label={`${h.name ?? h.listing_id} ${h.signal_date} 对`}
                              onClick={() => setVerdict(h, "right")}><Kbd>Y</Kbd>对</Button>
                            <Button variant="outline" size="sm" className="h-6 px-1.5 text-[0.75rem]" aria-label={`${h.name ?? h.listing_id} ${h.signal_date} 错`}
                              onClick={() => setVerdict(h, "wrong")}><Kbd>N</Kbd>错</Button>
                          </>}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
      </div>
      <div className="flex h-9 shrink-0 items-center gap-3 border-t border-border px-3 text-[0.8125rem] text-subtle-foreground">
        <span><Kbd>J</Kbd><Kbd>K</Kbd> 上一个 / 下一个</span><span><Kbd>Y</Kbd> 对</span><span><Kbd>N</Kbd> 错</span>
        <span><Kbd>↵</Kbd> 打开大图</span><span><Kbd>U</Kbd> 撤销</span>
        <span className="ml-auto">你的判断会变成回归测试：以后改规则时，会提醒你哪些判断被推翻</span>
      </div>
    </div>
  )
}
