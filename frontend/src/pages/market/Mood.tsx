// 情绪 in one row (redesign §2.3): the phase and its days (how it got here and what it does not mean on hover), how the
// day compares with the same minute of yesterday, six numbers each beside yesterday's at that minute, and the limit-ups
// by minute today against the day before. A stored day's close shows its own six, with no "yesterday".
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { BoardResponse, PhaseSummary, TapeResponse } from "@/lib/market"
import { moodNote, phaseDays } from "@/lib/marketWords"
import { cn } from "@/lib/utils"
import { count, rate, useMoved } from "./fmt"
import { MiniLine } from "./MiniLine"
import { Moved } from "./parts"

const HOT = ["启动", "主升", "高潮"], COLD = ["冰点", "退潮"]
const phaseTone = (p: string) => (HOT.includes(p) ? "text-up ring-1 ring-inset ring-up/45" : COLD.includes(p) ? "text-down ring-1 ring-inset ring-down/45" : "bg-accent text-foreground")
const FILL: Record<string, string> = { 高潮: "fill-up/40", 主升: "fill-up/25", 启动: "fill-up/12", 平稳: "fill-transparent", 修复: "fill-down/10", 退潮: "fill-down/25", 冰点: "fill-down/40" }

/** The last 60 days: limit-ups, the share up, new highs less new lows — on the phases' shades. */
function PhaseChart({ p }: { p: PhaseSummary }) {
  const d = p.recent, n = d.length
  if (n < 2) return null
  const W = 260, H = 34
  const line = (vals: (number | null)[]) => {
    const v = vals.map((x) => x ?? 0), lo = Math.min(...v), hi = Math.max(...v)
    return v.map((x, i) => `${i ? "L" : "M"}${((i / (n - 1)) * W).toFixed(1)} ${(H - 2 - ((x - lo) / (hi - lo || 1)) * (H - 4)).toFixed(1)}`).join("")
  }
  const rows: [string, (number | null)[], string][] = [["涨停家数", d.map((x) => x.limit_up), "stroke-up"], ["上涨占比", d.map((x) => x.up_ratio), "stroke-foreground"],
    ["新高 − 新低", d.map((x) => x.net_high), "stroke-structure"]]
  return (
    <div className="flex flex-col gap-1.5 py-1">
      <div className="text-[0.75rem]">近 {n} 个交易日（底色是每天的阶段，红热绿冷）</div>
      {rows.map(([label, vals, tone]) => (
        <div key={label}>
          <div className="text-[0.6875rem] text-muted-foreground">{label}</div>
          <svg width={W} height={H} aria-hidden>
            {d.map((x, i) => <rect key={x.date} x={(i / n) * W} y={0} width={W / n + 0.5} height={H} className={FILL[x.phase] ?? "fill-transparent"} />)}
            <path d={line(vals)} className={cn("fill-none", tone)} strokeWidth={1.2} />
          </svg>
        </div>
      ))}
    </div>
  )
}

function Metric({ label, value, then, title, moved, testid }: {
  label: string; value: string; then: string | null; title: string; moved?: "up" | "down" | null; testid: string
}) {
  return (
    <span className="flex items-baseline gap-1.5 whitespace-nowrap" title={title} data-testid={testid}>
      <span className="text-[0.75rem] text-muted-foreground">{label}</span>
      <span className="num text-[0.9375rem] font-medium">{value}</span><Moved dir={moved ?? null} />
      {then != null && <span className="num text-[0.6875rem] text-subtle-foreground">昨 {then}</span>}
    </span>
  )
}

export function Mood({ r, tape }: { r: BoardResponse; tape: TapeResponse | undefined }) {
  const b = r.board!, lim = b.limits, live = r.mode === "live", y = live ? r.yesterday : null
  const moved = {
    limit_up: useMoved(lim.limit_up, 3), broken: useMoved(lim.broken, 3), seal: useMoved(lim.seal_rate, 0.05),
    promo: useMoved(lim.promo_rate, 0.05), down: useMoved(lim.limit_down, 3),
  }
  const p = r.phase
  const day = (r.as_of ?? "").slice(5)
  const read = moodNote(r)
  const then = (v: number | null | undefined, f: (x: number | null | undefined) => string) => (y ? f(v) : null)
  const today = tape ? tape.rows.filter((x) => x.limit_up != null).map((x) => [x.i, x.limit_up!] as [number, number]) : []
  const before = tape ? tape.prev_rows.filter((x) => x.limit_up != null).map((x) => [x.i, x.limit_up!] as [number, number]) : []
  const height = lim.height == null ? "未知" : `${lim.height} 板`
  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2.5 rounded-md border border-border px-4 py-3" data-testid="market-mood" id="sec-mood">
      {p ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className={cn("cursor-default rounded px-2 py-0.5 text-[0.75rem] font-medium whitespace-nowrap", phaseTone(p.phase))} data-testid="market-phase">
              {p.phase} · 第 {phaseDays(p, r)} 天</span>
          </TooltipTrigger>
          <TooltipContent side="bottom" align="start" className="max-w-[18rem]">
            <p className="text-[0.75rem]">最近：{p.path.map((x) => `${x.phase}${x.days > 1 ? ` ${x.days} 天` : ""}`).join(" → ")}</p>
            <PhaseChart p={p} />
            <p className="text-[0.6875rem] text-muted-foreground">阶段只描述现在：2020-08 以来各阶段第二天涨停股的表现差不多；箱体模板的越线在冰点那天最容易收不住（指标说明里有数）。</p>
          </TooltipContent>
        </Tooltip>
      ) : <span className="rounded bg-accent px-2 py-0.5 text-[0.75rem] whitespace-nowrap" data-testid="market-phase">阶段 未知</span>}
      {read && <span className="text-[0.75rem] text-muted-foreground" data-testid="market-read">{read}</span>}
      {!lim.known ? (
        <span className="text-[0.8125rem] text-subtle-foreground" data-testid="market-no-limits">{r.mode === "close"
          ? "这个数据档没有涨跌停价，涨停相关的数都是未知" : "当天的涨跌停价还没取到（从 Tushare 取，确认了就有），涨停相关的数先当未知"}</span>
      ) : (
        <span className="flex flex-wrap items-baseline gap-x-5 gap-y-1.5">
          <Metric testid="mood-limit_up" label="涨停" value={count(lim.limit_up)} then={then(y?.limit_up, count)} moved={moved.limit_up} title="涨停：最新价等于交易所的涨停价（盘中就是此刻封着）" />
          <Metric testid="mood-broken" label="炸板" value={count(lim.broken)} then={then(y?.broken, count)} moved={moved.broken} title="炸板：今天最高价碰过涨停价，此刻不在涨停" />
          <Metric testid="mood-seal_rate" label="封板率" value={rate(lim.seal_rate)} then={then(y?.seal_rate, rate)} moved={moved.seal} title="封板率 = 涨停 ÷（涨停 + 炸板）" />
          <Metric testid="mood-promo_rate" label="晋级率" value={rate(lim.promo_rate)} then={then(y?.promo_rate, rate)} moved={moved.promo}
            title={`晋级率 = 昨天收盘涨停的股票里此刻涨停的比例（昨天 ${count(lim.pool)} 只，此刻 ${count(lim.promoted)} 只；昨天不到 10 只时不算）`} />
          <Metric testid="mood-height" label="最高板" value={height} then={null}
            title="最高板：今天碰过涨停的股票里板数最高的（到昨天连续涨停 k 天、今天又涨停的是 k+1 板）" />
          {lim.height != null && <span className="text-[0.6875rem] whitespace-nowrap text-subtle-foreground">{lim.top_sealed ? "封住" : "炸板中"}{b.ladder[0]?.names[0] ? ` · ${b.ladder[0].names[0].name}` : ""}</span>}
          <Metric testid="mood-limit_down" label="跌停" value={count(lim.limit_down)} then={then(y?.limit_down, count)} moved={moved.down} title="跌停：最新价等于交易所的跌停价" />
        </span>
      )}
      {today.length > 1 && (
        <span className="ml-auto flex items-center gap-2">
          <MiniLine className="h-8 w-36" testid="mood-limit-line" format={(v) => String(v)}
            lines={[{ name: live ? "昨天" : "前一天", points: before, tone: "stroke-subtle-foreground", step: true },
              { name: live ? "今天" : day, points: today, tone: "stroke-up", step: true }]} />
          <span className="text-[0.6875rem] whitespace-nowrap text-subtle-foreground">{`涨停 ${live ? "今天" : day}${before.length > 1 ? ` / ${live ? "昨天" : "前一天"}` : ""}`}</span>
        </span>
      )}
    </section>
  )
}
