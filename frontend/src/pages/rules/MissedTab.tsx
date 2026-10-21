// 漏检排查 (spec §6.3): a stock that should have hit in some period, and why it did not — the deepest stage
// it reached on its closest day, the condition that failed and by how much, and, when that threshold is a
// parameter, the value that lets it through and what that does to the hits on the sample (37 → 112).
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ListingPicker } from "@/components/rules/ListingPicker"
import { inUnit, marketOf } from "@/lib/format"
import { type Missed, ruleEndpoints, type RuleRef } from "@/lib/rules"

const OPS: Record<string, string> = { ">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠" }
const num = (v: unknown) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")) : String(v ?? "—"))
const SAMPLE = 300

interface Props {
  ruleRef: RuleRef | null; listingId: string | null; onListing: (id: string) => void; seed: number; markets: string[]
  onOpen: (listingId: string, day: string) => void
}

export function MissedTab({ ruleRef, listingId, onListing, seed, markets, onOpen }: Props) {
  const today = new Date().toISOString().slice(0, 10)
  const [start, setStart] = useState(() => new Date(Date.now() - 90 * 86_400_000).toISOString().slice(0, 10))
  const [end, setEnd] = useState(today)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ for: string; data: Missed } | null>(null)
  const wrongMarket = !!listingId && !markets.includes(marketOf(listingId))
  const run = async () => {
    if (!ruleRef || !listingId) return
    setBusy(true)
    try {
      const n = Math.round(SAMPLE / Math.max(1, markets.length))
      const data = await ruleEndpoints.missed(ruleRef, listingId, start, end, { n, seed })
      setResult({ for: `${listingId}|${start}|${end}|${JSON.stringify(ruleRef)}`, data })
    } catch (e) {
      toast.error(`没查出来：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }
  const r = result?.for === `${listingId}|${start}|${end}|${JSON.stringify(ruleRef)}` ? result.data : null
  // the miss by its name and in its unit (a result without names: by ids, as it was)
  const key = r?.miss ? `${r.miss.stage}.${r.miss.id}` : ""
  const missName = r?.names?.conds[key] ?? key
  const unit = r?.names?.units[key] ?? ""
  const val = (v: unknown) => (unit && typeof v === "number" ? inUnit(v, unit) : num(v))
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <p className="max-w-2xl text-[0.875rem] text-muted-foreground">你觉得某只股票在某段时间应该被这条规则选出来、但它没有：说出是哪只、哪段时间，这里告诉你它走到了哪一步、差在哪个条件、差多少；差的那个数是参数时，还会算出放宽到多少能选出它，以及样本上的命中数会怎么变。</p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <ListingPicker value={listingId} onPick={onListing} />
        <Input type="date" aria-label="从" value={start} max={end} onChange={(e) => setStart(e.target.value)} className="h-8 w-[9.375rem]" />
        <span className="text-subtle-foreground">至</span>
        <Input type="date" aria-label="到" value={end} min={start} max={today} onChange={(e) => setEnd(e.target.value)} className="h-8 w-[9.375rem]" />
        <Button size="sm" disabled={!ruleRef || !listingId || wrongMarket || busy || !start || !end} onClick={() => void run()}>{busy ? "排查中…" : "排查"}</Button>
        {wrongMarket && <span className="text-[0.8125rem] text-warning">这条规则不适用于 {marketOf(listingId!)}</span>}
        {!ruleRef && <span className="text-[0.8125rem] text-error">规则有错误，先在「规则」里改好</span>}
      </div>
      {r && (
        <div className="mt-5 max-w-3xl space-y-2 text-[0.875rem]" data-testid="missed-result">
          {r.status === "hit" && (
            <div>这段时间其实命中了：{(r.days ?? []).map((d) => (
              <button key={d} type="button" className="num mr-1.5 rounded-md border border-border px-1.5 hover:bg-accent" onClick={() => onOpen(listingId!, d)}>{d}</button>
            ))}</div>
          )}
          {r.status === "no_bars" && <div className="text-warning">{r.note ?? "这段时间没有 K 线"}</div>}
          {r.status === "missed" && <>
            <div>最接近的一天是 <button type="button" className="num underline-offset-2 hover:underline" onClick={() => r.day && onOpen(listingId!, r.day)}>{r.day}</button>，走到了 <b>{r.deepest_stage ? r.names?.stages[r.deepest_stage] ?? r.deepest_stage : "—"}</b>。</div>
            {r.miss && (
              <div>差在 <b>{missName}</b>{r.miss.op && r.miss.threshold != null
                ? <>：现在 <span className="num">{val(r.miss.value)}</span>，要 {OPS[r.miss.op] ?? r.miss.op} <span className="num">{val(r.miss.threshold)}</span></>
                : "（不满足）"}
                <div className="num text-[0.8125rem] text-subtle-foreground">{r.miss.expr}</div></div>
            )}
            {r.param && (
              <div className="rounded-md border border-border px-3 py-2">
                把参数 <b>{r.param}</b> 放宽到 <span className="num">{val(r.relaxed)}</span> 就能选出它；样本上的命中会从 <span className="num">{r.hits_before ?? "—"}</span> 变成 <span className="num">{r.hits_after ?? "—"}</span>。
              </div>
            )}
            {r.note && <div className="text-muted-foreground">{r.note}</div>}
            {r.lines && r.lines.length > 0 && (
              <pre className="num overflow-x-auto rounded-md bg-muted/40 px-3 py-2 text-[0.8125rem] leading-5 whitespace-pre-wrap">{r.lines.join("\n")}</pre>
            )}
          </>}
        </div>
      )}
    </div>
  )
}
