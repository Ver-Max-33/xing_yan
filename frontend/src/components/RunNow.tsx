// A run of 验证's queue — a 验证 run, a 校准 — while it waits, runs or is paused: its strip (where it is, how long)
// and 暂停 / 继续 / 取消.
import { RunControls } from "@/components/RunControls"
import { RunStrip } from "@/components/RunStrip"
import type { ValidateRun } from "@/lib/validate"

export function RunNow({ run, title, noun = "运行", after, onAct, testid = "run-now" }: {
  run: ValidateRun; title: string; noun?: string
  after: string   // what stays if it is cancelled
  onAct: (what: "pause" | "resume" | "cancel") => void; testid?: string
}) {
  const state = run.asked === "cancel" ? "cancelling" : run.asked === "pause" ? "pausing" : run.status as "queued" | "running" | "paused"
  return (
    <RunStrip testid={testid} paused={run.status === "paused"} title={title} since={run.created_at.slice(0, 19)}
      text={run.progress ?? (run.status === "queued" ? "排队中" : "")}>
      <RunControls state={state} what={title} noun={noun} after={after}
        onPause={() => onAct("pause")} onResume={() => onAct("resume")} onCancel={() => onAct("cancel")} />
    </RunStrip>
  )
}
