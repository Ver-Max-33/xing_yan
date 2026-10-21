// 个股研究 on the K 线 page (spec §10.3, design 09). 资料: the evidence a research is given, as of the latest day, with the
// sources kept from AI named. AI 研究: the latest research (or one from the stock's history) — the summary, what supports
// and what argues against with the facts each rests on (an inference says so), what to check, what would show it wrong
// and when to look again, how it relates to the last research and what changed in the evidence since. A research marked
// 需复核 says why before anything else and is not read as a conclusion. The documents you import for the stock are kept
// here too; a research's citation of one opens the original text at its page (日文资料：一键跳回原文位置).
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { FileUp, Loader2, RefreshCw, ShieldAlert, ShieldCheck, StickyNote, Trash2 } from "lucide-react"
import { createContext, useContext, useMemo, useRef, useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { desktop } from "@/app/platform"
import { ask } from "@/lib/ask"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useAIStatus } from "@/lib/ai"
import { shortDate } from "@/lib/format"
import type { PAAnalysis } from "@/lib/pa"
import {
  diffLine, factGroups, type Fact, irEndpoints, type Item, noteText, outcomeText, type Pack, type Research, researchEndpoints,
  useIrDocs, usePack, useResearchHistory, useRunResearch,
} from "@/lib/research"
import { cn } from "@/lib/utils"
import { useWatchChange, useWatchEntries, watchEndpoints } from "@/lib/watch"
import { CompanyProfile } from "./CompanyProfile"
import { PriceActionPanel } from "./PriceActionPanel"

const LEAN = { 支持: "支持的理由更强", 反对: "反对的理由更强", 相当: "两边相当" } as const
const RELATION = { 首次: "第一次研究这只股票", 同向: "和上次同向", 反手: "和上次反手", 失效: "上次的失效条件发生了" } as const

/** Short names for a pack's facts in its order (design 09): F1… for facts, D1… for documents (announcements, their
 *  excerpts, what you imported). */
function shortNames(pack: Pack): Map<string, string> {
  let f = 0, d = 0
  return new Map(pack.facts.map((x) => [x.id, ["ann", "doc", "ir"].includes(x.kind) ? `D${++d}` : `F${++f}`]))
}

/** Opens a document you imported at a page (its viewer lives in the panel). */
const OpenDoc = createContext<(doc: string, page: number) => void>(() => {})

const when = (iso: string) => `${iso.slice(5, 10)} ${iso.slice(11, 16)}`

function Ref({ fact, name }: { fact: Fact | undefined; name: string }) {
  const openDoc = useContext(OpenDoc)
  const open = fact?.url ? () => desktop.openExternal(fact.url!) : fact?.doc ? () => openDoc(fact.doc!, fact.page ?? 1) : null
  const chip = (
    <button type="button" disabled={!open} onClick={() => open?.()}
      className={cn("num rounded border border-border px-1 text-[0.75rem] text-muted-foreground", open && "hover:text-foreground")}>{name}</button>
  )
  return (
    <Tooltip>
      <TooltipTrigger asChild><span>{chip}</span></TooltipTrigger>
      <TooltipContent className="max-w-xs">{fact ? `${fact.text}（${fact.as_of}）${fact.url ? "；点一下打开原文" : fact.doc ? `；点一下看原文第 ${fact.page} 页` : ""}` : "证据包里没有这一条"}</TooltipContent>
    </Tooltip>
  )
}

/** A document you imported: its text page by page (the original's words at the cited place), and the excerpt a research
 *  gets — any passage of any page you choose. */
function DocViewer({ doc, page, onClose }: { doc: string; page: number; onClose: () => void }) {
  const qc = useQueryClient()
  const { data, error } = useQuery({ queryKey: ["ir", "doc", doc], queryFn: () => irEndpoints.one(doc) })
  const [at, setAt] = useState(page)
  const [edited, setEdited] = useState<string | null>(null)
  const text = edited ?? data?.doc.excerpt ?? ""
  const save = async () => {
    if (!data) return
    try {
      await irEndpoints.excerpt(doc, text, at)
      void qc.invalidateQueries({ queryKey: ["ir"] })
      void qc.invalidateQueries({ queryKey: ["research", "pack", data.doc.listing_id] })
      toast.success(`摘录改成第 ${at} 页的这段`)
    } catch (e) {
      toast.error(String(e))
    }
  }
  const n = data?.pages.length ?? 0
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl" data-testid="doc-viewer">
        <DialogHeader>
          <DialogTitle>{data?.doc.title ?? "…"}</DialogTitle>
          <DialogDescription>原文第 {at} / {n} 页（只有文字）。研究看的是下面的摘录，可以换成任何一页的任何一段。</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2 text-[0.8125rem]">
          <Button size="sm" variant="outline" className="h-7" disabled={at <= 1} onClick={() => setAt(at - 1)}>上一页</Button>
          <Button size="sm" variant="outline" className="h-7" disabled={at >= n} onClick={() => setAt(at + 1)}>下一页</Button>
        </div>
        <pre className="max-h-[40vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-border p-3 font-sans text-[0.875rem] leading-6"
          data-testid="doc-page">{error ? `读不出来：${String(error)}` : data?.pages[at - 1] ?? "读取…"}</pre>
        <Textarea aria-label="给研究的摘录" rows={4} maxLength={3000} value={text} onChange={(e) => setEdited(e.target.value)} />
        <div className="flex justify-end"><Button size="sm" onClick={() => void save()} disabled={!data || !text.trim()}>用作摘录（第 {at} 页）</Button></div>
      </DialogContent>
    </Dialog>
  )
}

/** The documents you imported for the stock: add one (PDF or text), see its original, delete it; each as the AI gets it
 *  (its D-number and the line sent). */
function Imported({ listingId, pack, names }: { listingId: string; pack: Pack; names: Map<string, string> }) {
  const qc = useQueryClient()
  const openDoc = useContext(OpenDoc)
  const { data = [] } = useIrDocs(listingId)
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const changed = () => {
    void qc.invalidateQueries({ queryKey: ["ir", listingId] })
    void qc.invalidateQueries({ queryKey: ["research", "pack", listingId] })
  }
  const add = async (file: File | undefined) => {
    if (!file) return
    if (file.size > 20_000_000) {   // the backend refuses it too, before reading it all
      toast.error(`「${file.name}」太大（${(file.size / 1e6).toFixed(0)} MB），最多 20 MB`)
      if (input.current) input.current.value = ""
      return
    }
    setBusy(true)
    try {
      const d = await irEndpoints.add(listingId, file)
      changed()
      openDoc(d.id, d.page)
    } catch (e) {
      toast.error(`导入不了：${String(e)}`)
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ""
    }
  }
  const remove = async (id: string, title: string) => {
    if (!(await ask({ title: `删除「${title}」？`, text: "之后的研究不再读到它；已经做过的研究里的引用还在。", action: "删除资料" }))) return
    await irEndpoints.remove(id).then(changed, (e) => toast.error(String(e)))
  }
  return (
    <div className="mt-3" data-testid="imported">
      <div className="flex items-center text-[0.8125rem] text-subtle-foreground">
        <span className="flex-1">你导入的资料（IR 资料、纪要；研究总会带上，不受数据使用开关影响）</span>
        <input ref={input} type="file" accept=".pdf,.txt,.md,application/pdf,text/plain" className="hidden" aria-label="导入资料文件"
          onChange={(e) => void add(e.target.files?.[0])} />
        <Button size="sm" variant="ghost" className="h-7 px-2" disabled={busy} onClick={() => input.current?.click()}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <FileUp className="size-3.5" />}导入</Button>
      </div>
      {data.map((d) => {
        const sent = pack.facts.find((f) => f.doc === d.id)   // what the AI gets (the pack comes back after a change)
        return (
        <div key={d.id} className="mt-1 flex gap-2 leading-5">
          <span className="num w-7 shrink-0 text-[0.75rem] text-subtle-foreground">{sent ? names.get(sent.id) : ""}</span>
          <button type="button" className="line-clamp-3 flex-1 text-left hover:underline" title={sent?.text} onClick={() => openDoc(d.id, d.page)}>
            {sent?.text ?? `你导入的资料《${d.title}》第 ${d.page} 页：${d.excerpt}`}</button>
          <button type="button" aria-label={`删除 ${d.title}`} className="text-subtle-foreground hover:text-error" onClick={() => void remove(d.id, d.title)}>
            <Trash2 className="size-3.5" /></button>
        </div>
        )
      })}
    </div>
  )
}

function Refs({ ids, pack, names }: { ids: string[]; pack: Pack; names: Map<string, string> }) {
  return <span className="flex shrink-0 flex-wrap justify-end gap-1">{ids.map((id) =>
    <Ref key={id} name={names.get(id) ?? id} fact={pack.facts.find((f) => f.id === id)} />)}</span>
}

function Items({ title, items, pack, names }: { title: string; items: Item[]; pack: Pack; names: Map<string, string> }) {
  if (!items.length) return null
  return (
    <div className="mt-3">
      <div className="text-[0.8125rem] text-subtle-foreground">{title} <span className="num">{items.length}</span></div>
      {items.map((i) => (
        <div key={i.text} className="mt-1.5 flex gap-2 text-[0.875rem] leading-5">
          <span className="flex-1">{i.inference && <span className="mr-1 rounded bg-muted px-1 text-[0.75rem] text-muted-foreground">推断</span>}{i.text}</span>
          <Refs ids={i.refs} pack={pack} names={names} />
        </div>
      ))}
    </div>
  )
}

function Evidence({ listingId, pack }: { listingId: string; pack: Pack | undefined }) {
  const [fetching, setFetching] = useState(false)
  const [fresh, setFresh] = useState<Pack | null>(null)
  const p = fresh?.listing_id === listingId ? fresh : pack
  if (!p) return <p className="py-6 text-center text-[0.8125rem] text-subtle-foreground">读取证据…</p>
  const names = shortNames(p)
  const latest = async () => {
    setFetching(true)
    try {
      setFresh(await researchEndpoints.pack(listingId, true))
    } catch (e) {
      toast.error(`取不到公告：${String(e)}`)
    } finally {
      setFetching(false)
    }
  }
  return (
    <div className="pb-3 text-[0.875rem]" data-testid="research-evidence">
      <p className="text-[0.8125rem] text-subtle-foreground">证据截至 {p.day}：研究时这些事实发给 AI，结论要标出依据。</p>
      {p.left_out.length > 0 && <p className="mt-1 text-[0.8125rem] text-warning">没有给 AI：{p.left_out.join("、")}
        （<Link to="/settings?s=data" className="underline">设置 → 数据使用</Link>）</p>}
      {p.market === "CN" && !p.demo && <Button variant="ghost" size="sm" className="mt-1 h-7 px-2" onClick={() => void latest()} disabled={fetching}>
        {fetching ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}取最新公告（巨潮资讯）</Button>}
      <Imported listingId={listingId} pack={p} names={names} />
      {factGroups(p.facts).filter((g) => g.label !== "你导入的资料").map((g) => (
        <div key={g.label} className="mt-3">
          <div className="text-[0.8125rem] text-subtle-foreground">{g.label}</div>
          {g.facts.map((f) => (
            <div key={f.id} className="mt-1 flex gap-2 leading-5">
              <span className="num w-7 shrink-0 text-[0.75rem] text-subtle-foreground">{names.get(f.id)}</span>
              {f.url ? <button type="button" className="flex-1 text-left hover:underline" onClick={() => desktop.openExternal(f.url!)}>{f.text}</button>
                : <span className="flex-1">{f.text}</span>}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

function Answer({ r, older }: { r: Research; older: Research | undefined }) {
  const names = useMemo(() => shortNames(r.pack), [r.pack])
  const o = r.output
  const cites = [...o.support, ...o.against, ...o.invalidation].reduce((n, i) => n + i.refs.length, 0) + o.continuity.refs.length
  return (
    <div className="pb-3" data-testid="research-answer">
      <div className="flex items-center gap-2 text-[0.875rem]">
        <span className="flex-1 font-medium">{when(r.created_at)} 生成<span className="ml-1 font-normal text-subtle-foreground">（证据截至 {shortDate(r.day)}）</span></span>
        {r.status === "ok"
          ? <span className="flex items-center gap-1 text-[0.8125rem] text-muted-foreground"><ShieldCheck className="size-3.5" />{cites} 处引用都核对过</span>
          : <span className="flex items-center gap-1 text-[0.8125rem] text-warning"><ShieldAlert className="size-3.5" />需复核</span>}
      </div>
      <div className="mt-0.5 text-[0.8125rem] text-subtle-foreground">
        {r.model ?? "AI"} · 证据包 {r.pack.facts.length} 条{r.cost != null ? ` · 约 ¥${r.cost.toFixed(2)}` : ""}{r.trigger === "daily" ? " · 每日自动" : ""}{r.demo ? " · 演示数据" : ""}
      </div>
      {r.status !== "ok" && (
        <div className="mt-2 rounded-md border border-warning/50 px-3 py-2 text-[0.8125rem]" data-testid="research-flags">
          <div className="text-warning">这次的回答没通过核对，下面的内容不当作结论：</div>
          {r.flags.map((f) => <div key={f} className="mt-1 text-muted-foreground">· {f}</div>)}
        </div>
      )}
      <div className="mt-3 text-[0.8125rem] text-subtle-foreground">摘要</div>
      <p className="mt-1 text-[0.875rem] leading-6">{o.summary}</p>
      <div className="mt-2 text-[0.8125rem] text-muted-foreground">倾向：{o.lean}（{LEAN[o.lean]}；是对证据的概括，不是买卖建议）</div>
      <div className="mt-1 flex gap-2 text-[0.8125rem] text-muted-foreground">
        <span className="flex-1">{RELATION[o.continuity.verdict]}{older && o.continuity.verdict !== "首次" ? `（${shortDate(older.day)}）` : ""}
          {o.continuity.text ? `：${o.continuity.text}` : ""}</span>
        <Refs ids={o.continuity.refs} pack={r.pack} names={names} />
      </div>
      <Items title="支持" items={o.support} pack={r.pack} names={names} />
      <Items title="反对" items={o.against} pack={r.pack} names={names} />
      {o.to_verify.length > 0 && (
        <div className="mt-3">
          <div className="text-[0.8125rem] text-subtle-foreground">待核实 <span className="num">{o.to_verify.length}</span></div>
          {o.to_verify.map((t) => <div key={t} className="mt-1.5 text-[0.875rem] leading-5">{t}</div>)}
        </div>
      )}
      {(o.invalidation.length > 0 || o.next_review.length > 0) && (
        <div className="mt-3 rounded-md border border-border px-3 py-2">
          <div className="text-[0.8125rem] text-subtle-foreground">失效条件与下次复核</div>
          {o.invalidation.map((i) => (
            <div key={i.text} className="mt-1.5 flex gap-2 text-[0.875rem] leading-5">
              <span className="flex-1">· {i.inference && <span className="mr-1 text-[0.75rem] text-muted-foreground">（推断）</span>}{i.text}</span>
              <Refs ids={i.refs} pack={r.pack} names={names} />
            </div>
          ))}
          {o.next_review.map((t) => <div key={t} className="mt-1.5 text-[0.875rem] leading-5">· {t}</div>)}
        </div>
      )}
      {older && r.diff && (
        <details className="mt-3 text-[0.8125rem] text-muted-foreground">
          <summary className="cursor-pointer">和 {shortDate(older.day)} 的研究相比：{diffLine(r.diff)}</summary>
          {r.diff.added.map((f) => <div key={f.id} className="mt-1">+ {f.text}</div>)}
          {r.diff.removed.map((f) => <div key={f.id} className="mt-1">− {f.text}</div>)}
          {r.diff.changed.map((c) => <div key={c.id} className="mt-1">~ {c.before} → {c.after}</div>)}
        </details>
      )}
    </div>
  )
}

export type ResearchTab = "company" | "docs" | "ai" | "pa"

/** `initial`: the tab it opens on — 公司 unless a link asked for the stock's AI 研究 or 价格行为 (研究 page's records, 观察's
 *  button). `onPlanFor`: 价格行为's analysis on screen, for its plan's lines on the chart (null: none shown). */
export function ResearchPanel({ listingId, inWatch, initial = "company", onPlanFor }:
  { listingId: string; inWatch: boolean; initial?: ResearchTab; onPlanFor?: (a: PAAnalysis | null) => void }) {
  const [tab, setTab] = useState<ResearchTab>(initial)
  const [viewing, setViewing] = useState<{ doc: string; page: number } | null>(null)
  const [pick, setPick] = useState<string | null>(null)
  const pack = usePack(listingId)
  const history = useResearchHistory(listingId)
  const status = useAIStatus()
  const run = useRunResearch()
  const entries = useWatchEntries()
  const toNote = useWatchChange((text: string) => watchEndpoints.setText(listingId, "note", text))
  const list = history.data ?? []
  const at = Math.max(0, list.findIndex((x) => x.id === pick))
  const r = list[at]
  const ai = status.data?.tasks.research
  const start = () => run.mutate(listingId, {
    onSuccess: () => setPick(null),
    onError: (e) => toast.error(`研究没做成：${String(e)}`),
  })
  const note = () => {
    if (!r) return
    const old = entries.data?.find((e) => e.listing_id === listingId)?.note ?? ""
    toNote.mutate(old ? `${old}\n\n${noteText(r)}` : noteText(r), { onSuccess: () => toast.success("摘到观察笔记里了") })
  }
  return (
    <OpenDoc.Provider value={(doc, page) => setViewing({ doc, page })}>
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-11 shrink-0 items-center px-3">
        <Tabs value={tab} onValueChange={(v) => setTab(v as ResearchTab)} className="w-full">
          <TabsList className="w-full">
            <TabsTrigger value="company">公司</TabsTrigger>
            <TabsTrigger value="docs">资料 <span className="num ml-1 text-subtle-foreground">{pack.data?.facts.length ?? ""}</span></TabsTrigger>
            <TabsTrigger value="ai">AI 研究 <span className="num ml-1 text-subtle-foreground">{list.length || ""}</span></TabsTrigger>
            <TabsTrigger value="pa">价格行为</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>
      {tab === "pa" ? <PriceActionPanel listingId={listingId} onPlanFor={onPlanFor} /> : <>
      <div className="min-h-0 flex-1 overflow-y-auto px-4" data-testid="research-panel">
        {tab === "company" ? <CompanyProfile listingId={listingId} /> : tab === "docs" ? <Evidence listingId={listingId} pack={pack.data} /> : r ? (
          <>
            <Answer r={r} older={list[at + 1]} />
            {list.length > 1 && (
              <div className="border-t border-border py-2">
                <div className="text-[0.8125rem] text-subtle-foreground">这只股票的研究</div>
                {list.slice(0, 12).map((x) => (
                  <button key={x.id} type="button" onClick={() => setPick(x.id)}
                    className={cn("flex h-7 w-full items-center gap-2 rounded px-1 text-left text-[0.8125rem] hover:bg-accent", x.id === r.id && "bg-accent")}>
                    <span className="num">{when(x.created_at)}</span><span>{x.output.lean}</span>
                    <span className={cn(x.status !== "ok" && "text-warning")}>{x.status === "ok" ? "" : "需复核"}</span>
                    <span className="num ml-auto text-muted-foreground">20 日超额 {outcomeText(x)}</span>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="py-8 text-center text-[0.875rem] text-muted-foreground">
            {history.isLoading ? "读取…" : "还没有研究过这只股票。AI 只根据「资料」里的事实回答，每条结论都标出依据。"}
          </div>
        )}
      </div>
      {tab !== "company" && <div className="shrink-0 border-t border-border px-4 py-2">
        <div className="flex gap-2">
          <Tooltip>
            <TooltipTrigger asChild><span>
              <Button size="sm" variant="outline" onClick={start} disabled={run.isPending || !ai?.ready}>
                {run.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                {run.isPending ? "研究中…（要半分钟到几分钟，可以先去看别的，做完会存在这里）" : r ? "重新研究" : "开始研究"}</Button>
            </span></TooltipTrigger>
            <TooltipContent className="max-w-xs">{ai?.ready ? `${ai.name}用 ${ai.model}；回答通常要 10–60 秒` : ai?.reason ?? "读取 AI 设置…"}</TooltipContent>
          </Tooltip>
          <Button size="sm" variant="ghost" onClick={note} disabled={!r || !inWatch || toNote.isPending}
            title={inWatch ? "把这次研究加到观察笔记的末尾（笔记每一版都保留）" : "先加入观察，才有笔记"}>
            <StickyNote className="size-3.5" />摘到笔记</Button>
        </div>
        <p className="mt-1.5 text-[0.75rem] text-subtle-foreground">AI 只整理证据，不给买卖评级、目标价或仓位建议；每次研究都记下，20 个交易日后对照基准结算（研究页）。</p>
      </div>}
      </>}
    </div>
    {viewing && <DocViewer key={`${viewing.doc}-${viewing.page}`} doc={viewing.doc} page={viewing.page} onClose={() => setViewing(null)} />}
    </OpenDoc.Provider>
  )
}
