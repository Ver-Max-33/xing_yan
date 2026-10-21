// Pick a stock by code or name (the same search as ⌘K).
import { useQuery } from "@tanstack/react-query"
import { ChevronsUpDown } from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { endpoints, type SearchHit, useInstrument } from "@/lib/api"
import { codeOf } from "@/lib/format"

export function ListingPicker({ value, onPick }: { value: string | null; onPick: (listingId: string) => void }) {
  const [q, setQ] = useState("")
  const { data: inst } = useInstrument(value ?? undefined)
  const { data: found = [] } = useQuery({ queryKey: ["search", q], queryFn: () => endpoints.search(q, 8), enabled: q.trim().length > 0 })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="min-w-[11.25rem] justify-between" aria-label="选择股票">
          <span className="truncate">{value ? (inst?.name ?? codeOf(value)) : "选一只股票"}</span>
          {value && <span className="num text-[0.8125rem] text-subtle-foreground">{codeOf(value)}</span>}
          <ChevronsUpDown className="size-3.5 text-subtle-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <div className="px-2 py-1.5" onKeyDown={(e) => e.stopPropagation()}>
          <Input aria-label="搜索股票" value={q} onChange={(e) => setQ(e.target.value)} placeholder="代码或名称" className="h-7" autoFocus />
        </div>
        {(found as SearchHit[]).map((h) => (
          <DropdownMenuItem key={h.listing_id} onSelect={() => { onPick(h.listing_id); setQ("") }}>
            <span className="min-w-0 flex-1 truncate">{h.name ?? h.code}</span>
            <span className="num text-[0.75rem] text-subtle-foreground">{h.listing_id}</span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
