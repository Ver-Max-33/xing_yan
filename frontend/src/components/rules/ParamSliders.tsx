// The rule's parameters as sliders and switches (spec §6.4: changing a parameter never goes through AI);
// a moved one shows the value written in the rule next to the new one.
import { RotateCcw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import type { RuleParam } from "@/lib/rules"
import { stepOf } from "@/pages/rules/ruleText"
import { cn } from "@/lib/utils"

export type ParamValues = Record<string, number | boolean>

interface Props {
  params: RuleParam[]
  values: ParamValues
  onChange: (values: ParamValues) => void
  uses?: Record<string, string[]>   // where each parameter is read (ruleText.paramUses)
}

/** The decimals a step is written with (0.25 → 2, 0.005 → 3, 1 → 0). */
const decimals = (step: number | null) => (step === null ? 0 : Math.min(6, (String(Number(step.toPrecision(10))).split(".")[1] ?? "").length))
const fmt = (v: number, step: number | null) => v.toFixed(decimals(step))

/** What a slider moves, under its name: the first place the parameter is read, all of them on hover. */
function Use({ list }: { list?: string[] }) {
  if (!list?.length) return null
  return <div className="truncate text-[0.75rem] text-subtle-foreground" title={list.join("\n")}>{list[0]}{list.length > 1 && ` 等 ${list.length} 处`}</div>
}

export function ParamSliders({ params, values, onChange, uses = {} }: Props) {
  const shown = params.filter((p) => typeof p.value === "boolean" || (typeof p.value === "number" && p.range))
  if (shown.length === 0) return <div className="text-[0.8125rem] text-subtle-foreground">这条规则没有写范围的参数（params 里给 range 和 step 才能拖）</div>
  const set = (id: string, v: number | boolean, base: number | boolean) => {
    const next = { ...values, [id]: v }
    if (v === base) delete next[id]
    onChange(next)
  }
  return (
    <div className="space-y-3">
      {shown.map((p) => {
        if (typeof p.value === "boolean") {
          const v = (values[p.id] as boolean | undefined) ?? p.value
          return (
            <label key={p.id} className="flex min-h-6 items-center justify-between gap-2 text-[0.8125rem]">
              <span className="min-w-0"><span className={cn(v !== p.value ? "text-foreground" : "text-muted-foreground")}>{p.id}{v !== p.value && " •"}</span>
                <Use list={uses[p.id]} /></span>
              <Switch checked={v} aria-label={`参数 ${p.id}`} onCheckedChange={(c) => set(p.id, c, p.value as boolean)} />
            </label>
          )
        }
        const base = p.value as number
        const v = (values[p.id] as number | undefined) ?? base
        const [lo, hi] = p.range!
        const step = stepOf(p)
        const pct = p.unit === "%"
        const inUnit = (x: number) => (pct ? fmt(x * 100, step * 100) : fmt(x, step))
        const suffix = pct ? "%" : p.unit ? ` ${p.unit}` : ""
        return (
          <label key={p.id} className="block">
            <div className="flex items-baseline justify-between text-[0.8125rem]">
              <span className={cn(v !== base ? "text-foreground" : "text-muted-foreground")}>{p.id}{v !== base && " •"}</span>
              {/* in its unit: a share as % (0.15 → 15%), the unit once at the end */}
              <span className="num">{v !== base && <span className="text-subtle-foreground">{inUnit(base)} → </span>}{inUnit(v)}{suffix}</span>
            </div>
            <Use list={uses[p.id]} />
            <input type="range" min={lo} max={hi} step={step} value={v} aria-label={`参数 ${p.id}`}
              className="mt-1 h-1 w-full accent-structure"
              onChange={(e) => set(p.id, Number(Number(e.target.value).toFixed(decimals(step) + 2)), base)} />
          </label>
        )
      })}
      {Object.keys(values).length > 0 && (
        <Button variant="ghost" size="sm" className="h-7 px-2 text-[0.8125rem]" onClick={() => onChange({})}><RotateCcw className="size-3.5" />撤销参数改动</Button>
      )}
    </div>
  )
}
