// A stock's industry standing as the 市场 page shows it (redesign §6): its change that day, its rank among the ranked,
// whose numbers they are — for 选股's column and the K-line's chip.
import { fmtPct } from "@/lib/format"
import type { Standing, Standings } from "@/lib/market"

export function standingOf(st: Standings | undefined, level: "l1" | "l2", name: string | null | undefined): Standing | null {
  if (!st || !name) return null
  return st[level].find((r) => r.name === name) ?? null
}

/** "+6.58% · 今天第 1 / 127" (the K-line's chip) or "+6.58% · 第 1 / 127" (选股, whose header says the time); the change
 *  alone for an industry without a rank. */
export function standingText(s: Standing, of: number, dayWord = ""): string {
  return s.rank == null ? fmtPct(s.chg) : `${fmtPct(s.chg)} · ${dayWord}第 ${s.rank} / ${of}`
}

/** "今天" while the standings are live, else their day ("10-09"). */
export const standingDay = (st: Standings) => (st.mode === "live" || st.mode === "auction_result" ? "今天" : (st.day ?? "").slice(5))

/** 选股's header: "今天 10:42" / "10-09". */
export const standingLabel = (st: Standings) => (standingDay(st) === "今天" ? `今天 ${st.time ?? ""}`.trim() : standingDay(st))
