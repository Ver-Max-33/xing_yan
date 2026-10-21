// The explanation of one listing on one day (spec §5.6): every number comes from the matcher's result;
// the backend renders the sentences, this card only lays them out.
import { Badge } from "@/components/ui/badge"
import type { PreviewDetail } from "@/lib/rules"
import { cn } from "@/lib/utils"

const STATUS: Record<string, [string, string]> = {
  signal: ["信号", "border-up/50 text-up"], watch: ["潜伏", "border-structure/50 text-structure"],
  unknown: ["无法判断", "border-warning/50 text-warning"], fail: ["未通过", "border-border text-muted-foreground"],
}

export function DetailCard({ data, className }: { data: PreviewDetail; className?: string }) {
  const { detail, lines } = data
  const [label, tone] = STATUS[detail.status ?? ""] ?? ["不在股票池", "border-border text-subtle-foreground"]
  return (
    <div className={cn("space-y-1.5 text-[0.8125rem]", className)} data-testid="detail-card">
      <div className="flex items-center gap-2">
        <span className="num text-muted-foreground">{detail.date}</span>
        <Badge variant="outline" className={cn("h-5 px-1.5 text-[0.75rem] font-normal", tone)}>{label}</Badge>
      </div>
      {lines.slice(1).map((l, k) => (
        l.trimStart().startsWith("公式：")   // the formula as written, under its condition: small and quiet
          ? <div key={k} className="num pl-4 text-[0.75rem] leading-4 text-subtle-foreground">{l.trim()}</div>
          : <div key={k} className={cn("leading-5", l.trimStart().startsWith("✗") && "text-down", l.trimStart().startsWith("?") && "text-warning",
            l.trimStart().startsWith("差在") && "text-foreground", l.includes("无法判断的原因") && "text-warning")}>{l.trim()}</div>
      ))}
    </div>
  )
}
