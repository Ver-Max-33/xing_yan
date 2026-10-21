// The difference between the current rule and the one AI proposes (spec §6.4): added lines green, removed amber.
import { cn } from "@/lib/utils"

export function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="num max-h-[16.25rem] overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 text-[0.75rem] leading-5" data-testid="rule-diff">
      {diff.split("\n").map((l, i) => (
        <div key={i} className={cn(l.startsWith("+") && !l.startsWith("+++") && "text-structure", l.startsWith("-") && !l.startsWith("---") && "text-warning")}>{l || " "}</div>
      ))}
    </pre>
  )
}
