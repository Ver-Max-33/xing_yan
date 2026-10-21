import { type DangerAnswer, dangerKey, dangerNotes } from "@/lib/danger"

const ago = (kind: string, days: number) => kind === "unlock"
  ? (days > 0 ? `还有 ${days} 个交易日` : days === 0 ? "今天解禁" : "推算的日子已过")
  : (days === 0 ? "今天" : `${days} 个交易日前`)

/** 危险提醒 in the K-line panel (design 2026-10-15 §3; plain words 10-17): the stock's warnings on the chart's day — each its
 *  name (the risk), 原因 (this stock's numbers) and 可能的风险 (what followed in ten years). Nothing when there is nothing to say. */
export function DangerBlock({ listingId, answer }: { listingId: string; answer: DangerAnswer | undefined }) {
  if (!answer) return null
  const why = !answer.covered ? answer.why : answer.uncovered[listingId]
  if (why) return <div data-testid="danger-block" className="border-t border-border px-3 py-1.5 text-[0.75rem] text-subtle-foreground">{why}</div>
  const ws = answer.warnings[listingId] ?? []
  const notes = dangerNotes(answer, listingId)
  if (!ws.length && !notes.length) return null
  return (
    <div data-testid="danger-block" className="max-h-[36%] overflow-y-auto border-t border-border px-3 py-2">
      <div className="pb-1 text-[0.75rem] text-subtle-foreground">危险提醒</div>
      {ws.map((w) => (
        <div key={dangerKey(w)} className="pb-1.5 text-[0.8125rem]">
          <div className="flex items-baseline gap-2">
            <span className="font-medium text-warning">{w.label}</span>
            <span className="num text-[0.75rem] text-subtle-foreground">{ago(w.kind, w.days)}</span>
          </div>
          <div className="text-muted-foreground"><span className="text-subtle-foreground">原因：</span>{w.reason}</div>
          <div className="text-muted-foreground"><span className="text-subtle-foreground">可能的风险：</span>{w.risk}</div>
        </div>
      ))}
      {ws.length > 0 && <div className="text-[0.75rem] text-subtle-foreground">这些是过去十年的统计：多数时候是这样，不是每只都会这样。</div>}
      {notes.map((n) => <div key={n} className="text-[0.75rem] text-subtle-foreground">{n}</div>)}
    </div>
  )
}
