import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** 44px page toolbar (design 00: "每页一条 44px 工具栏"); doubles as the window drag area. */
export function PageHeader({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    // nothing in a toolbar wraps: squeezed, Chinese would break between any two characters, one per line. A container: what
    // it holds can give way by its own width (on Windows ~150 px less, the window's buttons' room — .win-overlay)
    <header className="@container drag clear-window-buttons flex h-11 shrink-0 items-center gap-3 border-b border-border px-4 whitespace-nowrap">
      <h1 className="min-w-0 overflow-hidden text-[0.9375rem] font-semibold">{title}</h1>
      <div className="no-drag ml-auto flex items-center gap-2">{children}</div>
    </header>
  )
}

/** A titled section separated by hairlines (design: "用边线分区，不用卡片"). */
export function Section({ title, hint, children, className }: { title: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section id={`sec-${title}`} className={cn("scroll-mt-2 border-b border-border py-5", className)}>
      <h2 className="text-[0.9375rem] font-semibold">{title}</h2>
      {hint && <p className="mt-1 text-[0.8125rem] text-muted-foreground">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  )
}

/** Parts kept whole, a squeezed line breaking only between them, at the separator: "CN 09-30 · 信号 12 · 潜伏 4290"
 *  wraps at a " · ", never inside a figure. */
export function Parts({ parts, sep }: { parts: ReactNode[]; sep: string }) {
  return <>{parts.map((p, i) => <span key={i}>{i ? sep : ""}<span className="whitespace-nowrap">{p}</span></span>)}</>
}

/** A figure in a card; the cards go onto another row before a figure is squeezed. */
export function KpiRow({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-[repeat(auto-fit,minmax(12rem,1fr))]">{children}</div>
}

export function Kpi({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: ReactNode }) {
  return (
    <div className="min-w-0 border-r border-b border-border px-5 py-4">
      <div className="text-[0.8125rem] text-muted-foreground">{label}</div>
      {/* two figures (浮盈 / 浮亏) break at the slash, never inside */}
      <div className={cn("num mt-1 text-[1.5625rem] leading-8", tone)}><Parts sep=" / " parts={value.split(" / ")} /></div>
      {sub && <div className="mt-1 text-[0.75rem] text-subtle-foreground">{sub}</div>}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-[0.8125rem] text-subtle-foreground">{children}</div>
}
