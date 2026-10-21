// Small pieces the 市场 board's sections share (design 2026-10-12): a section's head and the arrow a number keeps for
// 30 s after it moved past a margin (the page updates every 10 s without flashing). Their words and helpers: ./fmt.
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** A section's head: its title, what sits beside it, and a note or a control at the right. */
export function Head({ title, extra, right, className }: { title: string; extra?: ReactNode; right?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex min-w-0 items-center gap-2 pb-2.5", className)}>
      <h2 className="shrink-0 text-[0.8125rem] font-semibold">{title}</h2>
      {extra}
      {right && <div className="ml-auto flex min-w-0 shrink items-center gap-3 text-[0.75rem] text-muted-foreground">{right}</div>}
    </div>
  )
}

export function Moved({ dir }: { dir: "up" | "down" | null }) {
  if (!dir) return null
  return <span className={cn("text-[0.625rem]", dir === "up" ? "text-up" : "text-down")} aria-label={dir === "up" ? "刚升" : "刚降"}>{dir === "up" ? "▲" : "▼"}</span>
}
