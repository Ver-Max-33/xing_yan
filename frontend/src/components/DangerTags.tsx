import { TriangleAlert } from "lucide-react"
import { type Danger, dangerKey, dangerTitle } from "@/lib/danger"
import { cn } from "@/lib/utils"

/** 危险提醒's tags beside a stock's name (design 2026-10-15 §3): amber, as everything to watch; the sentences on hover.
 *  `compact` (a narrow list): the sign and how many. */
export function DangerTags({ ws, compact = false, className }: { ws: Danger[] | undefined; compact?: boolean; className?: string }) {
  if (!ws?.length) return null
  return (
    <span data-testid="danger-tags" title={dangerTitle(ws)}
      className={cn("inline-flex min-w-0 shrink-0 items-center gap-1 text-[0.6875rem] font-medium text-warning", className)}>
      <TriangleAlert className="size-3 shrink-0" aria-hidden />
      {compact ? <span className="num">{ws.length}</span>
        : ws.map((w) => <span key={dangerKey(w)} className="whitespace-nowrap rounded bg-warning-muted px-1">{w.label}</span>)}
    </span>
  )
}
