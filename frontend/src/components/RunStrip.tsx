// A long run as a strip under a page's header — a market's scan on 今日, a 验证 run: what it is doing, how far it is,
// how long it has taken, and its controls (RunControls). Paused: where it stopped, no bar.
import { CirclePause, Loader2 } from "lucide-react"
import { type ReactNode, useEffect, useState } from "react"
import { took } from "@/lib/jobs"

export function RunStrip({ testid, paused, title, text, share, since, children }: {
  testid: string
  paused?: boolean
  title: string          // whose run: "A 股", "事件研究"
  text: string           // what it is doing, or where it stopped
  share?: number | null  // the share done, when the run knows it: a bar that fills; otherwise one that sweeps
  since?: string | null  // when it began (the backend's local time)
  children?: ReactNode   // its controls
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (paused || !since) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [paused, since])
  return (
    <div className="shrink-0 border-b border-border px-4 py-1.5" data-testid={testid}>
      <div className="flex min-h-7 items-center gap-2 text-[0.8125rem]">
        {paused ? <CirclePause className="size-3.5 shrink-0 text-warning" /> : <Loader2 className="size-3.5 shrink-0 animate-spin text-structure" />}
        <span className="shrink-0 font-medium">{title}</span>
        <span className="min-w-0 truncate text-muted-foreground" title={text}>{text}</span>
        <span className="num ml-auto shrink-0 whitespace-nowrap text-subtle-foreground">
          {!paused && share != null && `${Math.round(share * 100)}% · `}{!paused && since ? `已用 ${took(since, now)}` : ""}</span>
        {children}
      </div>
      {!paused && (
        <div role="progressbar" aria-label={`${title}：${text}`} aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={share != null ? Math.round(share * 100) : undefined} className="mt-1 h-1 overflow-hidden rounded-full bg-muted">
          {share != null ? <div className="h-full rounded-full bg-structure transition-[width] duration-500" style={{ width: `${Math.max(2, share * 100)}%` }} />
            : <div className="h-full w-1/3 animate-[sweep_1.4s_ease-in-out_infinite] rounded-full bg-structure/60 motion-reduce:animate-none" />}
        </div>
      )}
    </div>
  )
}
