// ⌘K: jump to any security (code, name, pinyin initials, Japanese or English name) or page; empty, it starts from the
// stocks you looked at last.
import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
import { codeOf } from "@/lib/format"
import { endpoints, useRecent } from "@/lib/api"
import { ALL_NAV } from "./nav"

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [q, setQ] = useState("")
  const dq = useDebounced(q.trim(), 120)
  const navigate = useNavigate()
  const { data: hits = [] } = useQuery({ queryKey: ["search", dq], queryFn: () => endpoints.search(dq, 30), enabled: dq.length > 0 })
  const { data: recentAll = [] } = useRecent()
  // what is on this side follows each key at once (the debounce is for the backend's search): Enter right after typing
  // never takes a row the query has already left
  const qq = q.trim().replace(/\s/g, "")
  const recent = qq ? [] : recentAll.slice(0, 6)
  const pages = qq ? ALL_NAV.filter((n) => n.label.replace(/\s/g, "").toLowerCase().includes(qq.toLowerCase())) : ALL_NAV
  const fresh = dq === q.trim() ? hits : []   // results of a query already left behind are never the row Enter takes
  // Results arrive asynchronously, so cmdk does not pick a row by itself: the first row is selected
  // until the user moves, and again whenever a new result list arrives.
  const first = recent[0] ? `recent ${recent[0].listing_id}` : fresh[0]?.listing_id ?? (pages[0] ? `page ${pages[0].label}` : "")
  const [moved, setMoved] = useState({ first, value: first })
  const selected = moved.first === first ? moved.value : first

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onOpenChange])

  const go = (to: string) => {
    onOpenChange(false)
    setQ("")
    navigate(to)
  }

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="搜索或跳转" description="代码、名称、拼音首字母、日文或英文名">
      {/* results come from the backend's ranking, so cmdk's own fuzzy filter is off */}
      <Command shouldFilter={false} value={selected} onValueChange={(value) => setMoved({ first, value })}>
      <CommandInput placeholder="代码 / 名称 / 拼音（如 gzmt）/ トヨタ / toyota" value={q} onValueChange={setQ} />
      <CommandList>
        <CommandEmpty>{qq ? "没有匹配的证券或页面" : "输入以搜索"}</CommandEmpty>
        {recent.length > 0 && (
          <CommandGroup heading="最近看过">
            {recent.map((r) => (
              <CommandItem key={r.listing_id} value={`recent ${r.listing_id}`} onSelect={() => go(`/k/${r.listing_id}`)}>
                <span className="num w-16 text-muted-foreground">{codeOf(r.listing_id)}</span>
                <span className="flex-1 truncate">{r.name ?? codeOf(r.listing_id)}</span>
                <span className="text-[0.75rem] text-subtle-foreground">{r.listing_id.split(":")[0]}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {hits.length > 0 && (
          <CommandGroup heading="证券">
            {hits.map((h) => (
              <CommandItem key={h.listing_id} value={h.listing_id} onSelect={() => go(`/k/${h.listing_id}`)}>
                <span className="num w-16 text-muted-foreground">{h.code}</span>
                <span className="flex-1 truncate">{h.name ?? h.name_en ?? h.code}</span>
                <span className="text-[0.75rem] text-subtle-foreground">{h.market} {h.board ?? ""}{h.status === "D" ? " · 已退市" : ""}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {pages.length > 0 && (
          <CommandGroup heading="页面">
            {pages.map((n) => (
              <CommandItem key={n.to} value={`page ${n.label}`} onSelect={() => go(n.to)}>
                <n.icon className="size-4" strokeWidth={1.75} />
                <span className="flex-1">{n.label}</span>
                {n.phase && <span className="text-[0.75rem] text-subtle-foreground">{n.phase}</span>}
              </CommandItem>
            ))}
          </CommandGroup>
        )}
      </CommandList>
      </Command>
    </CommandDialog>
  )
}
