// AI 起草与澄清 (spec §6.1–§6.4, design 05): your description → a rule the program has checked and reads back; the
// assumptions the model made are cards whose options you pick (each with a small preview on the same stock), the
// phrases it could not compute need your decision (a stand-in, or drop it), and one sentence changes the rule —
// the whole new rule comes back and the difference is shown before you take it. Picking an option never calls AI.
// "去预览" opens it as a new rule in the workspace (单股调试、样例图集…), where saving makes it a version.
import { useQuery } from "@tanstack/react-query"
import { ChevronRight, CircleAlert, CircleCheck, Sparkles } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { Link, useNavigate } from "react-router"
import { toast } from "sonner"
import { AIFailure } from "@/components/rules/AIEdit"
import { DiffView } from "@/components/rules/DiffView"
import { MiniChart } from "@/components/rules/MiniChart"
import { Understanding } from "@/components/rules/Understanding"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { type AIFailed, aiEndpoints, DRAFT_INPUT, type Draft, failureOf, type StandIn, useAIStatus } from "@/lib/ai"
import type { Market } from "@/lib/api"
import { ruleEndpoints, type Understanding as U } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { applyParams } from "./rules/ruleText"

/** Where a proxy condition goes, as 我的理解 says it (a stage's 那一根 / 期间 / 每一根 / 至少一根). */
const WHERE: Record<string, string> = { at: "那一根", during: "期间", all: "每一根都满足", any: "至少一根满足" }

const KEY = "rule.draft"

interface State {
  description: string
  draft: Draft | null                     // as the model gave it (checked)
  text: string | null; understanding: U | null // after your picks and stand-ins
  picks: Record<string, string>           // assumption id → the value you picked
  decided: Record<number, number | "drop"> // unmapped phrase → the stand-in you took, or dropped
}
const EMPTY: State = { description: "", draft: null, text: null, understanding: null, picks: {}, decided: {} }

function load(): State {
  try {
    const s = JSON.parse(sessionStorage.getItem(KEY) ?? "null") as State | null
    const input = sessionStorage.getItem(DRAFT_INPUT)
    if (input !== null) {
      sessionStorage.removeItem(DRAFT_INPUT)
      return { ...EMPTY, description: input }
    }
    return s ?? EMPTY
  } catch {
    return EMPTY
  }
}
const STOP = "不等这次的结果了：可以改了再试。模型那边已经开始的这一次还会算完，用量照算"
const yuan = (x: number | null) => (x === null ? "费用未知" : `约 ¥${x < 0.01 ? x.toFixed(4) : x.toFixed(2)}`)

/** A stock and day the draft hits in a small sample: where each option's preview is drawn. */
function usePreviewSpot(text: string | null, markets: Market[]) {
  return useQuery({
    queryKey: ["draft-spot", text], enabled: !!text, staleTime: Infinity, retry: false,
    queryFn: async () => {
      for (const m of markets) {
        const g = await ruleEndpoints.gallery({ text: text! }, m, { n: 60, seed: 0 }).catch(() => null)
        const h = g?.hits.at(-1)
        if (h) return { listingId: h.listing_id, day: h.signal_date, market: m }
      }
      return null
    },
  })
}

/** Descriptions to start from, in plain words (养箱). */
const EXAMPLES: [string, string][] = [
  ["上涨后横盘突破", "股价先有一波 20% 以上的上涨，然后横盘 15~60 天形成箱体，振幅不超过 15%，期间明显缩量；之后某天放量（量比 > 2）收盘突破箱体上沿，突破后 3 天不跌回箱体。"],
  ["涨停后养箱", "最近两个月里有过涨停；之后横盘形成中箱，箱体期间缩量；某天放量收盘站上箱体上沿。"],
  ["成交量突然放大", "今天的成交量是昨天的 2 倍以上，收阳线，收盘在 20 日均线上方。"],
]

export default function RuleDraftPage() {
  const navigate = useNavigate()
  const { data: ai } = useAIStatus()
  const [st, setSt] = useState<State>(load)
  const [busy, setBusy] = useState<"draft" | "edit" | null>(null)
  const waiting = useRef<AbortController | null>(null)   // 停止: the page stops waiting for the model
  const [failure, setFailure] = useState<AIFailed | null>(null)
  const [editFailure, setEditFailure] = useState<AIFailed | null>(null)
  const [showYaml, setShowYaml] = useState(false)
  const [instruction, setInstruction] = useState("")
  const [proposal, setProposal] = useState<Draft | null>(null)
  useEffect(() => { sessionStorage.setItem(KEY, JSON.stringify(st)) }, [st])
  const ready = ai?.tasks.rule_assistant.ready ?? false
  const d = st.draft
  const spot = usePreviewSpot(d?.rule_text ?? null, d?.markets ?? ["CN", "JP"])

  const run = async () => {
    const c = (waiting.current = new AbortController())
    setBusy("draft")
    setFailure(null)
    setEditFailure(null)
    setProposal(null)   // a proposal was a change to the old draft
    try {
      const r = await aiEndpoints.draft(st.description, c.signal)
      setSt((s) => ({ ...s, draft: r, text: r.rule_text, understanding: r.understanding, picks: {}, decided: {} }))
    } catch (e) {
      if (!c.signal.aborted) setFailure(failureOf(e))   // stopped: not a failure
    } finally {
      setBusy(null)
    }
  }
  const stop = () => waiting.current?.abort()
  /** The model's rule (`base`) with your picks and stand-ins, applied by the program (no AI). */
  const recompute = async (picks: State["picks"], decided: State["decided"], base = d?.rule_text) => {
    if (!d || !base) return
    const changes = d.assumptions.filter((a) => picks[a.id] !== undefined && picks[a.id] !== a.chosen).map((a) => ({ path: a.path, value: picks[a.id] }))
    const add: StandIn[] = d.unmapped.flatMap((u, i) => (typeof decided[i] === "number" ? [u.proxies[decided[i] as number]] : []))
    try {
      const r = await aiEndpoints.apply(base, changes, add)
      setSt((s) => ({ ...s, draft: s.draft && { ...s.draft, rule_text: base }, picks, decided, text: r.text, understanding: r.understanding }))
    } catch (e) {
      toast.error(`改不了：${String(e)}`)
    }
  }
  const askEdit = async () => {
    if (!st.text || !instruction.trim()) return
    const c = (waiting.current = new AbortController())
    setBusy("edit")
    setEditFailure(null)
    try {
      setProposal(await aiEndpoints.edit(st.text, instruction, undefined, c.signal))
    } catch (e) {
      if (!c.signal.aborted) setEditFailure(failureOf(e))
    } finally {
      setBusy(null)
    }
  }
  const take = (p: Draft) => {
    setSt((s) => ({ ...s, draft: p, text: p.rule_text, understanding: p.understanding, picks: {}, decided: {} }))
    setProposal(null)
    setInstruction("")
  }
  const setParam = (id: string, v: number | boolean) => {
    if (!d) return
    // a parameter chip changes the model's rule itself; your picks and stand-ins are applied on top again
    const base = applyParams(d.rule_text, { [id]: v })
    if (base.missing.length) toast.error(`参数 ${id} 在规则文字里没找到，请改 YAML`)
    else void recompute(st.picks, st.decided, base.text)
  }
  const pending = d ? d.unmapped.filter((_, i) => st.decided[i] === undefined).length : 0
  const confirmed = d ? d.assumptions.filter((a) => st.picks[a.id] !== undefined).length + d.unmapped.length - pending : 0
  const total = d ? d.assumptions.length + d.unmapped.length : 0
  const toPreview = () => {
    if (!st.text) return
    sessionStorage.setItem("rule.import", st.text)
    navigate("/rules/new?from=draft")
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="drag clear-window-buttons flex h-11 shrink-0 items-center gap-2 border-b border-border px-4">
        <Link to="/rules" className="no-drag text-[0.875rem] text-muted-foreground hover:text-foreground">规则库</Link>
        <ChevronRight className="size-3.5 text-subtle-foreground" />
        <h1 className="no-drag truncate text-[0.9375rem] font-semibold">{st.understanding?.summary.split(" · ")[0] ?? "AI 起草规则"}</h1>
        {d && <Badge variant="outline" className="h-5 px-1.5 text-[0.75rem] font-normal">草稿</Badge>}
        <div className="ml-3 flex items-center gap-1.5 text-[0.8125rem] text-subtle-foreground">
          {["描述", "澄清", "预览", "定版"].map((x, i) => (
            <span key={x} className={cn(i === (d ? (pending ? 1 : 2) : 0) && "text-foreground")}>{i + 1} {x}{i < 3 && <ChevronRight className="ml-1 inline size-3" />}</span>
          ))}
        </div>
        <span className="flex-1" />
        <Button size="sm" className="no-drag" disabled={!st.text || pending > 0} onClick={toPreview}><ChevronRight className="size-4" />去预览</Button>
      </header>
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col overflow-y-auto px-6 py-4">
          <div className="flex items-center justify-between pb-1 text-[0.8125rem] text-muted-foreground"><span>你的描述</span></div>
          <Textarea aria-label="你的选股描述" value={st.description} onChange={(e) => setSt((s) => ({ ...s, description: e.target.value }))} rows={4}
            placeholder="例如：股价先有一波 20% 以上的上涨，然后横盘 15~60 天形成箱体，振幅不超过 15%，期间明显缩量；之后某天放量（量比 > 2）收盘突破箱体上沿，突破后 3 天不跌回箱体。"
            className="resize-none text-[0.875rem] leading-6" />
          {!st.description.trim() && !d && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[0.8125rem] text-subtle-foreground">从一个例子开始：
              {EXAMPLES.map(([label, text]) => (
                <button key={label} type="button" title={text} onClick={() => setSt((s) => ({ ...s, description: text }))}
                  className="rounded-md border border-border px-2 py-0.5 hover:bg-accent hover:text-foreground">{label}</button>
              ))}
            </div>
          )}
          <div className="mt-2 flex items-center gap-3">
            <Button size="sm" disabled={!ready || busy !== null || !st.description.trim()} onClick={() => void run()}>
              <Sparkles className="size-4" />{busy === "draft" ? "起草中…（模型思考时要半分钟到几分钟）" : d ? "重新起草" : "起草规则"}
            </Button>
            {busy === "draft" && <Button size="sm" variant="ghost" onClick={stop} title={STOP}>停止</Button>}
            {ai && !ready && <span className="text-[0.8125rem] text-subtle-foreground">{ai.tasks.rule_assistant.reason} <Link className="underline" to="/settings?s=ai">去设置</Link></span>}
          </div>
          {failure && <AIFailure failure={failure} testId="draft-failure" />}
          {d && st.understanding && st.text && <>
            <div className="mt-6 flex items-center justify-between pb-2 text-[0.8125rem] text-muted-foreground">
              <span>系统的理解 <span className="text-subtle-foreground">由规则直接生成，和实际执行一致</span></span>
              <button type="button" className="hover:text-foreground" onClick={() => setShowYaml(!showYaml)}>{showYaml ? "看理解" : "查看 YAML"}</button>
            </div>
            {showYaml ? <Textarea aria-label="规则 YAML" readOnly value={st.text} className="num min-h-[22.5rem] font-mono text-[0.8125rem] leading-5" />
              : <Understanding u={st.understanding} onParam={setParam} />}
            <p className="mt-2 text-[0.8125rem] text-subtle-foreground">点蓝色参数直接改数值；要改含义，用下面这一句话告诉 AI。</p>
            {proposal ? (
              <div className="mt-4 rounded-md border border-border p-3 text-[0.8125rem]" data-testid="edit-proposal">
                <div className="pb-1 font-medium">AI 的修改：{proposal.note || "（没有说明）"}</div>
                <DiffView diff={proposal.diff ?? ""} />
                <div className="mt-2 flex gap-2">
                  <Button size="sm" onClick={() => take(proposal)}>用这个修改</Button>
                  <Button size="sm" variant="ghost" onClick={() => setProposal(null)}>不要</Button>
                </div>
              </div>
            ) : (
              <div className="mt-4 rounded-md border border-border p-3">
                <Input aria-label="用一句话修改规则" value={instruction} onChange={(e) => setInstruction(e.target.value)}
                  placeholder="用一句话修改规则，例如「箱体期间不能出现涨停」" className="border-0 px-0 text-[0.875rem] shadow-none focus-visible:ring-0"
                  onKeyDown={(e) => { if (e.key === "Enter") void askEdit() }} />
                <div className="mt-2 flex items-center text-[0.8125rem] text-subtle-foreground">
                  <span className="flex-1">{ai ? `${ai.tasks.rule_assistant.provider === "openrouter" ? "OpenRouter" : "DeepSeek"} · ${ai.tasks.rule_assistant.model}` : ""}</span>
                  {busy === "edit" && <Button size="sm" variant="ghost" onClick={stop} title={STOP}>停止</Button>}
                  <Button size="sm" variant="outline" disabled={!ready || busy !== null || !instruction.trim()} onClick={() => void askEdit()}>
                    {busy === "edit" ? "修改中…" : "生成修改"}
                  </Button>
                </div>
              </div>
            )}
            {editFailure && !proposal && <AIFailure failure={editFailure} testId="edit-failure" />}
            <div className="mt-2 text-[0.8125rem] text-subtle-foreground" data-testid="draft-meta">
              上次起草：{d.model} · {(d.latency_ms / 1000).toFixed(1)} 秒 · {yuan(d.cost)} · 规则检查通过{d.attempts > 1 ? `（改了 ${d.attempts - 1} 次）` : ""}
            </div>
          </>}
        </div>
        {d && (
          <aside className="w-[32.5rem] shrink-0 overflow-y-auto border-l border-border px-5 py-4 text-[0.875rem]" data-testid="clarify">
            <div className="pb-3"><span className="font-semibold">需要你确认</span> <span className="num text-[0.8125rem] text-subtle-foreground">已确认 {confirmed} / {total}</span></div>
            {total === 0 && <div className="text-[0.8125rem] text-subtle-foreground">AI 没有替你做选择，也没有算不了的说法。</div>}
            {d.assumptions.map((a) => {
              const picked = st.picks[a.id] ?? a.chosen
              return (
                <section key={a.id} className="border-b border-border/60 pb-4 pt-1" data-testid={`card-${a.id}`}>
                  <div className="flex items-center gap-2 pb-1">
                    {st.picks[a.id] !== undefined ? <CircleCheck className="size-4 text-structure" /> : <CircleAlert className="size-4 text-subtle-foreground" />}
                    <span className="flex-1 font-medium">{a.text}</span>
                    <span className="text-[0.8125rem] text-subtle-foreground">{st.picks[a.id] !== undefined ? "已确认" : "按 AI 的选择"}</span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 pt-1">
                    {a.options.map((o) => (
                      <button key={o.value} type="button" onClick={() => void recompute({ ...st.picks, [a.id]: o.value }, st.decided)}
                        className={cn("rounded-md border p-1.5 text-left", picked === o.value ? "border-ring" : "border-border hover:border-muted-foreground")}>
                        {spot.data && <OptionPreview base={d.rule_text} path={a.path} value={o.value} chosen={a.chosen} spot={spot.data} />}
                        <div className="pt-1 text-[0.8125rem]">{picked === o.value ? "◉" : "○"} {o.label}</div>
                      </button>
                    ))}
                  </div>
                  {!spot.data && spot.isFetched && <div className="pt-1 text-[0.75rem] text-subtle-foreground">样本里没找到命中，没有预览</div>}
                </section>
              )
            })}
            {d.unmapped.map((u, i) => (
              <section key={i} className="border-b border-border/60 pb-4 pt-3" data-testid={`unmapped-${i}`}>
                <div className="flex items-center gap-2 pb-1">
                  {st.decided[i] !== undefined ? <CircleCheck className="size-4 text-structure" /> : <CircleAlert className="size-4 text-warning" />}
                  <span className="flex-1 font-medium">「{u.phrase}」无法直接计算</span>
                  <span className={cn("text-[0.8125rem]", st.decided[i] === undefined ? "text-warning" : "text-subtle-foreground")}>{st.decided[i] === undefined ? "待决定" : "已决定"}</span>
                </div>
                <div className="pb-2 text-[0.8125rem] text-muted-foreground">{u.reason}</div>
                {u.proxies.map((p, k) => (
                  <label key={k} className="flex cursor-pointer items-start gap-2 py-1">
                    <input type="radio" name={`unmapped-${i}`} checked={st.decided[i] === k} onChange={() => void recompute(st.picks, { ...st.decided, [i]: k })} className="mt-1" />
                    <span>{p.label}<span className="block text-[0.75rem] text-subtle-foreground">{p.stage} · {WHERE[p.where] ?? p.where}：{p.expr}</span></span>
                  </label>
                ))}
                <label className="flex cursor-pointer items-start gap-2 py-1">
                  <input type="radio" name={`unmapped-${i}`} checked={st.decided[i] === "drop"} onChange={() => void recompute(st.picks, { ...st.decided, [i]: "drop" })} className="mt-1" />
                  <span>删除这个说法<span className="block text-[0.75rem] text-subtle-foreground">只保留其他条件</span></span>
                </label>
              </section>
            ))}
            {pending > 0 && <div className="mt-4 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-[0.8125rem] text-warning">还有 {pending} 项需要你决定，决定后才能去预览</div>}
          </aside>
        )}
      </div>
    </div>
  )
}

/** One option of a card drawn on the spot the draft hits: the model's rule with just this option applied. */
function OptionPreview({ base, path, value, chosen, spot }: {
  base: string; path: string; value: string; chosen: string; spot: { listingId: string; day: string; market: Market }
}) {
  const q = useQuery({
    queryKey: ["option-text", base, path, value], enabled: value !== chosen, staleTime: Infinity, retry: false,
    queryFn: async () => (await aiEndpoints.apply(base, [{ path, value }], [])).text,
  })
  const rule = value === chosen ? base : q.data
  if (!rule) return <div className="h-[9.375rem]" />
  return <MiniChart ruleRef={{ text: rule }} listingId={spot.listingId} day={spot.day} ids={[]} digits={spot.market === "JP" ? 1 : 2} />
}
