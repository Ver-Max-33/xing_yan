// A rule's text as you edit it (spec §5.2): the starting text of a new rule, a copy of another rule under
// its own id, slider values written back into `params`, and saving it as a file. The text stays as you
// wrote it — comments and layout included — so these touch only the lines they must.
import type { Names, RuleStage } from "@/lib/rules"

export const NEW_RULE = `# 一条规则：股票池过滤之后，按阶段一步步匹配（spec §5.2）。阶段可以是结构（BOX 箱体等）、
# 事件（某个公式成立的那根 K 线）或时间窗；数字写成参数，就能在右边拖滑块试。
id: my_rule
name: 我的规则
markets: [CN, JP]
params:
  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}
stages:
  - {id: today, kind: event, when: "C > MA(C, $n)"}
signal_at: today.at
`

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

type JsonRule = { id?: unknown; name?: unknown; params?: { id?: unknown; value?: unknown }[] }
/** A rule written as JSON (it has no comments or layout to keep, so it is parsed and written back), or null for YAML. */
function asJson(text: string): JsonRule | null {
  if (!text.trimStart().startsWith("{")) return null
  try {
    return JSON.parse(text) as JsonRule
  } catch {
    return null
  }
}

/** The rule id written in the text, or null. */
export function ruleIdOf(text: string): string | null {
  const j = asJson(text)
  if (j) return typeof j.id === "string" ? j.id : null
  return text.match(/^id:\s*["']?([^"'\s#]+)/m)?.[1] ?? null
}

/** A copy to change under its own id: `<id>_copy`, `<name>（副本）`. */
export function copyText(text: string): string {
  const j = asJson(text)
  if (j) return JSON.stringify({ ...j, id: `${String(j.id)}_copy`, name: `${String(j.name)}（副本）` }, null, 2)
  return text
    .replace(/^(id:\s*["']?)([^"'\s#]+)/m, "$1$2_copy")
    .replace(/^(name:\s*)(["']?)(.*?)\2([ \t]*(?:#.*)?)$/m, "$1$2$3（副本）$2$4")
}

const show = (v: number | boolean | string) => (typeof v === "number" ? String(Number(v.toPrecision(12))) : String(v))

/** Write parameter values into the text's `params` (flow `{id: n, value: 20, …}` or block `- id: n` /
 *  `value: 20` items). Parameters it cannot find are listed in `missing`, the text left alone for them. */
export function applyParams(text: string, values: Record<string, number | boolean | string>): { text: string; missing: string[] } {
  const missing: string[] = []
  const j = asJson(text)
  if (j) {
    const params = j.params ?? []
    for (const [id, v] of Object.entries(values)) {
      const p = params.find((x) => x.id === id)
      if (p) p.value = v
      else missing.push(id)
    }
    return { text: JSON.stringify(j, null, 2), missing }
  }
  let out = text
  for (const [id, v] of Object.entries(values)) {
    const name = esc(id)
    const patterns = [
      new RegExp(`(\\{[^}\\n]*\\bid:\\s*${name}\\b[^}\\n]*\\bvalue:\\s*)([^,}\\s]+)`),                // {id: n, value: 20}
      new RegExp(`(\\{[^}\\n]*\\bvalue:\\s*)([^,}\\s]+)(?=[^}\\n]*\\bid:\\s*${name}\\b)`),           // {value: 20, id: n}
      new RegExp(`(-[ \\t]+id:[ \\t]*${name}[ \\t]*\\n(?:[ \\t]+[^-\\s][^\\n]*\\n)*?[ \\t]+value:[ \\t]*)([^\\s#,}]+)`), // block item
    ]
    const p = patterns.find((re) => re.test(out))
    if (p) out = out.replace(p, (_m, head: string) => `${head}${show(v)}`)
    else missing.push(id)
  }
  return { text: out, missing }
}

/** The step a parameter's slider moves by: the rule's own, else 1 for a whole number in a whole range
 *  (the backend's Rule.whole() takes only whole numbers there), else a hundredth of the range. */
export function stepOf(p: { value: unknown; range: [number, number] | null; step: number | null }): number {
  if (p.step) return p.step
  const [lo, hi] = p.range ?? [0, 1]
  if (Number.isInteger(p.value) && Number.isInteger(lo) && Number.isInteger(hi)) return 1
  return Number(((hi - lo) / 100).toPrecision(2))
}

/** Save text as a file (the browser's download: Electron asks where). */
export function downloadText(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/yaml;charset=utf-8" }))
  const a = document.createElement("a")
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

const PRETTY: [RegExp, string][] = [[/\s*>=\s*/g, " ≥ "], [/\s*<=\s*/g, " ≤ "], [/\s*!=\s*/g, " ≠ "]]
const pretty = (e: string) => PRETTY.reduce((s, [re, to]) => s.replace(re, to), e)
const span = (v: unknown) => (Array.isArray(v) ? v.join("–") : String(v))

/** Where each parameter is read ("rally.runup：RUNUP(H, L) ≥ $rally"), so a slider says what it moves. */
export function paramUses(stages: RuleStage[] | undefined, names?: Names): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  const add = (x: unknown, where: string) => {
    for (const m of JSON.stringify(x ?? "").matchAll(/\$([A-Za-z_]\w*)/g)) {
      const list = (out[m[1]] ??= [])
      if (!list.includes(where)) list.push(where)
    }
  }
  for (const s of stages ?? []) {   // by name where the rule's names are known (中箱, 突破量), else by id
    const name = names?.stages[s.id] ?? s.id
    for (const [k, v] of Object.entries(s.with ?? {})) add(v, `${name} 的 ${k}`)
    for (const r of [s.before, s.after, s.within]) if (r) add([r.bars, r.gap], `${name} 的窗口：${span(r.bars ?? r.gap)} 根`)
    for (const e of [s.when, s.all, s.any]) if (e) add(e, `${name}：${pretty(e)}`)
    for (const c of [...(s.at ?? []), ...(s.during ?? [])]) add(c.expr, `${names?.conds[`${s.id}.${c.id}`] ?? `${s.id}.${c.id}`}：${pretty(c.expr)}`)
  }
  return out
}
