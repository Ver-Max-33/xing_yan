// 规则 tab: the rule as YAML (spec §5.2), checked as you type, and read back as 我的理解 (§6.1). The reasons
// come from the backend's rule loader in Chinese; a built-in rule is read-only (copy it to change it).
import { CircleAlert, CircleCheck } from "lucide-react"
import { toast } from "sonner"
import { AIEdit } from "@/components/rules/AIEdit"
import { Understanding } from "@/components/rules/Understanding"
import { Textarea } from "@/components/ui/textarea"
import type { RuleCheck } from "@/lib/rules"
import { applyParams } from "./ruleText"
import { marketNames } from "@/lib/format"

interface Props {
  text: string
  onText: (t: string) => void
  readOnly: boolean
  check: RuleCheck | null
  versions: { version: number; created_at: string }[]
  ruleId?: string // a saved rule: what you marked on it goes along with an AI edit
}

export function EditorTab({ text, onText, readOnly, check, versions, ruleId }: Props) {
  const setParam = (id: string, v: number | boolean) => {
    const r = applyParams(text, { [id]: v })
    if (r.missing.length) toast.error(`参数 ${id} 在规则文字里没找到，请直接改 YAML`)
    else onText(r.text)
  }
  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col p-3">
        <Textarea aria-label="规则 YAML" value={text} readOnly={readOnly} spellCheck={false}
          onChange={(e) => onText(e.target.value)}
          className="num min-h-0 flex-1 resize-none font-mono text-[0.875rem] leading-6" />
        {!readOnly && <AIEdit text={text} ruleId={ruleId} onText={onText} />}
      </div>
      <aside className="w-[26.25rem] shrink-0 overflow-y-auto border-l border-border px-4 py-3 text-[0.8125rem]">
        <div className="pb-2 text-[0.875rem] font-semibold">检查</div>
        {!check ? <div className="text-subtle-foreground">检查中…</div> : check.ok ? (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-structure"><CircleCheck className="size-4" />规则可以运行</div>
            {check.rule && <div className="text-muted-foreground">{check.rule.id} · {check.rule.name} · {marketNames(check.rule.markets)} · {check.rule.params.length} 个参数</div>}
            {check.warnings?.map((w) => (
              <div key={w} className="flex gap-1.5 text-warning" data-testid="rule-warning">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0" /><span>{w}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="space-y-1.5" data-testid="rule-errors">
            {check.errors.map((e, i) => (
              <div key={i} className="flex gap-1.5 text-error"><CircleAlert className="mt-0.5 size-3.5 shrink-0" /><span>{e}</span></div>
            ))}
          </div>
        )}
        {readOnly && <div className="mt-3 text-subtle-foreground">内置规则只读：点右上角「复制为新规则」后再改</div>}
        {check?.ok && check.understanding && <>
          <div className="pt-5 pb-1 text-[0.875rem] font-semibold">我的理解</div>
          <div className="pb-2 text-[0.75rem] text-subtle-foreground">由程序按规则逐项读出来，和实际运行的一致{readOnly ? "" : "；点蓝色参数直接改数值"}</div>
          <Understanding u={check.understanding} onParam={readOnly ? undefined : setParam} />
        </>}
        <details className="pt-5">
          <summary className="cursor-pointer pb-2 text-[0.875rem] font-semibold">写法</summary>
        <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
          <li>阶段 <code>kind</code>：<code>structure</code>（<code>use: BOX</code> 等结构）、<code>event</code>（<code>when:</code> 公式成立或 <code>on: box.break_up</code>）、<code>window</code>（<code>before</code> / <code>after</code> 某阶段若干根）</li>
          <li>条件：<code>during</code>（阶段期间）、<code>at</code>（事件那根）、<code>all</code> / <code>any</code>（窗口内每根 / 任一根）</li>
          <li>数字写成 <code>params</code> 里的参数（给 <code>range</code> 和 <code>step</code>），用 <code>$名字</code> 引用，就能在「单股调试」里拖</li>
          <li><code>signal_at</code> 是出信号的时点；<code>watch_stage</code> 是进潜伏名单的结构阶段</li>
          <li>内置方案是现成的例子：在规则库打开它们看写法</li>
        </ul>
        </details>
        {versions.length > 0 && <>
          <div className="pt-5 pb-2 text-[0.875rem] font-semibold">版本</div>
          {[...versions].reverse().map((v) => (
            <div key={v.version} className="num flex text-muted-foreground"><span className="w-10">v{v.version}</span><span>{v.created_at.replace("T", " ").slice(0, 16)}</span></div>
          ))}
        </>}
      </aside>
    </div>
  )
}
