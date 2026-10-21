// 盯盘 (spec §8.5, §4.8): each market's session and the stocks you follow — your watch groups and the 潜伏 lists of
// the enabled rules' latest daily scans — now: intraday while the market trades, the latest close otherwise. The
// server's feed (SSE) carries every poll; a stock's minutes of the day draw its 分时图. Nothing here is a signal.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { desktop } from "@/app/platform"
import { api, type Market } from "./api"
import { fmtPct } from "./format"
import type { Box, Gold, Structures } from "./rules"
import { STATUSES, type WatchStatus } from "./watch"

export type SessionState = "pre" | "trading" | "lunch" | "after" | "closed"
export interface Session { state: SessionState; label: string; day: string; elapsed: number; total: number }
// a rule met on today's bar, a price beyond the day's limits (ex-rights: not judged); a line's 竞价、逼近、触线 and 持有中's
// reminder before the close, the market's fall, the feed stopping (design 2026-10-10); box and line: before that design
export type AlertKind = "rule" | "ex" | "auction" | "near" | "touch" | "hold" | "market" | "feed" | "box" | "line"
export type AlertStatus = "pending" | "confirmed" | "revoked" | "none" // a rule alert waits for the close; a price alert does not
export interface LiveRow {
  listing_id: string; name: string | null; rule: string | null; rule_id: string | null; watched: boolean; stage: string | null
  status: WatchStatus | null // its 观察条目's
  top: number | null; line: number | null // the box top and its break line (a 潜伏 rule's, or a stock 观察中's own box)
  last: number | null; prev: number | null; chg: number | null; vol_ratio: number | null
  to_top: number | null; live: boolean
  vwap: number | null     // the day's average price (the 分时图's yellow line)
  vwap_gap: number | null // the price above it, in points of the previous close (5 points and more: a warning)
  ex_rights: boolean // beyond the day's limits: an ex-rights day the factor has not caught, not judged today
  alert: { kind: AlertKind; text: string; status: AlertStatus; at: string } | null // the day's latest
}
export interface LiveAlert {
  id: number; day: string; market: Market; listing_id: string; kind: AlertKind; key: string; at: string; price: number | null
  title: string; text: string; status: AlertStatus; resolved_at: string | null
  detail: AlertDetail | null
}
/** What an alert saw: a rule alert's explanation (lines); a line's kind (box / fail / line: you drew it), side (up: the
 *  buying side) and price, whether Feishu heard it (notify), and the moment's facts. */
export interface AlertDetail {
  lines?: string[]; line?: number; kind?: "box" | "fail" | "line"; side?: "up" | "down"; notify?: boolean; how?: "near" | "beyond" | null
  move2?: number | null; vol3?: number | null; vol_ratio?: number | null; vwap_gap?: number | null; to_limit?: number | null
  tags?: string[]; history?: string | null
}

/** The moment's facts, as Feishu hears them (backend realtime/alerts.py _facts). */
export function factsOf(d: AlertDetail | null): string[] {
  if (!d) return []
  const out: string[] = []
  if (d.move2 != null) out.push(`2 分钟 ${fmtPct(d.move2, 1)}`)
  if (d.vol3 != null) out.push(`近 3 分钟量 ${d.vol3.toFixed(1)} 倍`)
  if (d.vol_ratio != null) out.push(`量比 ${d.vol_ratio.toFixed(1)}`)
  if (d.vwap_gap != null) out.push(`均价线${d.vwap_gap >= 0 ? "上" : "下"} ${(Math.abs(d.vwap_gap) * 100).toFixed(1)} 点`)
  if (d.to_limit != null) out.push(`离${d.side === "up" ? "涨停" : "跌停"} ${(d.to_limit * 100).toFixed(1)} 点`)
  return [...out, ...(d.tags ?? []), ...(d.history ? [d.history] : [])]
}
export interface LiveIndex { listing_id: string; last: number | null; chg: number | null; live: boolean }
export interface LiveMarket {
  type: "quotes"; market: Market; at: string; session: Session
  // approx_volume: its minutes' volume is only a sketch; approx_minutes: minutes put together from the day-so-far snapshots
  source: { name: string; delay: number; approx_volume: boolean; approx_minutes: boolean } | null
  error: string | null // the source failed (no permission, not logged in, cut off): 行情中断, and the rows are the close
  quote_at: string | null // the whole-market quote's time (the K-line's forming bar and 观察 read it), beside the poll's `at`
  rows: LiveRow[]; indexes: LiveIndex[]
}
// i: the place on the clock across the days shown (the day's position d × total + its minute, lunch left out)
export interface Minute { at: string; i: number; d: number; open: number; high: number; low: number; close: number; volume: number; vwap: number }
export interface Minutes {
  listing_id: string; day: string | null; days: string[]; prev: number | null
  total: number; sessions: [string, string][]; minutes: Minute[]
}
export type MinuteDays = 1 | 5 | 10 | 20 // the last days side by side (通达信 shows up to 20)
export type LiveTab = "watched" | "latent"   // 自选: what 盯盘 follows (yours, 10-12); 潜伏: the others, by the whole-market quote
/** A 潜伏 stock you do not watch, by the whole-market quote (seen, never judged): /api/live/{market}/latent */
export interface LatentRow {
  listing_id: string; name: string | null; rule: string | null; rule_id: string | null; stage: string | null
  top: number | null; line: number | null; last: number | null; chg: number | null; to_top: number | null; at: string | null
}
export interface LatentList { market: Market; session: Session; rows: LatentRow[] }
export type Row = LiveRow & { market: Market }

// ---- 分时小箱 (spec §8.5): the BOX primitive on 1, 5 and 15-minute bars of the kept days ----
export type MicroPeriod = 1 | 5 | 15
export interface MicroBar { at: string; open: number; high: number; low: number; close: number; volume: number; partial: boolean }
export interface MicroBox {
  box_id: string; scale: "short" | "mid" | "long"; status: string; start: string; known_at: string; end: string | null
  ended_at: string | null; end_reason: string | null; top: number; bottom: number; shape: string | null
}
export interface MicroEvent {
  box_id: string; at: string; event: string; value: number | null
  ref_ratio: number | null // a break's volume over the largest near that edge in the box (相关箱沿附近、同周期的参考柱)
  partial: boolean         // on the bar still forming: a process value
  known_at: string
}
// a wave of the zigzag (a research definition): its volume against the previous wave of its direction — the largest bar
// (peak_ratio, the bar heights compared) and the whole wave's (total_ratio); open: still going
export interface MicroWave {
  start: string; end: string; dir: "up" | "down"; start_price: number; end_price: number; volume: number; peak: number
  total_ratio: number | null; peak_ratio: number | null; new_extreme: boolean | null
  flag: "新高量不足" | "未创新高" | "不创新低、量缩" | "下跌放量" | null; open: boolean
}
// 分时金品: a bigger box's last small box (superseded_at: when a later small box in it was confirmed), as on the daily chart
export interface MicroGold {
  kind: "天品" | "上品" | "下品"; variant: boolean; big_box_id: string; small_box_id: string; known_at: string; superseded_at: string | null
  inner_distance: number | null; above_top: number | null
}
export interface Micro {
  listing_id: string; period: MicroPeriod; bars: MicroBar[]; boxes: MicroBox[]; events: MicroEvent[]; waves: MicroWave[]; gold?: MicroGold[]
}

export const useMicro = (listingId: string | undefined, period: MicroPeriod | null) =>
  useQuery({
    queryKey: ["live", "minutes", listingId?.split(":")[0], listingId, "micro", period], enabled: !!listingId && period !== null,
    queryFn: () => api<Micro>(`/live/${listingId}/micro?period=${period}`),
  })

/** Where a time (exchange-local ISO) falls on the trading day's clock, lunch left out (the 分时图's x axis). */
export function minuteOf(at: string, sessions: [string, string][]): number {
  const m = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5))
  const t = m(at.slice(11, 16))
  let base = 0
  for (const [a, b] of sessions) {
    if (t <= m(b)) return base + Math.max(0, t - m(a))
    base += m(b) - m(a)
  }
  return base
}

/** Where a time falls across the days shown side by side: its day's place × `total` + its minute; before the first day
 *  the left edge, after the last the right one, a day not kept the start of the next one kept. */
export function placeOf(at: string, days: string[], sessions: [string, string][], total: number): number {
  const day = at.slice(0, 10)
  const k = days.indexOf(day)
  if (k >= 0) return k * total + minuteOf(at, sessions)
  return days.filter((d) => d < day).length * total
}

/** 向左找位置: the nearest confirmed box edge above the price (阻力位) and below it (支撑位) — a top below the
 *  price is support now (顶底互换). */
export function nearestLevels(boxes: MicroBox[], last: number): { above: number | null; below: number | null } {
  const levels = boxes.filter((b) => b.status === "established").flatMap((b) => [b.top, b.bottom])
  const up = levels.filter((v) => v > last), down = levels.filter((v) => v < last)
  return { above: up.length ? Math.min(...up) : null, below: down.length ? Math.max(...down) : null }
}

/** The price against the day's average — above is the uptrend's order; 5 points and more above it is far, and when
 *  it is still so at the close (`late`: the 尾盘) the next day tends to open lower — a rule of thumb, not tested. */
export function vwapNote(gap: number | null | undefined, late = false): { text: string; warn: boolean } | null {
  if (gap == null) return null
  const text = `白线在均价线${gap >= 0 ? "上方" : "下方"} ${(Math.abs(gap) * 100).toFixed(1)} 个点`
  return gap >= 0.05 ? { text: `${text}：远离均价线${late ? "，尾盘还这样，次日容易回落" : ""}（经验说法，没有经过检验）`, warn: true }
    : { text, warn: false }
}

/** The session's last 30 minutes, or after its close: when the 尾盘 warning applies. */
export const lateInDay = (s: Session) => s.state === "after" || (s.state === "trading" && s.total - s.elapsed <= 30)

/** 分时金品: each small box that is a bigger box's last, named by where it sits (one small box can end two
 *  bigger boxes: both names). */
export function goldLabels(gold: MicroGold[] = []): Map<string, string> {
  const out = new Map<string, string>()
  for (const g of gold.filter((x) => !x.superseded_at)) {
    const name = `${g.variant ? "变异" : ""}${g.kind}`, had = out.get(g.small_box_id)
    out.set(g.small_box_id, had && !had.split("/").includes(name) ? `${had}/${name}` : name)
  }
  return out
}

/** A wave's label: its largest bar against the previous wave's of the same direction, and what that says. */
export function waveText(w: MicroWave): string {
  if (w.peak_ratio == null) return ""
  const base = `量${w.open ? "≈" : ""} ${w.peak_ratio.toFixed(1)}×`
  return w.open ? `${base}（还在走）` : w.flag ? `${base} · ${w.flag}` : base
}

export interface Resonance { macro: string | null; meso: string | null; micro: string | null; label: "三箱共振" | "两箱共振" | null }

/** 宏观 (the big box, 3–6 months), 中观 (its 金品), 微观 (today's small box breaking up in the minutes): all three are
 * 三箱共振; with only a short flat box (平箱) two are enough — 不硬凑大箱. */
export function resonance(s: Structures | undefined, micro: MicroEvent[] | undefined, period: MicroPeriod, day: string): Resonance {
  // the structures come scale by scale (short, mid, long), each in time order: "latest" is by time, not by place
  const later = <T extends { known_at: string }>(a: T | undefined, b: T) => (!a || b.known_at > a.known_at ? b : a)
  const boxes = (s?.boxes ?? []).filter((b) => b.status === "established")
  const big = boxes.filter((b) => b.scale === "long" && b.end === null).reduce<Box | undefined>(later, undefined)
  // its 金品 is the one the K-line names: not superseded by a later small box (a tie on known_at is common)
  const gold = big ? (s?.gold ?? []).filter((g) => g.big_box_id === big.id && !g.superseded_at).reduce<Gold | undefined>(later, undefined) : undefined
  // the day's break on the most volume against its box's edge (当天放量的小箱体); the later one when they tie
  const up = (micro ?? []).filter((e) => e.event === "break_up" && e.at.slice(0, 10) === day)
    .sort((a, b) => (a.ref_ratio ?? -1) - (b.ref_ratio ?? -1) || a.at.localeCompare(b.at)).at(-1)
  const latest = boxes.reduce<Box | undefined>((a, b) => (!a || b.start > a.start || (b.start === a.start && b.known_at > a.known_at) ? b : a), undefined)
  const label = big && gold && up ? "三箱共振" : !big && up && latest?.scale === "short" && latest.shape === "平箱" ? "两箱共振" : null
  return {
    macro: big ? `宏观 · 大箱（自 ${big.start.slice(5)}）` : null,
    meso: gold ? `中观 · 金品（${gold.kind}${gold.variant ? "，变异" : ""}）` : null,
    micro: up ? `微观 · ${period} 分钟小箱上破${up.ref_ratio != null ? ` · 量${up.partial ? "≈" : ""} ${up.ref_ratio.toFixed(1)} 倍` : ""}` : null,
    label,
  }
}

const PLACE: Record<Market, string> = { JP: "东京", CN: "沪深" }

/** A market's session as the header says it: 行情中断 when its source failed (spec §15.1: never stale prices as now). */
export const sessionText = (m: LiveMarket) => ({ text: `${PLACE[m.market]} ${m.error ? "行情中断" : m.session.label}`, broken: !!m.error })

/** Keep the computer from idling to sleep while a market trades or is at lunch (spec §3.3; a closed lid still sleeps). */
/** The minute a market's prices are as of while its day is open (no 行情中断), else null: what the K-line's forming bar follows. */
export const liveAt = (markets: LiveMarket[] | undefined, market: Market | null): string | null => {
  const m = markets?.find((x) => x.market === market)
  return m && m.session.state !== "closed" && !m.error && m.source ? m.at : null
}

/** The structures with today's bar counted, asked again at each poll: liveAt — and also through a 行情中断, when the chart
 *  still shows the forming bar of the last good poll and the structures must count that same bar (review 10-13: they fell
 *  back to the day before's, a box running on through today's bar). */
export const liveDay = (markets: LiveMarket[] | undefined, market: Market | null): string | null => {
  const m = markets?.find((x) => x.market === market)
  return m && m.session.state !== "closed" && (m.source || m.error) ? m.at : null   // down, the state names no source
}

const ZONE: Record<Market, [string, string]> = { CN: ["Asia/Shanghai", "+08:00"], JP: ["Asia/Tokyo", "+09:00"] }
const OPENS: Record<Market, string[]> = { CN: ["09:30", "13:00"], JP: ["09:00", "12:30"] }   // each half's first minute
export const LATE_AFTER = 180   // seconds: a live price older than this while its market trades is said (the 10-12 rule)

/** How old a live price is while its market trades, and whether it is late (not updated for 3 minutes, the 10-12 rule);
 *  null outside the trading minutes (the auction, lunch, after the close) and in each half's first 3 minutes (its first
 *  fetch may not be in yet). `at`: the exchange's clock, with an offset or without one. */
export type PriceAge = { seconds: number; late: boolean }
export function priceAge(at: string | null | undefined, market: Market, s: Session, now: number): PriceAge | null {
  if (!at || s.state !== "trading") return null
  const [h, m] = new Intl.DateTimeFormat("en-GB", { timeZone: ZONE[market][0], hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .format(now).split(":").map(Number)
  const minute = h * 60 + m
  if (OPENS[market].some((o) => { const [oh, om] = o.split(":").map(Number); return minute >= oh * 60 + om && minute < oh * 60 + om + 3 })) return null
  const seconds = Math.max(0, Math.round((now - Date.parse(/(Z|[+-]\d\d:\d\d)$/.test(at) ? at : at + ZONE[market][1])) / 1000))
  return { seconds, late: seconds > LATE_AFTER }
}
export const lateText = (age: PriceAge) => `行情 ${Math.floor(age.seconds / 60)} 分钟没更新`

/** The older of a market's two feeds while it trades — 盯盘's poll and the whole-market quote (the K-line's forming bar
 *  and 观察 read it; `boardAt`, from the feed, is fresher than the state's) — so either stopping is said. */
export function feedAge(m: LiveMarket, boardAt: string | null | undefined, now: number): PriceAge | null {
  const quote = [m.quote_at, boardAt].filter((x): x is string => !!x).sort().at(-1)   // both the exchange's clock, no offset
  const ages = [priceAge(m.at, m.market, m.session, now), priceAge(quote, m.market, m.session, now)].filter((a): a is PriceAge => a != null)
  return ages.length ? ages.reduce((a, b) => (b.seconds > a.seconds ? b : a)) : null
}

/** Now, again every `ms`: a price's age is measured against it while nothing new comes (a stalled feed is still said). */
export function useNow(ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), ms); return () => window.clearInterval(id) }, [ms])
  return now
}

export const awake = (markets: LiveMarket[]) => markets.some((m) => m.session.state === "trading" || m.session.state === "lunch")

/** The markets as the feed last said them: an event replaces its market's state. */
export function merge(old: LiveMarket[], events: LiveMarket[]): LiveMarket[] {
  const by = new Map(old.map((m) => [m.market, m]))
  for (const e of events) by.set(e.market, e)
  return [...by.values()]
}

/** A tab's rows, both markets, the ones nearest their trigger first: furthest over the box top, then the closest
 * under it; rows without a top (yours only) last, the day's bigger moves first. */
const byTrigger = (a: Row, b: Row) => Number(a.to_top == null) - Number(b.to_top == null) || (b.to_top ?? 0) - (a.to_top ?? 0)
      || Math.abs(b.chg ?? 0) - Math.abs(a.chg ?? 0)

/** What 盯盘 follows, both markets: furthest over the box top, then the closest under it; rows without a top last, the
 *  day's bigger moves first. */
export const rowsOf = (markets: LiveMarket[]): Row[] => markets.flatMap((m) => m.rows.map((r) => ({ ...r, market: m.market }))).sort(byTrigger)

/** The 潜伏 lists as rows of the same table: by the whole-market quote, no volume ratio, never an alert — the nearest their
 *  box top first, either side (a breakout to watch for; one long past its top, a run the scan still calls 潜伏, last). */
export const latentOf = (lists: (LatentList | undefined)[]): Row[] => lists.flatMap((l) => (l?.rows ?? []).map((r): Row => ({
  ...r, market: l!.market, watched: false, status: null, prev: null, vol_ratio: null, live: r.at != null, vwap: null, vwap_gap: null,
  ex_rights: false, alert: null,
}))).sort((a, b) => Number(a.to_top == null) - Number(b.to_top == null) || Math.abs(a.to_top ?? 0) - Math.abs(b.to_top ?? 0))

/** The 状态 column: an ex-rights day, then the day's latest alert, then over or near the box top while the market
 * trades, otherwise what the daily scan said. */
export function statusOf(r: Row): { text: string; tone: "warn" | "fg" | "muted" } {
  if (r.ex_rights) return { text: "疑似除权，今日不判断", tone: "muted" }
  const a = r.alert
  if (a?.kind === "rule") {
    return a.status === "pending" ? { text: "盘中满足 · 待收盘确认", tone: "warn" }
      : a.status === "confirmed" ? { text: "收盘确认", tone: "fg" } : { text: "收盘撤销", tone: "muted" }
  }
  if (a?.kind === "box") return { text: `${a.at.slice(11, 16)} 越过上沿`, tone: "warn" }
  if (a?.kind === "line") return { text: a.text, tone: "fg" }
  const layer = { auction: "开盘到线", near: "逼近线 · 注意看", touch: "已到线", hold: "收盘前仍在失效线下" } as Partial<Record<AlertKind, string>>
  if (a && layer[a.kind]) return { text: `${a.at.slice(11, 16)} ${layer[a.kind]}`, tone: "warn" }
  if (r.live && r.to_top != null && r.to_top >= 0) return { text: "越过上沿 · 待收盘确认", tone: "warn" }
  if (r.live && r.to_top != null && r.to_top > -0.01) return { text: "接近上沿", tone: "fg" }
  if (r.rule) return { text: r.stage ? `${r.rule} · 到 ${r.stage}` : r.rule, tone: "muted" }
  return { text: STATUSES.find(([k]) => k === r.status)?.[1] ?? "自选", tone: "muted" }
}

/** The 分时图's x axis, in minutes into the trading day: each open and close, whole hours at least 45 minutes clear
 * of them, and where lunch joins two sessions one mark for both ends ("11:30/12:30"). */
export function ticks(sessions: [string, string][]): { i: number; label: string }[] {
  const min = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5))
  const short = (t: string) => t.replace(/^0/, "")
  const out: { i: number; label: string }[] = []
  let base = 0
  sessions.forEach(([a, b], k) => {
    const start = min(a), end = min(b)
    if (k === 0) out.push({ i: 0, label: short(a) })
    else out[out.length - 1].label += `/${short(a)}`
    for (let h = Math.ceil(start / 60) * 60; h < end; h += 60) {
      if (h - start >= 45 && end - h >= 45) out.push({ i: base + h - start, label: `${h / 60}:00` })
    }
    base += end - start
    out.push({ i: base, label: short(b) })
  })
  return out
}

const NOW = ["live", "now"]

/** The server's feed (SSE), for the whole app: each market's state as it changes, and each alert as a toast on any page
 * (design 14: Toast + 飞书) with a way to the stock's chart. */
export function useLiveFeed() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  useEffect(() => {
    const es = new EventSource("/api/live/stream")
    const put = (events: LiveMarket[]) =>
      qc.setQueryData<{ markets: LiveMarket[] }>(NOW, (old) => ({ markets: merge(old?.markets ?? [], events) }))
    es.addEventListener("hello", (e) => put((JSON.parse(e.data) as { markets: LiveMarket[] }).markets))
    es.addEventListener("quotes", (e) => {
      const ev = JSON.parse(e.data) as LiveMarket
      put([ev])
      void qc.invalidateQueries({ queryKey: ["live", "minutes", ev.market] })
    })
    // the market board fetched (design 2026-10-12 §8.2): the 市场 page asks again, a K-line's forming bar follows, and
    // so do 观察's and 最近's prices (the same whole-market quote: they stood at the day before's close while the chart moved)
    es.addEventListener("board", (e) => {
      const ev = JSON.parse(e.data) as { market: Market; at: string }
      qc.setQueryData(["market", "boardAt", ev.market], ev.at)
      void qc.invalidateQueries({ queryKey: ["market", "board", ev.market] })
      void qc.invalidateQueries({ queryKey: ["watchlist"] })
      void qc.invalidateQueries({ queryKey: ["recent"] })
      void qc.invalidateQueries({ queryKey: ["live", "latent", ev.market] })
    })
    es.addEventListener("alert", (e) => {
      void qc.invalidateQueries({ queryKey: ["live", "alerts"] })
      for (const a of (JSON.parse(e.data) as { alerts: LiveAlert[] }).alerts) {
        toast(a.title, {
          description: `${a.at.slice(11, 16)} · ${a.text}${a.status === "pending" ? " · 未确认" : ""}`, duration: 15_000,
          action: { label: "打开 K 线", onClick: () => navigate(`/k/${a.listing_id}`) },
        })
      }
    })
    return () => es.close()
  }, [qc, navigate])
  const { data } = useLive()
  const on = awake(data?.markets ?? [])
  useEffect(() => { desktop.setKeepAwake(on) }, [on])
}

/** Each market now (the feed keeps it current); asked again every minute in case the feed dropped. */
export const useLatent = (market: Market) =>   // asked again at each whole-market fetch (the feed's board), else each minute
  useQuery({ queryKey: ["live", "latent", market], queryFn: () => api<LatentList>(`/live/${market}/latent`), refetchInterval: 60_000 })
export const useLive = () =>
  useQuery({ queryKey: NOW, queryFn: () => api<{ markets: LiveMarket[] }>("/live"), refetchInterval: 60_000 })

/** The day's alerts of both markets (预警中心) and the stocks muted for the day. */
export const useAlerts = () =>
  useQuery({ queryKey: ["live", "alerts"], queryFn: () => api<{ alerts: LiveAlert[]; muted: string[] }>("/live/alerts"), refetchInterval: 60_000 })

/** 今天不再提醒: no alert on this stock for the rest of its market's day. */
export function useMute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (listingId: string) => api("/live/mute", { method: "POST", body: JSON.stringify({ listing_id: listingId }) }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["live"] }),
  })
}

export const useMinutes = (listingId: string | undefined, days: MinuteDays = 1) =>
  useQuery({
    queryKey: ["live", "minutes", listingId?.split(":")[0], listingId, days], enabled: !!listingId,
    queryFn: () => api<Minutes>(`/live/${listingId}/minutes?days=${days}`),
  })
