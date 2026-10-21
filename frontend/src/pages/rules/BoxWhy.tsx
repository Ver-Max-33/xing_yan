// 这条线凭什么画 (design 06): the box behind the judgement — its edges and the touches that set them, the
// extremes it left out, how it was confirmed and how it ended, and what came before it.
import type { Box } from "@/lib/rules"

const SCALE: Record<Box["scale"], string> = { short: "小箱", mid: "中箱", long: "大箱" }
const md = (d: string) => d.slice(5)

export function BoxWhy({ box, digits }: { box: Box | null; digits: number }) {
  const seg = box?.segments[0]
  const side = (s: "top" | "bottom") => box?.touches.filter((t) => t.side === s) ?? []
  const left = box?.events.filter((e) => (e.event === "spike_up" || e.event === "spike_down") && e.value !== null) ?? []
  return (
    <section className="px-4 py-3" data-testid="box-why">
      <div className="flex items-baseline pb-2 text-[0.875rem] font-semibold">这条线凭什么画
        {box && <span className="ml-auto text-[0.75rem] font-normal text-subtle-foreground">{SCALE[box.scale]}{box.length ? ` · ${box.length} 根` : ""}</span>}</div>
      {!box || !seg ? <div className="text-[0.8125rem] text-subtle-foreground">点一个信号日或一根 K 线，这里说明它用到的箱体为什么画在这里</div> : (
        <dl className="grid grid-cols-[56px_1fr] gap-x-2 gap-y-1.5 text-[0.8125rem]">
          <dt className="text-muted-foreground">上沿</dt>
          <dd><span className="num">{seg.top.toFixed(digits)}</span> · {side("top").length} 次触碰{side("top").length ? `：${side("top").map((t) => md(t.date)).join("、")}` : ""}</dd>
          <dt className="text-muted-foreground">下沿</dt>
          <dd><span className="num">{seg.bottom.toFixed(digits)}</span> · {side("bottom").length} 次触碰{side("bottom").length ? `：${side("bottom").map((t) => md(t.date)).join("、")}` : ""}</dd>
          {(left.length > 0 || box.excluded_before) && <>
            <dt className="text-muted-foreground">排除</dt>
            <dd>
              {box.excluded_before && <div>箱前冲高（{md(box.excluded_before)}），不作上沿</div>}
              {left.map((e) => <div key={`${e.event}${e.date}`}>{e.event === "spike_up" ? "刺穿上沿" : "刺破下沿"}（{md(e.date)}，<span className="num">{e.value!.toFixed(digits)}</span>）又收回箱内</div>)}
            </dd>
          </>}
          <dt className="text-muted-foreground">成立</dt>
          <dd>{box.known_at}{box.status === "proto" ? "（雏形）" : ""}{box.width !== null ? ` · 箱宽 ${(box.width * 100).toFixed(1)}%` : ""}</dd>
          {box.ended_at && <><dt className="text-muted-foreground">结束</dt><dd>{box.ended_at}{box.end_reason ? ` · ${box.end_reason}` : ""}</dd></>}
          {(box.context || box.position) && <><dt className="text-muted-foreground">背景</dt><dd>{[box.context, box.position].filter(Boolean).join(" · ")}</dd></>}
        </dl>
      )}
    </section>
  )
}
