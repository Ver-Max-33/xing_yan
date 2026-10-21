// 规则库 (spec §5.2, §5.9, §6.6, §7; design 16): your rules and the built-in screening plans,
// with their version, markets, your Y/N verdicts on their hits, when they last changed and their latest
// market run. Enable a rule for the daily scan, put one away, copy a built-in to change it, import or
// export YAML. AI drafting and the box templates come with the AI rule assistant (P3).
import { useQueryClient } from "@tanstack/react-query"
import { Download, FileUp, MoreHorizontal, Plus, Sparkles } from "lucide-react"
import { useRef, useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Market } from "@/lib/api"
import { ruleEndpoints, type RuleSummary, type RunRecord, useRules, useRuns, useTemplates } from "@/lib/rules"
import { copyText, downloadText } from "./rules/ruleText"
import { DRAFT_INPUT, useAIStatus } from "@/lib/ai"
import { Empty, PageHeader, Parts, Section } from "./common"
import { MARKET_NAME, marketNames } from "@/lib/format"
import { cn, readText } from "@/lib/utils"

/** The latest daily run of each rule in each market. */
function latestRuns(runs: RunRecord[]): Map<string, RunRecord[]> {
  const out = new Map<string, RunRecord[]>()
  for (const r of runs) {
    if (r.mode !== "date") continue
    const mine = out.get(r.rule_id) ?? []
    if (!mine.some((x) => x.market === r.market)) out.set(r.rule_id, [...mine, r])
  }
  return out
}

function RuleRows({ rules, runs, onOpen, notes }: {
  rules: RuleSummary[]; runs: Map<string, RunRecord[]>; onOpen: (id: string) => void
  notes?: Map<string, string> // a line under the name (a template's shape and basis)
}) {
  const qc = useQueryClient()
  const [running, setRunning] = useState<string | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ["rules"] })
  const setState = async (r: RuleSummary, state: { enabled?: boolean; archived?: boolean }) => {
    try {
      await ruleEndpoints.setState(r.id, state)
      await refresh()
    } catch (e) {
      toast.error(`没改成：${String(e)}`)
    }
  }
  const run = async (r: RuleSummary, market: Market) => {
    setRunning(`${r.id}|${market}`)
    try {
      const s = await ruleEndpoints.run({ rule_id: r.id }, market)
      toast(`${r.name} · ${MARKET_NAME[market]} ${s.day}：信号 ${s.counts.signal ?? 0}、潜伏 ${s.counts.watch ?? 0}、无法判断 ${s.counts.unknown ?? 0}`)
      await qc.invalidateQueries({ queryKey: ["runs"] })
    } catch (e) {
      toast.error(`运行失败：${String(e)}`)
    } finally {
      setRunning(null)
    }
  }
  const exportYaml = async (r: RuleSummary) => {
    try {
      const doc = await ruleEndpoints.rule(r.id)
      downloadText(`${r.id}.yaml`, doc.text)
    } catch (e) {
      toast.error(`导出失败：${String(e)}`)
    }
  }
  const copy = async (r: RuleSummary) => {
    try {
      const doc = await ruleEndpoints.rule(r.id)
      sessionStorage.setItem("rule.import", copyText(doc.text))
      onOpen(`new?from=${encodeURIComponent(r.id)}`)
    } catch (e) {
      toast.error(`复制失败：${String(e)}`)
    }
  }
  return (
    // narrow (a small window, 大 / 特大 text) 版本, 更新 and 简报 give way so the name keeps room — 简报 is in each row's menu
    <div className="@container">
    <Table className="table-fixed">
      <TableHeader>
        <TableRow>
          {/* the fixed columns take what their content needs; the name takes the rest */}
          <TableHead>名称</TableHead><TableHead className={cn("w-14", NARROW)}>版本</TableHead><TableHead className="w-[6.5rem]">市场</TableHead>
          <TableHead className="w-24">你的判断</TableHead><TableHead className={cn("w-14", NARROW)}>更新</TableHead><TableHead className="w-[12.5rem]">最近运行</TableHead>
          <TableHead className="w-14">启用</TableHead><TableHead className="w-12" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {rules.map((r) => {
          const last = runs.get(r.id) ?? []
          return (
            <TableRow key={r.id} className="cursor-pointer" onClick={() => onOpen(r.id)} data-testid={`rule-${r.id}`}>
              <TableCell>
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium" title={r.doc ? `${r.name}\n${r.doc}` : r.name}>{r.name}</span>
                  {r.builtin && <Badge variant="outline" className="h-5 px-1.5 text-[0.75rem] font-normal text-muted-foreground">内置</Badge>}
                </div>
                {notes?.get(r.id) && <div className="mt-0.5 truncate text-[0.8125rem] text-subtle-foreground" title={notes.get(r.id)}>{notes.get(r.id)}</div>}
                {r.error && <div className="mt-0.5 truncate text-[0.8125rem] text-error" title={r.error}>{r.error}</div>}
                {r.problem && <div className="mt-0.5 truncate text-[0.8125rem] text-error" title={r.problem} data-testid="rule-problem">
                  跑不了：{r.problem.split("\n")[0]}</div>}
              </TableCell>
              <TableCell className={NARROW}><Badge variant="outline" className="num h-5 px-1.5 text-[0.75rem] font-normal">v{r.version}</Badge></TableCell>
              <TableCell className="num text-muted-foreground">{marketNames(r.markets) || "—"}</TableCell>
              <TableCell className="num text-muted-foreground">
                {r.verdicts.right + r.verdicts.wrong ? `对 ${r.verdicts.right} · 错 ${r.verdicts.wrong}` : "—"}
              </TableCell>
              <TableCell className={cn("num text-muted-foreground", NARROW)}>{r.updated_at ? r.updated_at.slice(5, 10) : "—"}</TableCell>
              <TableCell className="num text-[0.8125rem] whitespace-normal text-muted-foreground">
                {/* a line breaks between parts when the counts are long (4,290 潜伏) or the column narrow */}
                {last.length ? last.map((x) => (
                  <div key={x.market}><Parts sep=" · " parts={[`${MARKET_NAME[x.market]} ${x.day?.slice(5) ?? ""}`, `信号 ${x.counts.signal ?? 0}`, `潜伏 ${x.counts.watch ?? 0}`]} /></div>
                )) : "还没运行过"}
              </TableCell>
              <TableCell onClick={(e) => e.stopPropagation()}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span><Switch checked={r.enabled} disabled={!!r.error || (!!r.problem && !r.enabled)} aria-label={`启用 ${r.name}`}
                      onCheckedChange={(v) => void setState(r, { enabled: v })} /></span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">启用的规则列在侧栏；每天收盘后自动扫描它，变化进今日页（和飞书），潜伏名单在盯盘页盯着</TooltipContent>
                </Tooltip>
              </TableCell>
              <TableCell onClick={(e) => e.stopPropagation()}>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon-sm" aria-label={`${r.name} 的更多操作`}><MoreHorizontal className="size-4" /></Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onSelect={() => onOpen(r.id)}>打开</DropdownMenuItem>
                    {r.markets.map((m) => (
                      <DropdownMenuItem key={m} disabled={!!r.error || running !== null} onSelect={() => void run(r, m)}>
                        {running === `${r.id}|${m}` ? `${MARKET_NAME[m]}运行中…` : `在${/^[A-Za-z]/.test(MARKET_NAME[m]) ? ` ${MARKET_NAME[m]}` : MARKET_NAME[m]}全市场运行`}
                      </DropdownMenuItem>
                    ))}
                    <DropdownMenuSeparator />
                    <DropdownMenuItem disabled={!!r.error} onSelect={() => void copy(r)}>复制为新规则</DropdownMenuItem>
                    <DropdownMenuItem disabled={!!r.error} onSelect={() => void exportYaml(r)}><Download className="size-4" />导出 YAML</DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => void setState(r, { archived: !r.archived })}>{r.archived ? "放回规则库" : "收起"}</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
    </div>
  )
}

const NARROW = "@max-[61rem]:hidden"   // the columns that give way to the name in a narrow table

export default function RulesPage() {
  const navigate = useNavigate()
  const { data: rules = [], isLoading } = useRules()
  const { data: runs = [] } = useRuns(undefined, 500)
  const [showArchived, setShowArchived] = useState(false)
  const [describe, setDescribe] = useState("")
  const { data: ai } = useAIStatus()
  const file = useRef<HTMLInputElement>(null)
  const last = latestRuns(runs)
  const open = (id: string) => navigate(`/rules/${id}`)
  const mine = rules.filter((r) => !r.builtin && !r.archived)
  const builtin = rules.filter((r) => r.builtin && r.group === "builtin" && !r.archived)
  const { data: cards = [] } = useTemplates()
  const notes = new Map(cards.map((c) => [c.id, `${c.shape}（${c.basis}）`]))
  const boxTemplates = rules.filter((r) => r.group === "box" && !r.archived)
  const generalTemplates = rules.filter((r) => r.group === "general" && !r.archived)
  const archived = rules.filter((r) => r.archived)
  const importYaml = async (f: File | undefined) => {
    if (!f) return
    sessionStorage.setItem("rule.import", await readText(f))
    navigate("/rules/new?from=import")
  }
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="规则库">
        <input ref={file} type="file" accept=".yaml,.yml,.json,.txt" className="hidden" aria-label="选择 YAML 文件"
          onChange={(e) => { void importYaml(e.target.files?.[0]); e.target.value = "" }} />
        <Button variant="outline" size="sm" onClick={() => file.current?.click()}><FileUp className="size-4" />导入 YAML</Button>
        <Button size="sm" onClick={() => navigate("/rules/new")}><Plus className="size-4" />新建规则</Button>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto px-6">
        <Section title="用一句话描述你的选股流程" hint={ai && !ai.tasks.rule_assistant.ready ? `AI 起草要先配置：${ai.tasks.rule_assistant.reason}` : "AI 起草成规则，不确定的地方会问你；也可以点右上角「新建规则」直接写 YAML，或者复制一条内置方案、模板来改。"}>
          <form className="flex items-center gap-3 rounded-lg border border-dashed border-border px-4 py-3 text-[0.875rem]"
            onSubmit={(e) => { e.preventDefault(); sessionStorage.setItem(DRAFT_INPUT, describe); navigate("/rules/draft") }}>
            <Sparkles className="size-4 text-subtle-foreground" strokeWidth={1.75} />
            <input aria-label="用一句话描述你的选股流程" value={describe} onChange={(e) => setDescribe(e.target.value)}
              placeholder="例如：前面涨过一波，然后横盘一两个月、越走越缩量，某天放量收盘站上平台上沿……"
              className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-subtle-foreground" />
            <Button size="sm" type="submit" disabled={!describe.trim()}>AI 起草</Button>
          </form>
        </Section>
        <Section title={`我的规则 ${mine.length || ""}`.trim()}>
          {mine.length ? <RuleRows rules={mine} runs={last} onOpen={open} />
            : <Empty>{isLoading ? "…" : "还没有自己的规则。新建一条，或者从下面的内置方案复制一条来改。"}</Empty>}
        </Section>
        <Section title="内置选股公式" hint="内置的选股公式，宽松版另列；一键运行，复制后可以改。">
          <RuleRows rules={builtin} runs={last} onOpen={open} />
        </Section>
        <Section title="箱体模板" hint="按箱体做法转写，没定死的数字都是参数；可以直接运行，复制后改成自己的。它们也是 AI 起草规则时的示例。">
          <RuleRows rules={boxTemplates} runs={last} onOpen={open} notes={notes} />
        </Section>
        <Section title="通用模板" hint="常见形态，转写自开源项目的策略说明。">
          <RuleRows rules={generalTemplates} runs={last} onOpen={open} notes={notes} />
        </Section>
        {archived.length > 0 && (
          <Section title={`已收起 ${archived.length}`}>
            <Button variant="ghost" size="sm" onClick={() => setShowArchived(!showArchived)}>{showArchived ? "隐藏" : "显示"}</Button>
            {showArchived && <RuleRows rules={archived} runs={last} onOpen={open} />}
          </Section>
        )}
      </div>
    </div>
  )
}
