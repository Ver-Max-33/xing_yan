import { Search } from "lucide-react"
import { NavLink } from "react-router"
import { Badge } from "@/components/ui/badge"
import { Kbd } from "@/components/ui/kbd"
import { useStatus } from "@/lib/api"
import { useWatchEntries } from "@/lib/watch"
import { useRules, useRuns } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { BOTTOM_NAV, MAIN_NAV, type NavItem, STRATEGY_NAV } from "./nav"
import { desktop } from "./platform"

function Item({ item, count }: { item: NavItem; count?: number }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      className={({ isActive }) =>
        cn("no-drag flex h-8 items-center gap-2.5 rounded-md px-2 text-[0.875rem] text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
          isActive && "bg-sidebar-accent text-foreground")}
    >
      <item.icon className="size-4 shrink-0" strokeWidth={1.75} />
      <span className="flex-1 truncate">{item.label}</span>
      {item.phase && <span className="text-[0.75rem] text-subtle-foreground">{item.phase}</span>}
      {!item.phase && count !== undefined && count > 0 && <span className="num text-[0.75rem] text-subtle-foreground">{count}</span>}
    </NavLink>
  )
}

/** Rules you enabled, each with the signals of its latest daily run (in any market). */
function EnabledRules() {
  const { data: rules = [] } = useRules()
  const { data: runs = [] } = useRuns(undefined, 200)
  const enabled = rules.filter((r) => r.enabled && !r.archived)
  if (enabled.length === 0) return <div className="px-2 text-[0.8125rem] text-subtle-foreground">在规则库里启用规则后列在这里</div>
  return (
    <>
      {enabled.map((r) => {
        const last = runs.find((x) => x.rule_id === r.id && x.mode === "date")
        return (
          <NavLink key={r.id} to={`/rules/${r.id}`}
            className={({ isActive }) => cn("no-drag flex h-8 items-center gap-2.5 rounded-md px-2 text-[0.875rem] text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
              isActive && "bg-sidebar-accent text-foreground")}>
            <span className="size-4 shrink-0 rounded-[3px] border border-dashed border-structure/70" />
            <span className="flex-1 truncate">{r.name}</span>
            {last && <span className="num text-[0.75rem] text-subtle-foreground" title={`${last.market} ${last.day} 的运行：信号`}>{last.counts.signal ?? 0}</span>}
          </NavLink>
        )
      })}
    </>
  )
}

export function Sidebar({ onSearch }: { onSearch: () => void }) {
  const { data: status } = useStatus()
  const { data: entries } = useWatchEntries()
  const watchCount = entries?.filter((e) => e.status === "watching").length // 观察中
  const profiles = status?.markets ?? []
  return (
    <aside className="flex w-[13.75rem] shrink-0 flex-col border-r border-sidebar-border bg-sidebar">
      {/* the window's top row is its title bar: on macOS the traffic lights have the sidebar's part to themselves;
          on Windows (its buttons are at the far right) the name sits in it, level with the page's toolbar */}
      {desktop.isMac && <div className="drag h-11 shrink-0" />}
      <div className={cn("flex items-center gap-2 px-4", desktop.isMac ? "pb-3" : "drag mb-1 h-11 shrink-0")}>
        <span className="text-[1rem] font-semibold tracking-tight">形研</span>
        {profiles.map((m) => (
          <Badge key={m.market} variant="outline"
            className={cn("no-drag h-5 px-1.5 text-[0.75rem] font-normal", m.profile === "test" ? "border-warning/50 text-warning" : "text-muted-foreground")}
            title={m.profile === "test" ? "测试档：数据仅用于开发与熟悉功能" : "正式档"}>
            {m.market} {m.profile === "test" ? "测试档" : "正式档"}
          </Badge>
        ))}
      </div>
      <button type="button" onClick={onSearch}
        className="no-drag mx-3 mb-3 flex h-8 items-center gap-2 rounded-md border border-input bg-background/40 px-2.5 text-[0.875rem] text-subtle-foreground hover:text-muted-foreground">
        <Search className="size-4" strokeWidth={1.75} />
        <span className="flex-1 text-left">搜索或跳转</span>
        <Kbd>{desktop.mod}K</Kbd>
      </button>
      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2">
        {MAIN_NAV.map((i) => <Item key={i.to} item={i} count={i.to === "/watch" ? watchCount : undefined} />)}
        <div className="px-2 pt-4 pb-1 text-[0.75rem] text-subtle-foreground">策略</div>
        {STRATEGY_NAV.map((i) => <Item key={i.to} item={i} />)}
        <div className="px-2 pt-4 pb-1 text-[0.75rem] text-subtle-foreground">已启用规则</div>
        <EnabledRules />
      </nav>
      <div className="flex flex-col gap-0.5 px-2 pb-3">
        {BOTTOM_NAV.map((i) => <Item key={i.to} item={i} />)}
      </div>
    </aside>
  )
}
