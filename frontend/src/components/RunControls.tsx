// The controls of a long run — a market's scan on 今日, a 验证 run — in one set of words everywhere: 暂停 keeps what it
// finished and 继续 goes on from there; 取消 gives the run up, asked first when it would throw finished work away
// (a run still waiting in the queue goes at once: nothing of it would be lost).
import { Loader2, Pause, Play, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"

export type RunState = "queued" | "running" | "pausing" | "cancelling" | "paused"

// 暂停 and 继续 change the controls (on 今日 the paused run's own line replaces the running one): the focus that was on
// them goes to the controls shown next — the status while it stops, then 继续 — not to the page's <body>
let focusNext = 0   // until when the next controls shown take the focus

export function RunControls({ state, what, noun, lost, after, onPause, onResume, onCancel }: {
  state: RunState
  what: string    // the run, as the question names it: "A 股扫描", "组合回测"
  noun: string    // what 取消 gives up, on its button: "扫描", "运行"
  lost?: string   // what cancelling throws away ("4 / 14 条规则"); without it the question does not say how much
  after: string   // what stays when it is cancelled: "今日页还是上一次扫描的结果"
  onPause?: () => void; onResume?: () => void; onCancel: () => void
}) {
  const [asking, setAsking] = useState(false)
  const box = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (Date.now() > focusNext) return
    const next = box.current?.querySelector<HTMLElement>("button") ?? box.current
    next?.focus()
    if (next?.tagName === "BUTTON") focusNext = 0
  }, [state])
  const press = (f: () => void) => () => {
    if (box.current?.contains(document.activeElement)) focusNext = Date.now() + 10_000
    f()
  }
  if (state === "pausing" || state === "cancelling") {
    return (
      <span ref={box} tabIndex={-1} className="flex shrink-0 items-center gap-1.5 text-[0.8125rem] whitespace-nowrap text-muted-foreground outline-none" role="status">
        <Loader2 className="size-3.5 animate-spin" />{state === "pausing" ? "正在暂停…" : "正在取消…"}
      </span>
    )
  }
  const cancel = () => (state === "queued" ? onCancel() : setAsking(true))
  const space = (s: string) => (/^[\w.]/.test(s) ? ` ${s}` : s)   // "这次 A 股扫描", "这次组合回测"
  return (
    <span ref={box} className="flex shrink-0 items-center gap-1">
      {state === "running" && onPause && (
        <Button size="sm" variant="ghost" className="h-7" onClick={press(onPause)}
          title="做完当前这一步就停，做完的部分保留；之后点「继续」接着做"><Pause className="size-3.5" />暂停</Button>
      )}
      {state === "paused" && onResume && (
        <Button size="sm" variant="outline" className="h-7" onClick={press(onResume)}
          title="从暂停的地方接着做"><Play className="size-3.5" />继续</Button>
      )}
      <Button size="sm" variant="ghost" className="h-7" onClick={cancel}
        title={state === "queued" ? "不做了：从队列里拿掉" : `不做了：这次${noun}作废，${after}`}><X className="size-3.5" />取消</Button>
      <Dialog open={asking} onOpenChange={setAsking}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>取消这次{space(what)}？</DialogTitle>
            <DialogDescription>
              {lost ? `已经做完的${space(lost)}不会保留` : "已经做完的部分不会保留"}，{after}。
              {state === "running" && onPause ? "想以后从这里接着做，可以改点「暂停」。" : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAsking(false)}>{state === "paused" ? "保持暂停" : `继续${noun}`}</Button>
            <Button variant="destructive" onClick={() => { setAsking(false); onCancel() }}>取消{noun}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </span>
  )
}
