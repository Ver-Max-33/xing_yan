// The 市场 page's words (designs 2026-10-12-market-live §5.3 and -market-page-redesign §3–§6): what the toolbar says the
// numbers are, why the page is not today's live board, today's read, a strength tag's two numbers, how deep a heat tile
// is shaded, and industry names as the page shows them. The same board always gives the same words; no AI.
import { fmtPct, MARKET_NAME } from "./format"
import type { BoardResponse, Industry, PhaseSummary, Read } from "./market"

/** The phase's days, today counted when the page is showing today (a trading day after the phase's last close). */
export function phaseDays(p: PhaseSummary, r: BoardResponse): number {
  return p.days + (r.mode !== "close" && r.session.day > p.as_of ? 1 : 0)
}

/** Whether the record should be fetching now (design §8.2): the session, the auction's result, and the closing
 *  windows (11:30–11:35, 15:00–15:05) while the last print comes in. */
const fetching = (r: BoardResponse) => r.session.state === "trading" || r.mode === "auction_result"
  || (r.session.state === "lunch" && r.session.time < "11:35:00") || (r.session.state === "after" && r.session.time < "15:05:00")

/** The toolbar's note on the board's age (§8.6): amber only while the record should be fetching and the board is over a
 *  minute old or its source failing; at lunch and after the close the time of the last price, plainly. */
export function freshness(r: BoardResponse, now: number): { text: string; late: boolean; title?: string } | null {
  if (!r.fetched_at || (r.mode !== "live" && r.mode !== "auction_result")) return null
  if (!fetching(r)) return { text: `行情到 ${r.fetched_at.slice(11, 16)}`, late: false }
  const age = Math.max(0, Math.round((now - Date.parse(r.fetched_at)) / 1000))
  const ago = age >= 60 ? `${Math.floor(age / 60)} 分钟` : `${age} 秒`
  if (r.error == null && age <= 60) return { text: `${ago}前更新`, late: false }
  return { text: `行情 ${ago}前${r.error ? `（${r.error}）` : ""}`, late: true, ...(r.error ? { title: r.error } : {}) }
}

const mmdd = (d: string | null | undefined) => (d ? d.slice(5, 10) : "")

/** "今天" while the page shows today's numbers (live, the auction's result); else the day they are of ("10-09"). */
export const dayWord = (r: Pick<BoardResponse, "mode" | "as_of">) => (r.mode === "live" || r.mode === "auction_result" ? "今天" : mmdd(r.as_of))

/** The toolbar (redesign §5): whose numbers these are, a dot while they are live (amber when late or failing), and how
 *  fresh — at lunch and after the close the words already say which minute they are. */
export function sessionNote(r: BoardResponse, now: number): { label: string; dot: "live" | "warn" | null; fresh: ReturnType<typeof freshness> } {
  const m = MARKET_NAME[r.market]
  if (!r.as_of) return { label: `${m} 还没有数据`, dot: null, fresh: null }   // no data in this market's profile: no date to name
  if (r.mode === "close") {
    const tail = r.market === "JP" ? "（东京）" : r.session.state === "pre" || r.session.state === "closed" ? " · 下一个交易日开盘后换成盘中的数" : ""
    return { label: `截至 ${mmdd(r.as_of)} 收盘${tail}`, dot: null, fresh: null }
  }
  if (r.mode === "auction") return { label: `集合竞价中 · 显示 ${mmdd(r.as_of)} 收盘`, dot: null, fresh: null }
  if (r.mode === "auction_result") return { label: `${m} 集合竞价结果 09:25`, dot: "live", fresh: freshness(r, now) }
  if (r.session.state === "lunch") return { label: `${m} 午休 · 11:30 的数`, dot: null, fresh: null }
  if (r.session.state === "after") return { label: `${m} 今天收盘 · 盘中记录`, dot: null, fresh: null }
  const fresh = freshness(r, now)
  return { label: `${m} ${r.session.label} ${r.board?.at.slice(11, 19) ?? r.session.time}`, dot: fresh?.late ? "warn" : "live", fresh }   // the numbers' own time
}

/** The line under the toolbar (redesign §5): the evening update failed (the days missing), the live source failing in
 *  the session (a close is shown instead), or why the board cannot be live — the backend's own sentence. A live
 *  board whose source fails says so in the toolbar's freshness, not twice. */
export function whyLine(r: BoardResponse): string | null {
  const n = r.board?.impossible ?? 0   // prices beyond today's limits the backend left out (10-13 09:25: twice the auction's)
  const beyond = n > 0 ? `行情里 ${n.toLocaleString("en-US")} 只股票的价格超出了今天的涨跌停，不可能是成交价，没有算进来` : null
  return [beyond, baseWhy(r)].filter(Boolean).join("；") || null
}

function baseWhy(r: BoardResponse): string | null {
  if (r.stale) return `日线只到 ${mmdd(r.as_of)}（收盘后的更新没成功）：今天的涨跌照常；晋级率、连板、阶段、60 日线，行业的近 5 日、近 20 日和标签不知道`
  if (r.mode !== "close") return null
  if (r.error) return r.session.state === "trading" || r.session.state === "lunch" ? `盘中行情取不到（${r.error}），下面是 ${mmdd(r.as_of)} 收盘` : null
  return r.why_not
}

/** The 成交额 cell's line (redesign §2.2): in the session the day before at this minute (before 10:30, the volume clock
 *  is biased) or the whole day projected; the auction's matched turnover beside the day before's whole day; a day that
 *  is over (after the close, a stored day) against the day before. */
export function amountLine(r: BoardResponse, money: (v: number | null | undefined) => string): string {
  const b = r.board!, before = r.amount_before
  if (r.mode === "auction_result") return `集合竞价成交 · 前一天全天 ${money(before)}`
  if (r.mode === "live" && r.session.state !== "after") {
    const early = r.session.time < "10:30:00" || b.projected == null
    return early ? `前一天这时 ${money(r.yesterday?.amount)} · 全天 ${money(before)}` : `全天约 ${money(b.projected)} · 前一天 ${money(before)}`
  }
  const vs = before ? b.amount / before - 1 : null
  return `前一天 ${money(before)}${vs == null ? "" : ` · ${vs >= 0 ? "多" : "少"} ${Math.round(Math.abs(vs) * 100)}%`}`
}

/** The 情绪 row's note (redesign §2.3): today against yesterday at this minute in the session (from 9:45), the auction's
 *  result as such, else whose close the six numbers are. */
export function moodNote(r: BoardResponse): string | null {
  if (r.mode === "auction_result") return "集合竞价的结果（9:30 开盘后换成盘中的数）"
  if (r.mode === "live") return r.read ? readText(r.read) : r.session.time < "09:45:00" ? "9:45 以后和昨天比" : null
  return `${(r.as_of ?? "").slice(5)} 收盘的数`
}

export function readText(read: Read | null): string | null {
  if (!read) return null
  if (read.label === "持平") return "和昨天同一时间差不多"
  const items = read.label === "偏弱" ? read.worse : read.better
  return `今天${read.label}：${items.join("、")}（和昨天同一时间比）`
}

/** A 二级 name as the page shows it: without the trailing Ⅱ 申万 adds where a 一级 or 三级 has the same name. */
export const industryName = (name: string) => name.replace(/Ⅱ$/, "")

/** A stock's industry, always 一级 · 二级 (日股: its 33 業種); null without one. */
export function stockIndustry(l1: string | null | undefined, l2: string | null | undefined): string | null {
  const parts = [l1, l2 ? industryName(l2) : null].filter(Boolean)
  return parts.length ? parts.join(" · ") : null
}

/** The tag's two numbers (redesign §2.4.3): "近 5 日比大盘 +3.3%，之前 15 日 +1.4%"; without a tag, that the last 5 bars
 *  were like the market's. */
export function tagSentence(i: Pick<Industry, "tag" | "rel5" | "prior15">): string | null {
  if (i.rel5 == null) return null
  if (!i.tag) return `近 5 日和大盘差不多（${fmtPct(i.rel5, 1)}）`
  return `近 5 日比大盘 ${fmtPct(i.rel5, 1)}，之前 15 日 ${fmtPct(i.prior15, 1)}`
}

export type HeatStep = -4 | -3 | -2 | -1 | 0 | 1 | 2 | 3 | 4
/** A heat tile's shade (redesign §4): the largest move shown is the full shade, four steps a side; what shows as 0.00% is
 *  grey. */
export function heatStep(v: number | null | undefined, scale: number): HeatStep | null {
  if (v == null || !Number.isFinite(v)) return null
  if (Math.abs(v) < 0.00005 || !(scale > 0)) return 0
  return (Math.sign(v) * Math.min(4, Math.ceil((4 * Math.abs(v)) / scale - 1e-9))) as HeatStep
}
