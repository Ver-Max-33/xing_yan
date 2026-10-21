// The K-line page's right panel for rules (design 03): the selected rule's hits from its latest market
// run (spec §7.1), and what is on the chart — the clicked bar's explanation, or the structures known
// on the day shown (box, left pressure, your samples).
import { ChevronRight } from "lucide-react"
import { type KeyboardEvent, type ReactNode, useState } from "react"
import { Link } from "react-router"
import { DangerTags } from "@/components/DangerTags"
import { DetailCard } from "@/components/rules/DetailCard"
import { breakDistance } from "@/components/rules/distance"
import { asOf, type BarLen, boxCandles, boxesOnDay, boxSince, boxText, goldName, ladder, pendingBreak, type Rung, SCALE_NAME, spanOf, strongSet } from "@/chart/structures"
import { codeOf } from "@/lib/format"
import type { Danger } from "@/lib/danger"
import { type Screen, screenLink, type ScreenRow, type ScreenView, viewText } from "@/lib/jobs"
import type { Box, Label, Pressure, PreviewDetail, Signal, Structures } from "@/lib/rules"
import { type Period, PERIOD_NAME } from "@/lib/api"
import { cn } from "@/lib/utils"

/** 命中: the list the 选股 page shows — its rule, 同时满足, 附加条件 and filters (lib/jobs ScreenView) — in its order, for
 *  J / K to walk; the link goes back to change it. */
export function HitList({ view, screen, rows, listingId, onOpen, danger }: {
  view: ScreenView; screen: { data?: Screen; isFetching: boolean; error: unknown }; rows: ScreenRow[]
  listingId?: string; onOpen: (id: string) => void; danger?: Record<string, Danger[]>
}) {
  const hint = "px-2 py-6 text-center text-[0.8125rem] text-subtle-foreground"
  if (!view.rule) return <div className={hint}>在<Link to="/screen" className="underline">选股</Link>页选一条规则（可以加同时满足、附加条件），这里列出同样的股票，按 J / K 逐只看</div>
  const s = screen.data
  const stages = Object.values(s?.markets ?? {}).find(Boolean)?.stages ?? s?.stages ?? []
  const scanned = Object.values(s?.markets ?? {}).some(Boolean)
  return (
    <div>
      <div className="px-2 py-1.5 text-[0.75rem] text-subtle-foreground">
        <Link to={screenLink(view)} className="hover:text-foreground" title="到选股页改规则和条件">
          选股：{viewText(view, s?.rule.name ?? null, s?.also?.name ?? null, view.step !== null ? stages[view.step]?.label : null)}</Link>
        {s && <span className="num"> · {rows.length} 只</span>}
      </div>
      {screen.error ? <div className={hint}>读不出来：{String(screen.error)}</div>
        : !s ? <div className={hint}>{view.where.length ? "按附加条件计算中…" : "读取中…"}</div>
        : !scanned ? <div className={hint}>这条规则还没有全市场扫描：到<Link to={screenLink(view)} className="underline">选股</Link>页点「重新扫描」</div>
        : rows.length === 0 ? <div className={hint}>没有符合的股票</div> : null}
      {rows.map((r) => (
        <button key={r.listing_id} type="button" onClick={() => onOpen(r.listing_id)} data-active={r.listing_id === listingId || undefined}
          className={cn("flex h-8 w-full items-center gap-2 rounded-md px-2 text-left hover:bg-accent", r.listing_id === listingId && "bg-accent")}>
          <span className="min-w-0 flex-1 truncate">{r.name ?? codeOf(r.listing_id)}</span>
          <DangerTags ws={danger?.[r.listing_id]} compact />
          <span className="num w-14 text-[0.75rem] text-subtle-foreground">{codeOf(r.listing_id)}</span>
          <span className={cn("w-24 shrink-0 truncate text-right text-[0.75rem]", r.status === "signal" ? "text-up" : r.status === "unknown" ? "text-warning" : "text-structure")}>
            {r.status === "signal" ? "命中" : r.status === "watch" ? breakDistance(r.detail) ?? "潜伏" : r.status === "unknown" ? "无法判断"
              : r.recent_signal ? `近 5 日 ${r.recent_signal.slice(5)}` : "走到这一步"}
          </span>
        </button>
      ))}
    </div>
  )
}

interface FoundProps {
  structures: Structures | null; day: string | null; pressure: Pressure | null; labels: Label[]; signals: Signal[]
  detail: PreviewDetail | null; ruleName: string | null; digits: number; period: Period
  barLen?: BarLen   // 箱内 K 线平均长度 over the loaded bars
  price: number | null                                   // the last price on the chart (the replay day's close; the bar forming)
  bars: { date: string; close: number }[]                // the settled bars on the chart, up to the day shown
  dates?: string[]                                       // the stock's bar days in a replay: a slanted edge cut on the day by bar
  focus?: string | null                                  // the structure picked here, drawn in full on the chart
  onFocus?: (id: string | null) => void                  // picked (null: back to 当前形态)
  onPeek?: (id: string | null, rung?: Rung) => void      // the pointer resting on a row (null: it left); a 价位梯 row: its line too
  onReveal?: (from: string, to: string | null) => void   // a picked one off the screen is brought into view
}

// on the A-share market 2016–2026 the fifth of 大箱 breakouts with the shortest candles inside (≤ 2.8% a day) did best over
// the next 20 days, the longest fifth (> 4.5%) worst, in both halves (ledger 2026-10-07)
const CALM = 0.028
const MORE = 10   // 以前的结构 listed at a time

/** A structure in the panel: the pointer resting on it shows it on the chart, a click picks it (drawn in full, the rest
 *  faint; brought into view when it is off the screen), a second click or Esc lets it go. */
function Row({ id, from, to, rung, focus, onFocus, onPeek, onReveal, className, title, children }: Pick<FoundProps, "focus" | "onFocus" | "onPeek" | "onReveal"> & {
  id: string; from: string; to: string | null; rung?: Rung; className?: string; title?: string; children: ReactNode
}) {
  const pick = () => {
    const next = focus === id ? null : id
    onFocus?.(next)
    if (next && from) onReveal?.(from, to)
  }
  return (
    <div role="button" tabIndex={0} aria-pressed={focus === id} data-testid="structure-row" data-id={id}
      title={title ?? (focus === id ? "再点一下（或 Esc）回到当前形态" : "在图上完整画出这一个，其余变淡")}
      onMouseEnter={() => onPeek?.(id, rung)} onMouseLeave={() => onPeek?.(null)} onClick={pick}
      onKeyDown={(e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick() } }}
      className={cn("-mx-1.5 cursor-pointer rounded-md px-1.5 py-1 hover:bg-accent/60", focus === id && "bg-accent ring-1 ring-ring/40", className)}>
      {children}
    </div>
  )
}

const pct = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}%`

/** A line's sample, as the chart draws it: the box's colour (突破线 and 失效线 heavier, 失效线 dashed), the pressure zone's
 *  tint, 现价 a dot. */
function Swatch({ r }: { r: Rung }) {
  const mark = r.kind === "price" ? <span className="mx-auto size-2 rounded-full bg-foreground" />
    : r.kind === "pressure" ? <span className={cn("h-1.5 rounded-[1px]", r.warn ? "bg-warning/60" : "bg-warning/25")} />
    : <span style={{ borderTop: `${r.kind === "break" || r.kind === "fail" ? 2 : 1}px ${r.dash ? "dashed" : "solid"} var(${r.color})` }} />
  return <span aria-hidden className="flex h-[1lh] flex-col justify-center self-start">{mark}</span>   // on the first line
}

/** 价位梯: the plan's lines by price — price, its sample, what it is, how far from 现价. The decisions (突破线, 左侧压力,
 *  失效线) and 现价 read in full; the box's edges, the context, quieter. */
function Ladder({ rungs, price, digits, known, row }: { rungs: Rung[]; price: number | null; digits: number; known: (id: string) => boolean
  row: Pick<FoundProps, "focus" | "onFocus" | "onPeek" | "onReveal"> }) {
  return (
    // a row's words open to the left, over the chart's edge — under it, they would cover the next price
    <div role="list" aria-label="价位梯" data-testid="ladder" data-tip-side="left">
      {rungs.map((r) => {
        const loud = r.kind !== "top" && r.kind !== "bottom"
        const line = (
          <div role="listitem" className="grid grid-cols-[3.5rem_0.875rem_minmax(0,1fr)_3.25rem] items-baseline gap-x-2">
            <span className={cn("num text-right", r.kind === "price" ? "font-medium" : !loud && "text-muted-foreground")}>{r.price.toFixed(digits)}</span>
            <Swatch r={r} />
            {/* never cut short: a note that does not fit goes to the next line whole */}
            <span className={cn("flex min-w-0 flex-wrap items-baseline gap-x-1.5", r.kind === "price" ? "font-medium" : !loud && "text-muted-foreground")}>
              <span>{r.label}</span>
              {r.note && <span className={cn("text-[0.75rem] whitespace-nowrap", r.warn ? "text-warning" : "text-subtle-foreground")}>{r.note}</span>}</span>
            <span className="num text-right text-[0.75rem] text-muted-foreground">{r.kind === "price" || price === null ? "" : pct(r.price / price - 1)}</span>
          </div>
        )
        return r.id && known(r.id)
          ? <Row key={`${r.kind}:${r.id}`} id={r.id} from={r.from ?? ""} to={r.to} rung={r} {...row} className="py-0.5" title={r.hint || undefined}>{line}</Row>
          : <div key={`${r.kind}:${r.id ?? ""}`} className="-mx-1.5 px-1.5 py-0.5" title={r.hint || undefined}>{line}</div>
      })}
    </div>
  )
}

/** 识别到的结构: the explanation of the clicked bar; else 当前形态 as a plan, as a chart is read after the close —
 *  first the pattern (what box, a 金品, still inside or left), then the lines (价位梯: where to be alerted, the room above,
 *  where it fails) — and the structures before it, folded away. */
export function Found({ structures, day, pressure, labels, signals, detail, ruleName, digits, period, barLen, price, bars, dates, focus = null,
  onFocus, onPeek, onReveal }: FoundProps) {
  const [more, setMore] = useState(MORE)
  const [history, setHistory] = useState(false)
  if (detail) return <DetailCard data={detail} className="pb-2" />
  const s = structures ? asOf(structures, day, dates) : null
  const shown = boxesOnDay(s?.boxes ?? [])
  const [main, inner] = shown
  const now = s ? strongSet(s, null, []) : new Set<string>()
  const byId = new Map((s?.boxes ?? []).map((b) => [b.id, b]))
  const golds = [...new Set((s?.gold ?? []).filter((g) => shown.some((b) => b.id === g.big_box_id)).map((g) => g.small_box_id))]
  const others = (s?.boxes ?? []).filter((b) => b.status === "established" && b.scale !== "short" && !now.has(b.id))
    .sort((a, b) => b.start.localeCompare(a.start))
  const rungs = s ? ladder(s, price, pressure, bars, digits) : []
  const shownSignals = day ? signals.filter((x) => x.signal_date <= day) : signals
  const row = { focus, onFocus, onPeek, onReveal }
  const range = (b: Box | undefined) => (b ? `${b.segments[0]?.bottom.toFixed(digits)} – ${b.segments[0]?.top.toFixed(digits)}` : "")
  const year = (day ?? bars.at(-1)?.date ?? "").slice(0, 4)
  const when = (d: string) => { const t = d.replace("T", " "); return t.slice(0, 4) === year ? t.slice(5) : t }   // a minute bar: 10-12 09:35
  const k = (s?.tol ?? 0.015) / 0.015   // another period's bar against a day's (its band against a day's 1.5%)
  const meta = (b: Box, brief = false) => {   // brief: the 中箱 inside, a line of its own
    const len = brief ? null : boxCandles(b, day, barLen)
    return [b.width !== null ? `箱宽 ${(b.width * 100).toFixed(1)}%` : null, `成立 ${when(b.first_known ?? b.known_at)}`,
      brief ? null : `触点 上 ${b.touches.filter((t) => t.side === "top").length} 下 ${b.touches.filter((t) => t.side === "bottom").length}`,
      len != null ? `箱内 K 线 ${(len * 100).toFixed(1)}%${len <= CALM * k ? "（很平稳）" : ""}` : null].filter(Boolean).join(" · ")
  }
  const why = period === "day" ? "箱内 K 线：每根（最高 − 最低）÷ 前一天收盘，在箱里的平均，越短越平稳——A 股 2016–2026 年大箱突破里，最短的五分之一（2.8% 以下）之后 20 天表现最好，最长的五分之一最差"
    : `箱内 K 线：每根（最高 − 最低）÷ 前一根收盘，在箱里的平均，越短越平稳（${PERIOD_NAME[period]}按它一根 K 线的大小换算日线的 2.8%）`
  return (
    <div className="flex flex-col gap-2 pb-2 text-[0.8125rem]" data-testid="found">
      {period !== "day" && (   // another period's own structures (design 2026-10-12-structures-every-period)
        <div className="text-[0.75rem] text-subtle-foreground" data-testid="found-period">
          {PERIOD_NAME[period]}自己的结构（在这个周期的 K 线上算），破箱容差 {((s?.tol ?? 0.015) * 100).toFixed(2).replace(/\.?0+$/, "")}%。
          规则信号和左侧压力只在日线上</div>
      )}
      {(focus || day) && (
        <div className="flex items-center text-[0.75rem] text-subtle-foreground">{day ? `当前形态 · ${day}` : ""}
          {focus && <button type="button" className="ml-auto hover:text-foreground" onClick={() => onFocus?.(null)}>回到当前形态（Esc）</button>}
        </div>
      )}
      {main ? (
        <div>
          <div className="flex flex-wrap items-center gap-x-1 gap-y-0.5">
            <Row id={main.id} from={main.start} to={main.end} {...row} className="py-0.5 text-[0.875rem] font-medium">
              {SCALE_NAME[main.scale]} · {main.ended_at ? `${main.end_reason ?? "已结束"}${main.end ? ` ${when(main.end)}` : ""}`
                : pendingBreak(main) === "up" ? "向上破箱（等离箱或回箱）" : pendingBreak(main) === "down" ? "向下破箱（等回箱或离箱）" : "仍在箱内"}
            </Row>
            {golds.map((id) => {
              const small = byId.get(id), span = spanOf(s!, id)   // a 变异金品's small box is a slant one
              const name = goldName(s!.gold, id, new Set(shown.map((b) => b.id)), s!.boxes)
              return (
                <Row key={id} id={id} from={span?.start ?? ""} to={span?.end ?? null} {...row}
                  className="mx-0 rounded-md bg-warning/15 px-1.5 py-0 text-[0.75rem] text-warning hover:bg-warning/25"
                  title={`${name} · ${small ? `小箱${small.length ? ` ${small.length} 根` : ""} · ${range(small)}` : "斜的小箱"}（点一下在图上画出）`}>
                  {name}
                </Row>
              )
            })}
          </div>
          <div className="num text-[0.75rem] text-muted-foreground" title={`${boxSince(main)}。${why}`}>{meta(main)}</div>
          {inner && (
            <Row id={inner.id} from={inner.start} to={inner.end} {...row} className="py-0.5 text-[0.75rem] text-muted-foreground"
              title={`${meta(inner)}。${boxSince(inner)}`}>
              里面的中箱 · <span className="num">{meta(inner, true)}</span>
            </Row>
          )}
        </div>
      ) : <div className="text-subtle-foreground">这一段没有已确认的中、大箱</div>}
      {rungs.some((r) => r.kind !== "price") && (
        <Ladder rungs={rungs} price={price} digits={digits} known={(id) => !!s && spanOf(s, id) !== null} row={row} />
      )}
      {pressure?.none_above && <div className="text-[0.75rem] text-muted-foreground">上方没有左侧压力</div>}
      {(ruleName || labels.length > 0) && (
        <div className="text-[0.75rem] text-muted-foreground">
          {ruleName && <div>{ruleName}：{shownSignals.length ? `${shownSignals.length} 个信号，最近 ${shownSignals[shownSignals.length - 1].signal_date}` : "这只股票没有信号"}（点一根 K 线看判定）</div>}
          {labels.length > 0 && <div>你的样例 · {labels.length} 个</div>}
        </div>
      )}
      {others.length > 0 && (
        <div className="flex flex-col">
          <button type="button" aria-expanded={history} onClick={() => setHistory(!history)}
            className="flex items-center gap-1 self-start text-[0.75rem] text-muted-foreground hover:text-foreground">
            <ChevronRight className={cn("size-3.5 transition-transform", history && "rotate-90")} />以前的结构 {others.length} 个
          </button>
          {history && others.slice(0, more).map((b) => {
            const kinds = [...new Set((s?.gold ?? []).filter((g) => g.big_box_id === b.id).map((g) => goldName(s!.gold, g.small_box_id, new Set([b.id]))))]
            return (
              <Row key={b.id} id={b.id} from={b.start} to={b.end} {...row}>
                <span>{boxText(b)}</span>
                <span className="num text-muted-foreground"> · {b.start} → {b.end ?? "仍在"}{b.end_reason ? ` · ${b.end_reason}` : ""}{kinds.length ? ` · ${kinds.join("、")}` : ""}</span>
              </Row>
            )
          })}
          {history && others.length > more && <button type="button" className="self-start text-[0.75rem] text-muted-foreground hover:text-foreground"
            onClick={() => setMore(more + MORE)}>再显示 {Math.min(MORE, others.length - more)} 个</button>}
        </div>
      )}
    </div>
  )
}
