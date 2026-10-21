import { Link } from "react-router"
import type { NavItem } from "@/app/nav"
import { PageHeader } from "./common"

/** A page whose phase has not shipped yet: says what it will do and when (spec §13). */
export default function PhasePage({ item }: { item: NavItem }) {
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={item.label} />
      <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
        <item.icon className="size-8 text-subtle-foreground" strokeWidth={1.5} />
        <div className="text-[0.9375rem] font-medium">{item.label} · {item.phase} 开放</div>
        <p className="max-w-md text-[0.8125rem] text-muted-foreground">{item.summary}</p>
        <p className="text-[0.8125rem] text-subtle-foreground">现在可以先在 <Link to="/k" className="underline">K 线</Link> 看图、画线、加观察。</p>
      </div>
    </div>
  )
}
