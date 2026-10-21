// A task's model in 设置 → AI (spec §10.1): the provider, the model and how hard it thinks — the AI tasks' rows and
// 价格行为分析's (design 2026-10-20 §7) share them.
import { Input } from "@/components/ui/input"
import type { Effort, Provider } from "@/lib/ai"

const PROVIDER_NAME: Record<Provider, string> = { deepseek: "DeepSeek", openrouter: "OpenRouter" }
const EFFORTS: [Effort, string][] = [["default", "默认"], ["none", "关闭"], ["low", "低"], ["high", "高"], ["max", "最高"]]

type TaskModel = { provider: Provider; model: string; effort: Effort }

/** A task's provider, model and how hard it thinks (a provider change starts from its own default: `deepseekModel`). */
export function TaskControls({ name, value, onChange, deepseekModel = "deepseek-flash" }:
  { name: string; value: TaskModel; onChange: (v: TaskModel) => void; deepseekModel?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <select aria-label={`${name}的供应商`} value={value.provider}
        onChange={(e) => {
          const deepseek = e.target.value === "deepseek"
          onChange({ provider: e.target.value as Provider, model: deepseek ? deepseekModel : "", effort: deepseek ? "high" : "default" })
        }}
        className="h-8 rounded-md border border-input px-2">
        {(["deepseek", "openrouter"] as Provider[]).map((p) => <option key={p} value={p}>{PROVIDER_NAME[p]}</option>)}
      </select>
      <Input aria-label={`${name}的模型`} value={value.model} className="h-8 w-56" list={value.provider === "deepseek" ? "deepseek-models" : undefined}
        placeholder={value.provider === "openrouter" ? "例如 anthropic/claude-sonnet-4.5" : deepseekModel}
        onChange={(e) => onChange({ ...value, model: e.target.value })} />
      <label className="flex items-center gap-1.5 text-muted-foreground" title="模型回答前想多久：越高越准、越慢、越贵（思考的 token 按输出计费）；关闭就直接回答">思考
        <select aria-label={`${name}的思考强度`} value={value.effort} onChange={(e) => onChange({ ...value, effort: e.target.value as Effort })}
          className="h-8 rounded-md border border-input px-2 text-foreground">
          {EFFORTS.map(([v, l]) => <option key={v} value={v}>{v === "default" && value.provider === "deepseek" ? "默认（高）" : l}</option>)}
        </select></label>
    </div>
  )
}

