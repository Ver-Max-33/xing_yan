// 设置 → AI, 价格行为分析's model (design 2026-10-20 §7, dev branch): kept under its own setting, apart from the two tasks
// above, so main's AI settings read the same with or without it; the budget and the price list are the shared ones.
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { type PATask, usePASettings, useSavePASettings } from "@/lib/pa"
import { cn } from "@/lib/utils"
import { TaskControls } from "./TaskControls"

export function PATaskRow() {
  const { data } = usePASettings()
  const save = useSavePASettings()
  const [draft, setDraft] = useState<PATask | null>(null)
  if (!data) return null
  const st = data.status
  const store = () => draft && save.mutate(draft, {
    onSuccess: () => { setDraft(null); toast.success("价格行为的模型已保存") },
    onError: (e) => toast.error(String(e)),
  })
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 border-b border-border/60 pb-3" data-testid="pa-task">
      <div className="pt-1.5 font-medium">{st.name}</div>
      <TaskControls name={st.name} value={draft ?? data.task} onChange={setDraft} deepseekModel="deepseek-v4-pro" />
      <div className="flex gap-1">
        {draft && <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>撤销</Button>}
        <Button size="sm" variant="outline" disabled={!draft || save.isPending} onClick={store}>保存</Button>
      </div>
      <div className="col-span-2 col-start-2 flex flex-col gap-0.5 text-[0.8125rem] text-subtle-foreground">
        <span className={cn(st.ready && "text-down")}>{st.ready ? "可以用" : st.reason}</span>
        <span>K 线页「价格行为」：读最近 100 根日线，分两步问，高峰时一次约 ¥1–1.5，夜里和周末减半（deepseek-v4-pro、思考「高」）；单独保存，不影响上面两项</span>
      </div>
    </div>
  )
}
