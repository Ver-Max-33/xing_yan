// 指标库 (design 2026-10-10 §5): your indicators and the built-in ones — 常用 (通达信 / TradingView formulas), 组合设置
// and ICT 概念 (rewritten from ArunKBhaskar/PineScript) — with where each is drawn, its parameters and markets. Open one
// to read or change it, copy a built-in to make it yours, put one away; on the K 线 page 「指标 → 指标库」 draws them.
import { useQueryClient } from "@tanstack/react-query"
import { ClipboardPaste, Download, MoreHorizontal, Plus } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { marketNames } from "@/lib/format"
import { GROUP_NAME, type IndicatorGroup, indicatorEndpoints, type IndicatorSummary, ordered, useIndicators } from "@/lib/indicators"
import { cn } from "@/lib/utils"
import { Empty, PageHeader, Section } from "./common"
import { downloadText } from "./rules/ruleText"

const PANE: Record<string, string> = { main: "主图", sub: "副图", auto: "自动" }
const HINT: Record<IndicatorGroup, string> = {
  mine: "",
  common: "通达信、TradingView 的标准公式。在 K 线页「指标 → 指标库」里勾上就画在图上，参数要改就复制一份。",
  setup: "改写自 ArunKBhaskar/PineScript 的组合设置：几个指标同时满足时，在 K 线上画出信号。",
  ict: "改写自 ArunKBhaskar/PineScript：缺口、流动性、结构转换、订单块。仓库的默认参数多是给日内图的，这里按日线重定，每个的说明里写着。",
}
const NARROW = "@max-[52rem]:hidden"   // the columns that give way to the name in a narrow table

const params = (r: IndicatorSummary) => r.params.map((p) => `${p.id} ${p.value}`).join(" · ")

function Rows({ rows, onOpen }: { rows: IndicatorSummary[]; onOpen: (id: string) => void }) {
  const qc = useQueryClient()
  const archive = async (r: IndicatorSummary) => {
    try {
      await indicatorEndpoints.setState(r.id, !r.archived)
      await qc.invalidateQueries({ queryKey: ["indicators"] })
    } catch (e) {
      toast.error(`没改成：${String(e)}`)
    }
  }
  const exportYaml = async (r: IndicatorSummary) => {
    try {
      downloadText(`${r.id}.yaml`, (await indicatorEndpoints.get(r.id)).text)
    } catch (e) {
      toast.error(`导出失败：${String(e)}`)
    }
  }
  return (
    <div className="@container">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow>
            <TableHead>名称</TableHead><TableHead className="w-16">画在</TableHead>
            <TableHead className={cn("w-[14rem]", NARROW)}>参数</TableHead><TableHead className="w-[6.5rem]">市场</TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.id} className="cursor-pointer" onClick={() => onOpen(r.id)} data-testid={`indicator-${r.id}`}>
              <TableCell>
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium" title={r.name}>{r.name}</span>
                  {r.builtin && <Badge variant="outline" className="h-5 px-1.5 text-[0.75rem] font-normal text-muted-foreground">内置</Badge>}
                  {!r.builtin && <Badge variant="outline" className="num h-5 px-1.5 text-[0.75rem] font-normal">v{r.version}</Badge>}
                </div>
                {r.error ? <div className="mt-0.5 truncate text-[0.8125rem] text-error" title={r.error}>{r.error}</div>
                  : r.doc && <div className="mt-0.5 truncate text-[0.8125rem] text-subtle-foreground" title={r.doc}>{r.doc}</div>}
              </TableCell>
              <TableCell className="text-muted-foreground">{PANE[r.pane]}</TableCell>
              <TableCell className={cn("num truncate text-[0.8125rem] text-muted-foreground", NARROW)} title={params(r)}>{params(r) || "—"}</TableCell>
              <TableCell className="num text-muted-foreground">{marketNames(r.markets) || "—"}</TableCell>
              <TableCell onClick={(e) => e.stopPropagation()}>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon-sm" aria-label={`${r.name} 的更多操作`}><MoreHorizontal className="size-4" /></Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onSelect={() => onOpen(r.id)}>打开</DropdownMenuItem>
                    <DropdownMenuItem disabled={!!r.error} onSelect={() => onOpen(`new?from=${encodeURIComponent(r.id)}`)}>复制为新指标</DropdownMenuItem>
                    <DropdownMenuItem disabled={!!r.error} onSelect={() => void exportYaml(r)}><Download className="size-4" />导出 YAML</DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onSelect={() => void archive(r)}>{r.archived ? "放回指标库" : "收起（K 线菜单里不再列出）"}</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export default function IndicatorsPage() {
  const navigate = useNavigate()
  const { data, isLoading } = useIndicators()
  const [showArchived, setShowArchived] = useState(false)
  const all = data?.indicators ?? []
  const open = (id: string) => navigate(`/indicators/${id}`)
  const of = (g: IndicatorGroup) => ordered(all.filter((r) => !r.archived), g)
  const archived = all.filter((r) => r.archived)
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="指标库">
        <Button variant="outline" size="sm" onClick={() => navigate("/indicators/new?paste=1")}><ClipboardPaste className="size-4" />粘贴通达信公式</Button>
        <Button size="sm" onClick={() => navigate("/indicators/new")}><Plus className="size-4" />新建指标</Button>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto px-6">
        <Section title={`${GROUP_NAME.mine} ${of("mine").length || ""}`.trim()}>
          {of("mine").length ? <Rows rows={of("mine")} onOpen={open} />
            : <Empty>{isLoading ? "…" : "还没有自己的指标。新建一个、把通达信 / 同花顺的公式粘进来，或者从下面复制一个来改。"}</Empty>}
        </Section>
        {(["common", "setup", "ict"] as IndicatorGroup[]).map((g) => (
          <Section key={g} title={GROUP_NAME[g]} hint={HINT[g]}>
            <Rows rows={of(g)} onOpen={open} />
          </Section>
        ))}
        {archived.length > 0 && (
          <Section title={`已收起 ${archived.length}`}>
            <Button variant="ghost" size="sm" onClick={() => setShowArchived(!showArchived)}>{showArchived ? "隐藏" : "显示"}</Button>
            {showArchived && <Rows rows={archived} onOpen={open} />}
          </Section>
        )}
      </div>
    </div>
  )
}
