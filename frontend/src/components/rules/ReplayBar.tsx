// The replay timeline (spec §6.3 逐根回放, §8.3, design 06): drag back to any day — the chart then shows only
// the structures and signals known that day; future bars are not loaded at all.
import { Pause, Play, SkipBack, SkipForward, StepBack, StepForward, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { dayIndex } from "./replay"

interface Props {
  dates: string[]                       // the listing's trading days, oldest first
  value: string | null                  // the replay day; null = the latest bar
  onChange: (day: string | null) => void
  onClose: () => void
  className?: string
}

const SPEEDS = [1, 2, 4]

export function ReplayBar({ dates, value, onChange, onClose, className }: Props) {
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const last = dates.length - 1
  const i = dayIndex(dates, value)
  const at = useRef(i)
  at.current = i
  const go = (k: number) => {
    const j = Math.max(0, Math.min(last, k))
    onChange(j >= last ? null : dates[j])
  }
  useEffect(() => {
    if (!playing) return
    const id = window.setInterval(() => {
      if (at.current >= last) setPlaying(false)
      else go(at.current + 1)
    }, 700 / speed)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed, last])
  if (dates.length === 0) return null
  return (
    <div className={cn("border-t border-border px-3 py-2", className)} data-testid="replay-bar">
      <div className="flex items-center gap-1.5">
        <Button variant="ghost" size="icon-sm" aria-label="回到开头" onClick={() => go(0)}><SkipBack className="size-4" /></Button>
        <Button variant="ghost" size="icon-sm" aria-label="后退一根" onClick={() => go(i - 1)}><StepBack className="size-4" /></Button>
        <Button variant="ghost" size="icon-sm" aria-label={playing ? "暂停" : "播放"} onClick={() => setPlaying(!playing)}>
          {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="前进一根" onClick={() => go(i + 1)}><StepForward className="size-4" /></Button>
        <Button variant="outline" size="sm" className="num h-6 px-1.5 text-[0.75rem]" aria-label="播放速度"
          onClick={() => setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length])}>{speed}×</Button>
        <input type="range" min={0} max={last} value={i} aria-label="回放日期" className="mx-2 h-1 min-w-0 flex-1 accent-structure"
          onChange={(e) => go(Number(e.target.value))} />
        <span className="num w-[7.5rem] text-right text-[0.8125rem]">{value === null ? `最新 · ${dates[last]}` : dates[i]}</span>
        <Button variant="ghost" size="icon-sm" aria-label="回到最新" onClick={() => go(last)}><SkipForward className="size-4" /></Button>
        <Button variant="ghost" size="icon-sm" aria-label="退出回放" onClick={onClose}><X className="size-4" /></Button>
      </div>
      <div className="pt-1 text-[0.75rem] text-subtle-foreground">拖动时间轴回到任意一天：图上只显示那天已经知道的结构和信号，未来数据不会提前出现。← → 一根一根走，空格退出。</div>
    </div>
  )
}
