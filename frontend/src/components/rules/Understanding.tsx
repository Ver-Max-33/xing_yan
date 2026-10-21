// 我的理解 (spec §6.1, design 05「系统的理解」): the rule read back by the program from the registry's own
// descriptions. Its parameters are chips: change one and the value is written into the rule's text. A chip reads and
// edits in its unit: 15% is typed 15, and written into the rule as 0.15.
import { useState } from "react"
import type { Understanding as U, UnderstandSeg } from "@/lib/rules"
import { cn } from "@/lib/utils"

type Chip = Exclude<UnderstandSeg, string>
const num = (v: number) => String(Number(v.toPrecision(12)))
const show = (v: Chip["value"], unit = "") => (typeof v === "boolean" ? (v ? "是" : "否")
  : typeof v === "number" ? (unit === "%" ? `${num(v * 100)}%` : `${num(v)}${unit ? ` ${unit}` : ""}`) : v)

function ParamChip({ chip, onParam }: { chip: Chip; onParam?: (id: string, v: number | boolean) => void }) {
  const [editing, setEditing] = useState<string | null>(null)
  const cls = "num mx-0.5 rounded bg-structure/15 px-1 text-structure"
  const pct = chip.unit === "%"
  if (!onParam || typeof chip.value === "string") return <span className={cls} title={`参数 ${chip.param}`}>{show(chip.value, chip.unit)}</span>
  if (typeof chip.value === "boolean") {
    return <button type="button" className={cn(cls, "hover:bg-structure/25")} title={`参数 ${chip.param}：点一下改成${chip.value ? "否" : "是"}`}
      onClick={() => onParam(chip.param, !chip.value)}>{show(chip.value)}</button>
  }
  if (editing !== null) {
    const commit = () => {
      const typed = Number(editing)
      const v = pct ? Number((typed / 100).toPrecision(12)) : typed   // 15 typed for 15% is 0.15 in the rule
      setEditing(null)
      if (editing.trim() !== "" && Number.isFinite(v) && v !== chip.value) onParam(chip.param, v)
    }
    return <span className="whitespace-nowrap">
      <input autoFocus aria-label={`参数 ${chip.param} 的值`} value={editing} onChange={(e) => setEditing(e.target.value)}
        onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); else if (e.key === "Escape") setEditing(null) }}
        className="num mx-0.5 w-16 rounded border border-structure bg-transparent px-1 text-structure outline-none" />
      {chip.unit && <span className="text-structure">{chip.unit}</span>}
    </span>
  }
  return <button type="button" className={cn(cls, "hover:bg-structure/25")} title={`参数 ${chip.param}：点一下改数值`}
    onClick={() => setEditing(num(pct ? chip.value as number * 100 : chip.value as number))}>{show(chip.value, chip.unit)}</button>
}

export function Understanding({ u, onParam }: { u: U; onParam?: (id: string, v: number | boolean) => void }) {
  return (
    <div className="space-y-3 text-[0.8125rem]" data-testid="understanding">
      <div className="text-[0.875rem] font-medium">{u.summary}</div>
      {u.sections.map((s) => (
        <section key={s.title}>
          <div className="pb-0.5 text-[0.75rem] text-subtle-foreground">{s.title}</div>
          {s.lines.map((line, i) => {
            const sub = typeof line[0] === "string" && line[0].startsWith("   ") // a stage's settings and conditions
            return (
              <div key={i} className={cn("leading-6 break-words", sub && "pl-4 text-muted-foreground")}>
                {line.map((x, j) => (typeof x === "string"
                  ? <span key={j}>{j === 0 && sub ? x.trimStart() : x}</span>
                  : <ParamChip key={`${j}-${x.param}-${String(x.value)}`} chip={x} onParam={onParam} />))}
              </div>
            )
          })}
        </section>
      ))}
    </div>
  )
}
