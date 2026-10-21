// A stock's industry on the K-line page (redesign §6.2): "电力设备 · 电池 −1.45% · 今天第 87 / 127 [一直强]" — the 二级's
// standing as the 市场 page shows it now (日股: its 33 業種); clicking opens that industry in the 市场 page's ranking.
// Nothing without an industry (a listing not yet classified).
import { useNavigate } from "react-router"
import type { Market } from "@/lib/api"
import { changeClass, fmtPct } from "@/lib/format"
import { useIndustries } from "@/lib/market"
import { stockIndustry, tagSentence } from "@/lib/marketWords"
import { cn } from "@/lib/utils"
import { tagTone } from "./order"
import { standingDay, standingOf, standingText } from "./standing"

export function IndustryChip({ market, l1, l2 }: { market: Market; l1: string | null | undefined; l2: string | null | undefined }) {
  const navigate = useNavigate()
  const { data: st } = useIndustries(market, !!l1)
  const words = stockIndustry(l1, l2)
  if (!words || !l1) return null
  const level: "l1" | "l2" = l2 ? "l2" : "l1"
  const name = l2 ?? l1
  const s = standingOf(st, level, name)
  const day = st ? standingDay(st) : ""
  const title = s && st ? `${name}（${market === "CN" ? (level === "l2" ? "申万二级" : "申万一级") : "33 業種"}）${day === "今天" ? "今天" : day}涨跌中位数 ${standingText(s, st.of[level])}；${tagSentence(s) ?? ""}${st.time ? `；${st.time} 的数` : ""}` : words
  return (
    <button type="button" data-testid="kline-industry" title={title} onClick={() => navigate(`/market?level=${level}&industry=${encodeURIComponent(name)}`)}
      className="no-drag flex h-7 max-w-[24rem] min-w-0 items-center gap-1.5 rounded-md bg-accent px-2 text-[0.8125rem] hover:bg-accent/70">
      <span className="truncate">{words}</span>
      {s && st && <span className={cn("num shrink-0", changeClass(s.chg))}>{fmtPct(s.chg)}</span>}
      {s && st && s.rank != null && <span className="num shrink-0 text-[0.75rem] text-muted-foreground">{day}第 {s.rank} / {st.of[level]}</span>}
      {s?.tag && <span className={cn("shrink-0 rounded px-1 text-[0.6875rem] font-medium", tagTone(s.tag))}>{s.tag}</span>}
    </button>
  )
}
