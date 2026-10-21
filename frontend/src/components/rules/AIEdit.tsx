// 改语义 (spec §6.4): say in one sentence how the rule should change; AI returns the whole new rule with a note, the
// program shows the difference, and "用这个修改" puts it in the editor (unsaved — preview it, then save a version).
// What you marked on this rule in the gallery goes along, so the change can keep your right hits.
import { Sparkles } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { type AIFailed, aiEndpoints, type Draft, failureOf, useAIStatus } from "@/lib/ai"
import { DiffView } from "./DiffView"

export function AIEdit({ text, ruleId, onText }: { text: string; ruleId?: string; onText: (t: string) => void }) {
  const { data: ai } = useAIStatus()
  const [instruction, setInstruction] = useState("")
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState<Draft | null>(null)
  const [failure, setFailure] = useState<AIFailed | null>(null)
  const ready = ai?.tasks.rule_assistant.ready ?? false
  const ask = async () => {
    if (!instruction.trim()) return
    setBusy(true)
    setFailure(null)
    try {
      setProposal(await aiEndpoints.edit(text, instruction, ruleId))
    } catch (e) {
      setFailure(failureOf(e))
    } finally {
      setBusy(false)
    }
  }
  if (proposal) {
    return (
      <div className="mt-2 rounded-md border border-border p-3 text-[0.8125rem]" data-testid="edit-proposal">
        <div className="pb-1 font-medium">AI 的修改：{proposal.note || "（没有说明）"}</div>
        <DiffView diff={proposal.diff ?? ""} />
        <div className="mt-2 flex gap-2">
          <Button size="sm" onClick={() => { onText(proposal.rule_text); setProposal(null); setInstruction("") }}>用这个修改</Button>
          <Button size="sm" variant="ghost" onClick={() => setProposal(null)}>不要</Button>
        </div>
      </div>
    )
  }
  return (<>
    <form className="mt-2 flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); void ask() }}>
      <Sparkles className="size-4 shrink-0 text-subtle-foreground" strokeWidth={1.75} />
      <Input aria-label="用一句话修改这条规则" value={instruction} onChange={(e) => setInstruction(e.target.value)} disabled={!ready}
        placeholder={ready ? "用一句话改这条规则的含义，例如「箱体期间不能出现涨停」" : `AI 改规则要先配置：${ai?.tasks.rule_assistant.reason ?? "…"}`}
        className="h-8 text-[0.875rem]" />
      {ready ? <Button size="sm" type="submit" variant="outline" disabled={busy || !instruction.trim()}>{busy ? "修改中…" : "生成修改"}</Button>
        : <Link to="/settings?s=ai" className="shrink-0 text-[0.8125rem] underline">去设置</Link>}
    </form>
    {failure && <AIFailure failure={failure} testId="edit-failure" />}
  </>)
}

export function AIFailure({ failure, testId }: { failure: AIFailed; testId: string }) {
  return (
    <div className="mt-3 rounded-md border border-error/40 bg-error/5 px-3 py-2 text-[0.8125rem] text-error" data-testid={testId}>
      <div className="whitespace-pre-wrap">{failure.message}</div>
      {failure.raw && <details className="mt-1 text-muted-foreground"><summary className="cursor-pointer">AI 的原始输出</summary>
        <pre className="num max-h-[15rem] overflow-auto whitespace-pre-wrap pt-1 text-[0.75rem]">{failure.raw}</pre></details>}
    </div>
  )
}
