// The 分时图 (design 04, spec §8.5): a day's price by the minute, its average price (VWAP) and volume, with the
// previous close and the box top, on the trading day's own clock (lunch left out, the whole day's width reserved).
// Several days side by side (向左找位置): each day on its own clock and with its own average, one after another.
import type { Market } from "@/lib/api"
import { fmtPrice, shortDate } from "@/lib/format"
import { goldLabels, type Micro, type Minutes, placeOf, ticks, waveText } from "@/lib/live"

const W = 600, PRICE = 196, VOL_TOP = 214, VOL = 60, AXIS = 292, RIGHT = 60
const PLOT = W - RIGHT

/** `top`: the stock's box top (its row's, in today's prices), drawn when it is near the day's prices; `micro`: the
 * 分时小箱 of the days shown and their breaks, each with its volume against the bars near that edge; `waves`: each
 * wave's volume against the wave before. */
export function IntradayChart({ data, market, top: boxTop, micro, waves }: {
  data: Minutes; market: Market; top: number | null; micro?: Micro; waves?: boolean
}) {
  const m = data.minutes
  const last = m[m.length - 1]
  const days = data.days.length ? data.days : data.day ? [data.day] : []
  const span = Math.max(data.total * Math.max(days.length, 1), 1)
  const x = (i: number) => (i / span) * PLOT
  const at = (t: string) => x(placeOf(t, days, data.sessions, data.total))
  const values = m.flatMap((p) => [p.close, p.vwap])
  if (data.prev != null) values.push(data.prev)
  const top = boxTop != null && last && Math.abs(boxTop / last.close - 1) < 0.15 ? boxTop : null // a far top would flatten the day
  if (top != null) values.push(top)
  const range = Math.max(...values) - Math.min(...values)
  const pad = range * 0.08 || Math.max(...values) * 0.005 || 1
  const lo = Math.min(...values) - pad, hi = Math.max(...values) + pad
  const y = (v: number) => PRICE - ((v - lo) / (hi - lo)) * PRICE
  const vmax = Math.max(...m.map((p) => p.volume), 1)
  const bar = Math.max(PLOT / span - 0.3, 0.6)
  const byDay = days.map((_, k) => m.filter((p) => p.d === k))
  const path = (ps: typeof m, k: "close" | "vwap") => ps.map((p) => `${x(p.i).toFixed(1)},${y(p[k]).toFixed(1)}`).join(" ")
  const levels = [0.1, 0.3, 0.5, 0.7, 0.9].map((f) => lo + (hi - lo) * f)
  const first = days[0] ?? "", end = days[days.length - 1] ?? ""
  const shown = (t: string) => t.slice(0, 10) >= first && t.slice(0, 10) <= end
  // the small boxes' breaks on the days shown, in time order; a label too close to the one before on its side is left out
  const said: Record<string, number> = {}
  const breaks = (micro?.events ?? []).filter((e) => (e.event === "break_up" || e.event === "break_down") && shown(e.at))
    .sort((a, b) => a.at.localeCompare(b.at)).flatMap((e) => {
      const box = micro!.boxes.find((b) => b.box_id === e.box_id)
      const edge = box ? (e.event === "break_up" ? box.top : box.bottom) : null
      if (edge == null || edge < lo || edge > hi) return []
      const cx = at(e.at)
      const label = e.ref_ratio != null && !(cx - (said[e.event] ?? -Infinity) < 48)
      if (label) said[e.event] = cx
      return [{ e, cx, cy: y(edge), up: e.event === "break_up", label }]
    })
  const named = goldLabels(micro?.gold)   // 分时金品: a bigger box's last small box
  const spoke: Record<string, number> = {}
  const marks = waves ? (micro?.waves ?? []).filter((w) => shown(w.end) && w.end_price >= lo && w.end_price <= hi).flatMap((w) => {
    const cx = at(w.end), text = waveText(w)
    const label = text !== "" && !(cx - (spoke[w.dir] ?? -Infinity) < 56)
    if (label) spoke[w.dir] = cx
    return [{ w, cx, cy: y(w.end_price), label, text }]
  }) : []
  return (
    <svg viewBox={`0 0 ${W} 300`} role="img" aria-label={`分时图 ${days.length > 1 ? `${shortDate(first)}—${shortDate(end)}` : data.day ?? ""}`}
      className="block h-auto w-full text-[0.6875rem]">
      {levels.map((v) => (
        <g key={v}>
          <line x1={0} x2={PLOT} y1={y(v)} y2={y(v)} stroke="var(--border)" strokeWidth={0.6} />
          <text x={W - 4} y={y(v) + 3} textAnchor="end" fill="var(--subtle-foreground)" className="num">{fmtPrice(v, market)}</text>
        </g>
      ))}
      {days.length > 1 ? days.map((d, k) => (   // one day after another: a line between them, the date under each
        <g key={d}>
          {k > 0 && <line x1={x(k * data.total)} x2={x(k * data.total)} y1={0} y2={VOL_TOP + VOL} stroke="var(--border)" strokeWidth={0.8} />}
          <text x={x((k + 0.5) * data.total)} y={AXIS} textAnchor="middle" fill="var(--subtle-foreground)" className="num">{shortDate(d)}</text>
        </g>
      )) : ticks(data.sessions).map((t) => (
        <g key={t.i}>
          <line x1={x(t.i)} x2={x(t.i)} y1={0} y2={VOL_TOP + VOL} stroke="var(--border)" strokeWidth={0.6} strokeDasharray="2 3" />
          <text x={x(t.i)} y={AXIS} textAnchor={t.i === 0 ? "start" : t.i >= data.total ? "end" : "middle"} fill="var(--subtle-foreground)"
            className="num">{t.label}</text>
        </g>
      ))}
      {data.prev != null && (
        <g>
          <line x1={0} x2={PLOT} y1={y(data.prev)} y2={y(data.prev)} stroke="var(--muted-foreground)" strokeWidth={0.8} strokeDasharray="4 3" />
          <text x={4} y={y(data.prev) - 4} fill="var(--subtle-foreground)" className="num">昨收 {fmtPrice(data.prev, market)}</text>
        </g>
      )}
      {top != null && (
        <g>
          <line x1={0} x2={PLOT} y1={y(top)} y2={y(top)} stroke="var(--warning)" strokeWidth={1} strokeDasharray="5 3" />
          <text x={PLOT - 4} y={y(top) - 4} textAnchor="end" fill="var(--warning)" className="num">箱顶 {fmtPrice(top, market)}</text>
        </g>
      )}
      {m.map((p, k) => {
        const h = (p.volume / vmax) * VOL
        const up = p.close >= (k && m[k - 1].d === p.d ? m[k - 1].close : (p.d === days.length - 1 ? data.prev : null) ?? p.open)
        return <rect key={p.at} x={x(p.i)} y={VOL_TOP + VOL - h} width={bar} height={h} fill={up ? "var(--up)" : "var(--down)"} opacity={0.5} />
      })}
      {micro?.boxes.filter((b) => b.status === "established" && (b.end ?? "9999") >= first && b.start.slice(0, 10) <= end).map((b) => {
        const from = at(b.start)
        const to = b.end && b.end.slice(0, 10) <= end ? at(b.end) : x(last ? last.i : 0)
        if (b.top < lo || b.bottom > hi) return null
        const name = named.get(b.box_id)
        return (
          <g key={b.box_id}>
            <rect x={from} y={y(Math.min(b.top, hi))} width={Math.max(to - from, 1)} height={Math.max(y(Math.max(b.bottom, lo)) - y(Math.min(b.top, hi)), 1)}
              fill="var(--structure)" fillOpacity={0.06} stroke="var(--structure)" strokeWidth={0.8} data-testid="micro-box" />
            {name && <text x={from + 2} y={Math.max(y(Math.min(b.top, hi)) - 3, 9)} fill="var(--structure)" data-testid="micro-gold">{name}</text>}
          </g>
        )
      })}
      {breaks.map(({ e, cx, cy, up, label }) => (
        <g key={`${e.box_id}-${e.at}`} data-testid="micro-break">
          <title>{`${e.at.slice(5, 16).replace("T", " ")} ${up ? "上破" : "下破"}${e.ref_ratio != null ? ` · 量是箱沿附近最大量的 ${e.ref_ratio.toFixed(1)} 倍` : ""}${e.partial ? "（还没收完）" : ""}`}</title>
          <path d={up ? `M${cx - 4},${cy + 6} L${cx + 4},${cy + 6} L${cx},${cy} Z` : `M${cx - 4},${cy - 6} L${cx + 4},${cy - 6} L${cx},${cy} Z`}
            fill={up ? "var(--up)" : "var(--down)"} opacity={e.partial ? 0.5 : 1} />
          {label && <text x={cx + 6} y={up ? cy - 3 : cy + 11} fill="var(--muted-foreground)" className="num">
            {e.partial ? "量≈" : "量 "}{e.ref_ratio!.toFixed(1)}×</text>}
        </g>
      ))}
      {byDay.map((ps, k) => <polyline key={`v${k}`} points={path(ps, "vwap")} fill="none" stroke="var(--ma-1)" strokeWidth={1.2} />)}
      {byDay.map((ps, k) => <polyline key={`c${k}`} points={path(ps, "close")} fill="none" stroke="var(--foreground)" strokeWidth={1.4} />)}
      {marks.map(({ w, cx, cy, label, text }) => (
        <g key={`${w.dir}-${w.end}`} data-testid="wave">
          <title>{`${w.dir === "up" ? "上涨波" : "下跌波"} ${w.start.slice(11, 16)}—${w.end.slice(11, 16)}${w.peak_ratio != null
            ? ` · 最大一根是上一${w.dir === "up" ? "涨" : "跌"}波的 ${w.peak_ratio.toFixed(1)} 倍，整段 ${w.total_ratio?.toFixed(1) ?? "—"} 倍` : ""}${w.flag ? ` · ${w.flag}` : ""}`}</title>
          <circle cx={cx} cy={cy} r={2.2} fill={w.flag ? "var(--warning)" : "var(--muted-foreground)"} opacity={w.open ? 0.5 : 1} />
          {label && <text x={cx < 70 ? Math.max(cx - 4, 2) : cx > PLOT - 70 ? cx + 4 : cx}   // near an edge: grow inwards
            textAnchor={cx < 70 ? "start" : cx > PLOT - 70 ? "end" : "middle"}
            y={Math.min(Math.max(w.dir === "up" ? cy - 6 : cy + 13, 10), PRICE + 8)}
            className="num" fill={w.flag ? "var(--warning)" : "var(--subtle-foreground)"}>{text}</text>}
        </g>
      ))}
      {last && (
        <g>
          <rect x={PLOT + 2} y={y(last.close) - 8} width={RIGHT - 4} height={16} rx={2} fill="var(--foreground)" />
          <text x={W - 4} y={y(last.close) + 3.5} textAnchor="end" fill="var(--background)" className="num">{fmtPrice(last.close, market)}</text>
        </g>
      )}
    </svg>
  )
}
