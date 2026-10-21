// 设置 → AI (spec §10.1): which provider and model each task uses, the day and month budget, the price list the
// cost is counted with, a test call, and the latest calls. AI is optional: without it everything else works.
import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { NumberField } from "@/components/NumberField"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { type AIConfig, aiEndpoints, type AITask, useAICalls, useAIConfig, useAIStatus } from "@/lib/ai"
import { usePutSetting, useSettings } from "@/lib/api"
import { usePASettings } from "@/lib/pa"
import { cn } from "@/lib/utils"
import { Parts } from "../common"
import { PATaskRow } from "./PATaskRow"
import { TaskControls } from "./TaskControls"

const TASK_HINT: Record<AITask, string> = {
  rule_assistant: "把你的描述起草成规则、提出澄清问题、按你的话改规则（用强一些的模型）",
  research: "可以用便宜一些的模型",
}
// DeepSeek's models since V4 (deepseek-chat / deepseek-reasoner stopped on 2026-07-24), and the strengths its API takes
const DEEPSEEK_MODELS = ["deepseek-flash", "deepseek-v4-pro"]
const yuan = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `¥${x < 0.01 && x > 0 ? x.toFixed(4) : x.toFixed(2)}`)

/** 每天自动研究 (spec §10.3): saved on leaving the field; an emptied field changes nothing (it is not 0). */
function DailyResearch() {
  const { data: settings } = useSettings()
  const put = usePutSetting()
  if (!settings) return null
  const n = settings["research.daily"]
  return (
    <div className="flex flex-wrap items-center gap-3">
      <label htmlFor="research-daily" className="w-24 font-medium">自动研究</label>
      <span className="flex items-center gap-2">每个市场每天最多
        <NumberField id="research-daily" commitOn="blur" rule={{ int: true, min: 0, max: 10 }} className="h-8 w-16" aria-label="每天自动研究的股票数"
          value={n} onChange={(v) => put.mutate({ key: "research.daily", value: v! }, { onError: (err) => toast.error(String(err)) })} />只</span>
      <span className="text-[0.8125rem] text-subtle-foreground">收盘扫描后研究当天新出信号的股票，0 = 不自动；花的是上面的预算，超出就停</span>
    </div>
  )
}

export function AISettings() {
  const qc = useQueryClient()
  const { data: status } = useAIStatus()
  const { data: saved } = useAIConfig()
  const { data: calls = [] } = useAICalls()
  const { data: pa } = usePASettings()
  const [draft, setDraft] = useState<AIConfig | null>(null)
  const [testing, setTesting] = useState<AITask | null>(null)
  const [wrong, setWrong] = useState<Record<string, string>>({})   // a field holding a number it cannot take
  const note = (label: string) => (error: string | null) => setWrong((w) => {
    const next = { ...w }
    if (error) next[label] = error
    else delete next[label]
    return next
  })
  const cfg = draft ?? saved
  if (!cfg || !status) return null
  const edit = (f: (c: AIConfig) => AIConfig) => setDraft(f(structuredClone(cfg)))
  // every DeepSeek model a task uses has a row, so a new one can be priced here (unpriced, the budget could not hold it)
  const used = [...Object.values(cfg.tasks), ...(pa ? [pa.task] : [])]   // 价格行为's model too: unpriced, it would not run
  const priced = [...new Set([...Object.keys(cfg.prices), ...used.filter((x) => x.provider === "deepseek" && x.model.trim()).map((x) => x.model.trim())])]
  const save = async () => {
    try {
      const c = await aiEndpoints.save(cfg)
      qc.setQueryData(["ai", "settings"], c)
      setDraft(null)
      await qc.invalidateQueries({ queryKey: ["ai"] })
      toast.success("AI 设置已保存")
    } catch (e) {
      toast.error(String(e))
    }
  }
  const test = async (task: AITask) => {
    setTesting(task)
    try {
      const r = await aiEndpoints.test(task)
      toast.success(`连接正常：${r.model}，${(r.latency_ms / 1000).toFixed(1)} 秒，${yuan(r.cost)}`)
    } catch (e) {
      toast.error(`连不上：${String(e)}`)
    } finally {
      setTesting(null)
      void qc.invalidateQueries({ queryKey: ["ai"] })
    }
  }
  return (
    <div className="flex flex-col gap-4 text-[0.875rem]" data-testid="ai-settings">
      <datalist id="deepseek-models">{DEEPSEEK_MODELS.map((m) => <option key={m} value={m} />)}</datalist>
      {(Object.keys(cfg.tasks) as AITask[]).map((t) => {
        const st = status.tasks[t]
        return (
          // a grid, not a wrapping row: in a narrow window the button stays at the end and the lines under the controls
          // stay under them, never one at the left edge and one indented
          <div key={t} className="grid grid-cols-[6rem_minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 border-b border-border/60 pb-3">
            <div className="pt-1.5 font-medium">{st.name}</div>
            <TaskControls name={st.name} value={cfg.tasks[t]} onChange={(v) => edit((c) => ({ ...c, tasks: { ...c.tasks, [t]: v } }))} />
            <Button size="sm" variant="outline" disabled={testing !== null || draft !== null || !st.key} onClick={() => void test(t)}>
              {testing === t ? "测试中…" : "测试连接"}
            </Button>
            <div className="col-span-2 col-start-2 flex flex-col gap-0.5 text-[0.8125rem] text-subtle-foreground">
              <span className={cn(st.ready && "text-down")}>{st.ready ? "可以用" : st.reason}</span>
              <span>{TASK_HINT[t]}{cfg.tasks[t].provider === "openrouter" ? "；OpenRouter 只路由到支持严格 JSON Schema 输出的服务商，模型不支持思考时选「默认」" : "；DeepSeek 用 JSON 输出，结果在本地再校验一遍"}</span>
            </div>
          </div>
        )
      })}
      <PATaskRow />
      <div className="flex flex-wrap items-center gap-3">
        <div className="w-24 font-medium">预算</div>
        <label className="flex items-center gap-2">每天 ¥<NumberField aria-label="每天的 AI 预算" rule={{ min: 0 }} value={cfg.day_cap} className="h-8 w-24"
          onValidity={note("每天的预算")} onChange={(v) => edit((c) => ({ ...c, day_cap: v! }))} /></label>
        <label className="flex items-center gap-2">每月 ¥<NumberField aria-label="每月的 AI 预算" rule={{ min: 0 }} value={cfg.month_cap} className="h-8 w-24"
          onValidity={note("每月的预算")} onChange={(v) => edit((c) => ({ ...c, month_cap: v! }))} /></label>
        <span className="num text-muted-foreground">今天已用 {yuan(status.spent.day)} · 本月已用 {yuan(status.spent.month)}</span>
      </div>
      <DailyResearch />
      <div className="flex flex-wrap items-start gap-3">
        <div className="w-24 font-medium">价格</div>
        <div className="flex flex-col gap-1.5">
          {priced.map((m) => {
            const p = cfg.prices[m] ?? { input: 0, cached: null, output: 0 }
            const set = (k: "input" | "cached" | "output", v: number | null) =>
              edit((c) => ({ ...c, prices: { ...c.prices, [m]: { ...p, [k]: v } } }))
            return (
              <div key={m} className="num flex flex-wrap items-center gap-2 text-muted-foreground">
                <span className="w-36 shrink-0 whitespace-nowrap">{m}</span>
                输入 ¥<NumberField aria-label={`${m} 输入价格`} rule={{ min: 0 }} value={p.input} className="h-7 w-20"
                  onValidity={note(`${m} 的输入价格`)} onChange={(v) => set("input", v)} />
                命中缓存 ¥<NumberField aria-label={`${m} 命中缓存的输入价格`} rule={{ min: 0, optional: true }} value={p.cached} placeholder="同输入"
                  className="h-7 w-20" onValidity={note(`${m} 的缓存价格`)} onChange={(v) => set("cached", v)} />
                输出 ¥<NumberField aria-label={`${m} 输出价格`} rule={{ min: 0 }} value={p.output} className="h-7 w-20"
                  onValidity={note(`${m} 的输出价格`)} onChange={(v) => set("output", v)} />
                <span className="text-[0.8125rem]">/ 百万 token</span>
              </div>
            )
          })}
          <div className="text-[0.8125rem] text-subtle-foreground">DeepSeek 按这里的价格算费用（填高峰价；北京时间工作日 9–12 点、14–18 点以外，以及周末和法定节假日，按半价算；思考的 token 按输出计）。
            价格变了请改。OpenRouter 用它自己报的费用，按 1 美元 = ¥{cfg.usd_cny} 折算。</div>
        </div>
      </div>
      {draft && (
        <div className="flex items-center gap-2 pl-24">
          <Button size="sm" onClick={() => void save()} disabled={Object.keys(wrong).length > 0}
            title={Object.entries(wrong).map(([k, e]) => `${k}${e}`)[0]}>保存 AI 设置</Button>
          <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>撤销</Button>
        </div>
      )}
      {calls.length > 0 && (
        <div>
          <div className="pb-1 font-medium">最近的调用</div>
          <Table>
            <TableHeader><TableRow><TableHead>时间</TableHead><TableHead>任务</TableHead><TableHead>模型</TableHead><TableHead className="whitespace-normal"><Parts sep="" parts={["token", "（入 / 出）"]} /></TableHead><TableHead>费用</TableHead><TableHead>结果</TableHead></TableRow></TableHeader>
            <TableBody>
              {calls.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="num">{c.created_at.replace("T", " ").slice(5, 16)}</TableCell>
                  <TableCell>{status.tasks[c.task as AITask]?.name ?? (c.task === "price_action" ? "价格行为" : c.task)}</TableCell>
                  <TableCell className="num">{c.model}</TableCell>
                  <TableCell className="num whitespace-normal"><Parts sep=" / " parts={[c.tokens_in ?? "—", c.tokens_out ?? "—"]} /></TableCell>
                  <TableCell className="num">{yuan(c.cost)}</TableCell>
                  {/* an error wraps, in a column wide enough to read it */}
                  <TableCell className={cn(c.ok ? "text-muted-foreground" : "whitespace-normal text-error")}>{c.ok ? "成功" : <div className="min-w-[10rem]">{c.error}</div>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
