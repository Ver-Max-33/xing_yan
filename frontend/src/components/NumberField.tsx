// A number field that keeps what you type: nothing is clamped, rounded or replaced by a default under the caret
// (that turned 8 into 18 and 0.025% into 25%). Each keystroke is read as you go: an invalid one is said beside the
// field (and reported, so a run can say which field is wrong) and held back; a valid one goes to the form at once,
// or — commitOn "blur", for a setting saved as it changes — when you leave the field or press Enter (so the 1 typed
// on the way to 12 is never saved).
import { useEffect, useRef, useState } from "react"
import { Input } from "@/components/ui/input"
import { type NumberRule, parseNumber, showNumber } from "@/lib/number"

export function NumberField({ value, onChange, rule, onValidity, commitOn = "change", ...props }: {
  value: number | null | undefined; onChange: (v: number | null) => void; rule: NumberRule
  onValidity?: (error: string | null) => void; commitOn?: "change" | "blur"
} & Omit<React.ComponentProps<typeof Input>, "value" | "onChange" | "type" | "defaultValue">) {
  const [text, setText] = useState<string | null>(null)   // what you typed, while it is yours
  const [error, setError] = useState<string | null>(null)
  const typed = useRef<{ value: number | null } | null>(null)   // a valid value waiting for the blur
  const report = useRef(onValidity)
  useEffect(() => { report.current = onValidity })
  useEffect(() => () => report.current?.(null), [])           // gone (the form folded): nothing wrong is left behind
  return (
    <>
      <Input {...props} type="text" inputMode="decimal" autoComplete="off" aria-invalid={error ? true : undefined}
        value={text ?? showNumber(value, rule)}
        onChange={(e) => {
          const t = e.target.value
          const p = parseNumber(t, rule)
          setText(t)
          setError(p.error)
          report.current?.(p.error)
          typed.current = p.error ? null : { value: p.value }
          if (!p.error && commitOn === "change") onChange(p.value)
        }}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); props.onKeyDown?.(e) }}
        onBlur={(e) => {
          if (commitOn === "blur" && typed.current && !error && typed.current.value !== (value ?? null)) onChange(typed.current.value)
          typed.current = null
          if (!error) setText(null)
          props.onBlur?.(e)
        }} />
      {error && <span role="alert" className="text-[0.75rem] text-error">{error}</span>}
    </>
  )
}
