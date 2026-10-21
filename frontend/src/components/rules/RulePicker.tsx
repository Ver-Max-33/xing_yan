// Choose the rule laid over the chart (spec §8.3 规则预览): built-in rules first, then yours.
import { Check, ChevronDown, Layers } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import type { Market } from "@/lib/api"
import { type RuleSummary, useRules } from "@/lib/rules"
import { cn } from "@/lib/utils"

interface Props { value: string | null; market: Market | null; onChange: (id: string) => void; className?: string }

export function RulePicker({ value, market, onChange, className }: Props) {
  const { data: rules = [] } = useRules()
  const chosen = rules.find((r) => r.id === value)
  const fits = (r: RuleSummary) => !r.error && (!market || r.markets.includes(market))
  const group = (title: string, items: RuleSummary[]) => items.length > 0 && (
    <>
      <DropdownMenuLabel className="text-[0.75rem] text-subtle-foreground">{title}</DropdownMenuLabel>
      {items.map((r) => (
        <DropdownMenuItem key={r.id} disabled={!fits(r)} onSelect={() => onChange(r.id)}>
          <Check className={cn("size-3.5", r.id === value ? "opacity-100" : "opacity-0")} />
          <span className="min-w-0 flex-1 truncate">{r.name}</span>
          <span className="num text-[0.75rem] text-subtle-foreground">{r.builtin ? "内置" : `v${r.version}`}</span>
        </DropdownMenuItem>
      ))}
    </>
  )
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className={cn("no-drag max-w-[13.75rem]", className)} aria-label="选择规则">
          <Layers className="size-4" strokeWidth={1.75} />
          <span className="truncate">{chosen ? chosen.name : "选择规则"}</span>
          <ChevronDown className="size-3.5 text-subtle-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-[70vh] w-72 overflow-y-auto">
        {group("我的规则", rules.filter((r) => !r.builtin))}
        <DropdownMenuSeparator />
        {group("内置（选股公式、规格示例）", rules.filter((r) => r.group === "builtin"))}
        <DropdownMenuSeparator />
        {group("箱体模板", rules.filter((r) => r.group === "box"))}
        {group("通用模板", rules.filter((r) => r.group === "general"))}
        {rules.every((r) => r.builtin) && <div className="px-2 py-1.5 text-[0.8125rem] text-subtle-foreground">还没有自建规则，去「规则库」新建</div>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
