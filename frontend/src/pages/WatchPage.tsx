// 观察 (spec §11, design 10): the stocks you watch by status — 观察中、待研究、暂不考虑 (still followed: how it has
// done since is kept, so the record is not only the successes)、已关闭 — and the groups they are in. One stock: where it
// came from, why, what speaks for and against it and your notes (saved when you leave the box, every version kept),
// its fail lines and review dates with how far the close is, how it has done since against the market's benchmark,
// and what happened. A stock in a group is watched; set to 已关闭 it leaves the groups.
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { BarChart3, Bell, Bot, CalendarClock, CircleHelp, Download, Plus, Trash2, TriangleAlert, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"
import { ask } from "@/lib/ask"
import { onFlush } from "@/lib/flush"
import { pickedOf, toggleAll } from "@/lib/pick"
import { readPersisted, writePersisted } from "@/lib/usePersisted"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { endpoints, type Market, useToggleWatch, useWatchlist } from "@/lib/api"
import { exportRows } from "@/lib/datause"
import { changeClass, codeOf, fmtPct, fmtPrice, indexName, marketOf, shortDate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { DangerTags } from "@/components/DangerTags"
import { type Danger, useDanger } from "@/lib/danger"
import {
  lineText, nextUp, reviewCSV, STATUSES, type TextKind, TEXTS, useFailLines, useVersions, useWatchChange, useWatchEntries, type WatchCondition,
  type WatchEntry, watchEndpoints, type WatchStatus,
} from "@/lib/watch"
import { Empty, PageHeader, Section } from "./common"

type Tab = WatchStatus | "groups"
const stamp = (at: string) => at.slice(5, 16).replace("T", " ")
const spaced = (s: string) => (/^[\x20-\x7e]/.test(s) ? ` ${s}` : s) // "来自 K 线页", "来自规则「…」"

export default function WatchPage() {
  const qc = useQueryClient()
  const { data: entries = [], error } = useWatchEntries()
  const [params, setParams] = useSearchParams()
  const tab = (params.get("tab") ?? "watching") as Tab
  const shown = entries.filter((e) => e.status === tab)
  const dangerQ = useDanger("CN", entries.filter((e) => e.market === "CN").map((e) => e.listing_id))   // 危险提醒's tags
  const picked = shown.find((e) => e.listing_id === params.get("s")) ?? shown[0]
  const [chosen, setChosen] = useState<Set<string>>(new Set())   // ticked in the list (what 删除所选 takes: the ones shown)
  const shownIds = shown.map((e) => e.listing_id)
  const ticked = shownIds.filter((id) => chosen.has(id))
  const all = pickedOf(shownIds, chosen)
  /** 删除: asked first — the reason, notes, conditions and record go, and the review no longer has them. */
  const remove = async (lids: string[]) => {
    const one = lids.length === 1 ? entries.find((e) => e.listing_id === lids[0]) : null
    if (!lids.length || !(await ask({
      title: one ? `删除「${one.name ?? codeOf(one.listing_id)}」的观察？` : `删除 ${lids.length} 只股票的观察？`,
      text: "理由、笔记、条件和记录都会删掉，复盘里也不再有它们。只想先不看，可以把状态改成「已关闭」，记录会留着。",
      action: "删除",
    }))) return
    try {
      const r = await watchEndpoints.remove(lids)
      toast(`已删除 ${r.deleted} 只`)
      setChosen((c) => new Set([...c].filter((id) => !lids.includes(id))))
      if (picked && lids.includes(picked.listing_id)) setParams({ tab })
    } catch (e) {
      toast.error(`删除失败：${String(e)}`)
    } finally {
      void qc.invalidateQueries({ queryKey: ["watch"] })
      void qc.invalidateQueries({ queryKey: ["watchlist"] })
    }
  }
  const exportCSV = async () => {
    const kept = await exportRows(qc, entries, (e) => e.market)   // 数据使用 (§4.10): a market kept from export is left out
    if (!kept) return
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([reviewCSV(kept)], { type: "text/csv;charset=utf-8" }))
    a.download = `观察复盘-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span className="flex items-center gap-3">观察
        <span role="tablist" className="no-drag flex items-center gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem] font-normal">
          {[...STATUSES, ["groups", "分组"] as const].map(([k, label]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setParams({ tab: k })}
              className={cn("rounded px-2 py-0.5", tab === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>
              {label}{k !== "groups" && <span className="num ml-1.5 text-subtle-foreground">{entries.filter((e) => e.status === k).length}</span>}
            </button>
          ))}
        </span></span>}>
        <Button size="sm" variant="ghost" onClick={exportCSV} disabled={!entries.length}
          title="每只股票的状态、加入之后相对基准的表现、理由、条件和记录"><Download className="size-4" />导出复盘</Button>
      </PageHeader>
      {error && <div className="py-10 text-center text-[0.875rem] text-error">读不出来：{String(error)}</div>}
      {tab === "groups" ? <Groups /> : (
        <div className="flex min-h-0 flex-1">
          <div className="flex w-[18.75rem] shrink-0 flex-col border-r border-border 2xl:w-[22.5rem]">
            {shown.length > 0 && (
              <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-3 text-[0.8125rem] text-muted-foreground">
                <input type="checkbox" aria-label="全选这一栏" checked={all === "all"} ref={(el) => { if (el) el.indeterminate = all === "some" }}
                  onChange={() => setChosen((c) => toggleAll(shownIds, c))} />
                <span className="num">{ticked.length ? `已选 ${ticked.length}` : `${shown.length} 只`}</span>
                <span className="flex-1" />
                <Button size="xs" variant="ghost" disabled={!ticked.length} onClick={() => void remove(ticked)}><Trash2 className="size-3.5" />删除所选</Button>
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto" data-testid="watch-list">
              {shown.length === 0 && <Empty>{entries.length ? "这一栏没有股票" : "还没有观察的股票。在 K 线页按 W，或在今日、选股里加入观察。"}</Empty>}
              {shown.map((e) => <Item key={e.listing_id} e={e} ws={dangerQ.data?.warnings[e.listing_id]} active={e.listing_id === picked?.listing_id} ticked={chosen.has(e.listing_id)}
                onTick={() => setChosen((c) => { const n = new Set(c); if (n.has(e.listing_id)) n.delete(e.listing_id); else n.add(e.listing_id); return n })}
                onPick={() => setParams({ tab, s: e.listing_id })} />)}
            </div>
          </div>
          {picked && <Detail key={picked.listing_id} e={picked} onMoved={(status) => setParams({ tab: status, s: picked.listing_id })}
            onDelete={() => void remove([picked.listing_id])} />}
        </div>
      )}
    </div>
  )
}

function Item({ e, ws, active, ticked, onTick, onPick }: { e: WatchEntry; ws?: Danger[]; active: boolean; ticked: boolean; onTick: () => void; onPick: () => void }) {
  const next = nextUp(e, e.market)
  return (
    <div className={cn("flex items-start gap-2 border-b border-border/60 pl-3 hover:bg-accent/60", active && "bg-accent")}>
    <input type="checkbox" className="mt-3.5 shrink-0" aria-label={`选择 ${e.name ?? codeOf(e.listing_id)}`} checked={ticked} onChange={onTick} />
    <button type="button" onClick={onPick} className="flex min-w-0 flex-1 flex-col gap-1 py-2.5 pr-4 text-left text-[0.875rem]">
      <span className="flex w-full items-center gap-2">
        <span className="min-w-0 truncate font-medium">{e.name ?? codeOf(e.listing_id)}</span>
        <span className="num shrink-0 text-[0.75rem] text-subtle-foreground">{codeOf(e.listing_id)}</span>
        <span className="shrink-0 text-[0.6875rem] text-subtle-foreground">{e.market}</span>
        <span className={cn("num ml-auto shrink-0 whitespace-nowrap", changeClass(e.performance?.since))} title="加入之后（后复权）">{fmtPct(e.performance?.since, 1)}</span>
      </span>
      {/* the warnings on a line of their own, wrapping: three long ones beside the name pushed it out of the list (10-17) */}
      <DangerTags ws={ws} className="flex flex-wrap" />
      <span className="flex w-full items-center gap-2 text-[0.8125rem] text-subtle-foreground">
        <span className="num shrink-0 whitespace-nowrap">{shortDate(e.added_at.slice(0, 10))} 加入</span>
        {next && <span className={cn("ml-auto flex min-w-0 items-center gap-1 truncate", next.warn && "text-warning")}>
          {next.warn && <TriangleAlert className="size-3.5 shrink-0" />}{next.text}</span>}
      </span>
    </button>
    </div>
  )
}

function Detail({ e, onMoved, onDelete }: { e: WatchEntry; onMoved: (status: WatchStatus) => void; onDelete: () => void }) {
  const lid = e.listing_id
  const status = useWatchChange((s: WatchStatus) => watchEndpoints.setStatus(lid, s))
  return (
    <div className="@container min-w-0 flex-1 overflow-y-auto @min-[46rem]:overflow-hidden" data-testid="watch-detail">
      {/* the conditions, the figures and the record sit beside the notes when there is room, under them when not */}
      <div className="flex flex-col @min-[46rem]:h-full @min-[46rem]:flex-row">
      <div className="flex min-w-0 flex-1 flex-col gap-5 px-6 py-4 @min-[46rem]:overflow-y-auto">
        <div className="flex flex-wrap items-start gap-3">   {/* the actions go under the name before the name gives way */}
          <div className="min-w-[12.5rem] flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate whitespace-nowrap text-[1.1875rem] font-semibold">{e.name ?? codeOf(lid)}</span>
              <span className="num text-[0.875rem] text-subtle-foreground">{codeOf(lid)}</span>
              <span className="rounded border border-border px-1 text-[0.75rem] text-muted-foreground">{e.market}</span>
            </div>
            <div className="mt-1 text-[0.8125rem] text-muted-foreground">
              <span className="num">{shortDate(e.added_at.slice(0, 10))}</span> 加入
              {e.added_price != null && <> · 当时收 <span className="num">{fmtPrice(e.added_price, e.market)}</span></>}
              {e.source && <> · 来自{spaced(e.source)}</>}
            </div>
          </div>
          <div className="ml-auto flex items-center gap-2">
          <select aria-label="状态" value={e.status} disabled={status.isPending}
            onChange={(ev) => status.mutate(ev.target.value as WatchStatus, {
              onSuccess: (x) => onMoved(x.status), onError: (err) => toast.error(String(err)),
            })}
            className="h-8 rounded-md border border-input px-2 text-[0.875rem]">
            {STATUSES.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
          <Button size="sm" variant="outline" asChild><Link to={`/k/${lid}`}><BarChart3 className="size-4" />打开 K 线</Link></Button>
          <Button size="sm" variant="outline" asChild><Link to={`/k/${lid}?panel=research&tab=ai`}><Bot className="size-4" />AI 研究</Link></Button>
          <Button size="sm" variant="ghost" onClick={onDelete} title="删掉这只股票的观察：理由、笔记、条件和记录都删掉（只想先不看，改成「已关闭」）">
            <Trash2 className="size-4" />删除</Button>
          </div>
        </div>
        <TextBox lid={lid} kind="reason" value={e.reason} rows={3} />
        <div className="grid grid-cols-2 gap-6">
          <TextBox lid={lid} kind="for" value={e.for} rows={4} placeholder="一行一条" />
          <TextBox lid={lid} kind="against" value={e.against} rows={4} placeholder="一行一条" />
        </div>
        <TextBox lid={lid} kind="note" value={e.note} rows={12} placeholder="继续写…" hint="边写边保存 · 每次修改都留版本" />
      </div>
      <aside className="border-t border-border px-5 py-4 @min-[46rem]:w-[18.75rem] @min-[46rem]:shrink-0 @min-[46rem]:overflow-y-auto @min-[46rem]:border-t-0 @min-[46rem]:border-l 2xl:@min-[46rem]:w-[21.25rem]">
        <Conditions e={e} />
        <Performance e={e} />
        <section className="mt-6">
          <h3 className="text-[0.875rem] font-semibold">记录</h3>
          <ul className="mt-2" data-testid="watch-record">
            {e.record.map((r, i) => (
              <li key={i} className="flex items-start gap-2 py-1 text-[0.8125rem]">
                <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-subtle-foreground" />
                <span className="min-w-0 flex-1">{r.text}</span>
                <span className="num shrink-0 text-subtle-foreground">{stamp(r.at)}</span>
              </li>
            ))}
          </ul>
        </section>
      </aside>
      </div>
    </div>
  )
}

/** One text, saved as you type (a moment after you stop), when you leave the box, and before the page goes (lib/flush:
 * a reload, the app closing) — a new version only when it changed. What you type stays while a save is on its way;
 * a text changed elsewhere (an older version put back) replaces it. */
function TextBox({ lid, kind, value, rows, placeholder, hint }: {
  lid: string; kind: TextKind; value: string; rows: number; placeholder?: string; hint?: string
}) {
  const save = useWatchChange((text: string) => watchEndpoints.setText(lid, kind, text))
  const [history, setHistory] = useState(false)
  const [draft, setDraft] = useState(value)
  const [state, setState] = useState<{ kind: "idle" | "saving" | "saved" | "failed"; at?: string }>({ kind: "idle" })
  const saved = useRef(value)    // what the backend has, as far as this box knows
  const focused = useRef(false)
  const timer = useRef<number | undefined>(undefined)
  // what was typed and not yet saved, kept on this computer at once: a page going away (a reload) does not wait for the
  // save, and on Windows the reloaded page read the note before the save sent as it went had arrived (2026-10-10)
  const key = `watch.unsaved.${lid}.${kind}`
  const latest = useRef({ draft, save, key })
  useEffect(() => { latest.current = { draft, save, key } })
  useEffect(() => {
    if (value !== saved.current && !focused.current) {
      saved.current = value
      setDraft(value)
    }
  }, [value])
  const flush = useCallback(async () => {
    window.clearTimeout(timer.current)
    const { draft: text, save: put, key: kept } = latest.current
    if (text === saved.current) return
    writePersisted(kept, { text, at: Date.now() })
    const before = saved.current
    saved.current = text
    setState({ kind: "saving" })
    try {
      await put.mutateAsync(text)
      if (readPersisted<{ text?: string } | null>(kept, null)?.text === text) writePersisted(kept, null)
      setState({ kind: "saved", at: new Date().toTimeString().slice(0, 5) })
    } catch (err) {
      if (saved.current === text) saved.current = before   // not saved: the next flush (or 重试) sends it again
      setState({ kind: "failed" })
      toast.error(`没保存上：${String(err)}`)
    }
  }, [])
  useEffect(() => onFlush(flush), [flush])
  useEffect(() => () => { void flush() }, [flush])
  useEffect(() => {   // typed and not saved when the page last went (within a day): put back, and saved now
    const kept = readPersisted<{ text?: string; at?: number } | null>(key, null)
    if (kept?.text === undefined) return
    if (kept.text === saved.current || Date.now() - (kept.at ?? 0) > 86_400_000) {
      writePersisted(key, null)
      return
    }
    setDraft(kept.text)
    latest.current = { ...latest.current, draft: kept.text }
    void flush()
  }, [key, flush])
  const id = `watch-${kind}`
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-3 text-[0.8125rem]">
        <label htmlFor={id} className="text-muted-foreground">{TEXTS[kind]}</label>
        {hint && <span className="text-[0.75rem] text-subtle-foreground">{hint}</span>}
        <span className="text-[0.75rem] text-subtle-foreground" aria-live="polite">
          {state.kind === "saving" ? "保存中…" : state.kind === "saved" ? `已保存 ${state.at}` : state.kind === "failed"
            ? <button type="button" className="text-error hover:underline" onClick={() => void flush()}>没保存上 · 重试</button> : null}
        </span>
        <button type="button" className="ml-auto text-[0.75rem] text-subtle-foreground hover:text-foreground" aria-expanded={history}
          onClick={() => setHistory(!history)}>{history ? "收起版本" : "历史版本"}</button>
      </div>
      <textarea id={id} value={draft} rows={rows} placeholder={placeholder}
        onChange={(ev) => {
          setDraft(ev.target.value)
          window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => { void flush() }, 800)
        }}
        onFocus={() => { focused.current = true }} onBlur={() => { focused.current = false; void flush() }}
        className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 text-[0.875rem] leading-6 outline-none focus-visible:border-ring" />
      {history && <Versions lid={lid} kind={kind} onPick={(text) => { saved.current = text; setDraft(text); save.mutate(text) }} />}
    </div>
  )
}

function Versions({ lid, kind, onPick }: { lid: string; kind: TextKind; onPick: (text: string) => void }) {
  const { data = [] } = useVersions(lid, kind, true)
  if (data.length === 0) return <div className="text-[0.75rem] text-subtle-foreground">还没有写过</div>
  return (
    <div className="max-h-48 overflow-y-auto rounded-md border border-border">
      {data.map((v, i) => (
        <div key={i} className="flex gap-3 border-b border-border/60 px-3 py-1.5 text-[0.8125rem] last:border-b-0">
          <span className="num w-20 shrink-0 text-subtle-foreground">{stamp(v.created_at)}</span>
          <span className="min-w-0 flex-1 whitespace-pre-wrap">{v.text || "（空）"}</span>
          {i > 0 && <button type="button" className="shrink-0 text-[0.75rem] text-primary" onClick={() => onPick(v.text)}>用这一版</button>}
        </div>
      ))}
    </div>
  )
}

const KINDS: [WatchCondition["kind"], string][] = [["fail", "失效线"], ["review", "复核日"], ["manual", "自己对照"]]
const ICON = { fail: Bell, review: CalendarClock, manual: CircleHelp }

function Conditions({ e }: { e: WatchEntry }) {
  const lid = e.listing_id
  const add = useWatchChange((c: Parameters<typeof watchEndpoints.addCondition>[1]) => watchEndpoints.addCondition(lid, c))
  const remove = useWatchChange((id: number) => watchEndpoints.removeCondition(lid, id))
  const [kind, setKind] = useState<WatchCondition["kind"]>("fail")
  const [text, setText] = useState("")
  const [op, setOp] = useState<"<" | ">">("<")
  const [price, setPrice] = useState("")
  const [due, setDue] = useState("")
  const ready = text.trim() && (kind !== "fail" || Number(price) > 0) && (kind !== "review" || due)
  const submit = (ev: React.FormEvent) => {
    ev.preventDefault()
    if (!ready) return
    add.mutate({ kind, text: text.trim(), ...(kind === "fail" ? { op, price: Number(price) } : kind === "review" ? { due } : {}) }, {
      onSuccess: () => { setText(""); setPrice(""); setDue("") },
      onError: (err) => toast.error(String(err)),
    })
  }
  return (
    <section>
      <div className="flex items-center">
        <h3 className="text-[0.875rem] font-semibold">失效条件</h3>
        <span className="ml-auto text-[0.75rem] text-subtle-foreground">每天收盘后对照，触发时提醒</span>
      </div>
      <div className="mt-2" data-testid="watch-conditions">
        {e.conditions.length === 0 && <div className="py-1 text-[0.8125rem] text-subtle-foreground">还没有。写下什么情况说明看错了，或哪天该再看一次。</div>}
        {e.conditions.map((c) => <ConditionRow key={c.id} c={c} market={e.market} onRemove={() => remove.mutate(c.id)} />)}
      </div>
      {e.status === "holding" && <Suggested e={e} onPick={(c) => add.mutate(c, { onError: (err) => toast.error(String(err)) })} busy={add.isPending} />}
      {(e.lines?.length ?? 0) > 0 && (
        <div className="mt-2 border-t border-border pt-2 text-[0.8125rem]" data-testid="watch-lines">
          <div className="flex items-center text-muted-foreground">盘中提醒线<span className="ml-2 text-[0.75rem] text-subtle-foreground">K 线上画的，价格穿过时提醒</span>
            <Link to={`/k/${e.listing_id}`} className="ml-auto text-[0.75rem] hover:underline">到 K 线上改</Link></div>
          {e.lines!.map((l) => <div key={l.id} className="num py-0.5">{lineText(l, e.market)}</div>)}
        </div>
      )}
      <form onSubmit={submit} className="mt-3 flex flex-col gap-2 border-t border-border pt-3">
        <div className="flex gap-0.5 rounded-md border border-border p-0.5 text-[0.8125rem]" role="radiogroup" aria-label="条件种类">
          {KINDS.map(([k, label]) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)}
              className={cn("flex-1 rounded px-2 py-0.5", kind === k ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground")}>{label}</button>
          ))}
        </div>
        <Input aria-label="条件说明" value={text} onChange={(ev) => setText(ev.target.value)} className="h-8"
          placeholder={kind === "fail" ? "收盘跌回箱顶" : kind === "review" ? "决算发表后复核" : "停产延长的公告"} />
        {kind === "fail" && (
          <div className="flex gap-2">
            <select aria-label="方向" value={op} onChange={(ev) => setOp(ev.target.value as "<" | ">")}
              className="h-8 rounded-md border border-input px-2 text-[0.875rem]">
              <option value="<">收盘跌破</option><option value=">">收盘涨破</option>
            </select>
            <Input aria-label="失效价位" type="number" step="any" min="0" value={price} onChange={(ev) => setPrice(ev.target.value)} className="h-8" placeholder="价格（不复权）" />
          </div>
        )}
        {kind === "review" && <Input aria-label="复核日期" type="date" value={due} onChange={(ev) => setDue(ev.target.value)} className="h-8" />}
        {kind === "manual" && <div className="text-[0.75rem] text-subtle-foreground">公告、业绩这类事这里没有数据源，只记下来，自己对照</div>}
        <Button size="sm" variant="outline" type="submit" disabled={!ready || add.isPending}><Plus className="size-4" />加上</Button>
      </form>
    </section>
  )
}

/** 持有中: the fail lines its structures suggest, nearest first (the sell side's alerts watch the one you save). */
function Suggested({ e, onPick, busy }: { e: WatchEntry; onPick: (c: Parameters<typeof watchEndpoints.addCondition>[1]) => void; busy: boolean }) {
  const { data } = useFailLines(e.listing_id, true)
  const saved = new Set(e.conditions.filter((c) => c.kind === "fail").map((c) => c.price))
  const lines = (data ?? []).filter((x) => !saved.has(x.price)).slice(0, 3)
  if (!data) return null
  return (
    <div className="mt-2 border-t border-border pt-2 text-[0.8125rem]" data-testid="fail-suggestions">
      <div className="text-muted-foreground">按结构建议的失效线<span className="ml-2 text-[0.75rem] text-subtle-foreground">收盘价下方最近的结构线，选一条存下</span></div>
      {lines.length === 0 && <div className="py-1 text-subtle-foreground">{data.length ? "建议的线都已经存了" : "收盘价下方没有还在起作用的金品、假底或箱底，自己写一条"}</div>}
      {lines.map((x) => (
        <div key={`${x.source}${x.price}`} className="flex items-center gap-2 py-1">
          <div className="min-w-0 flex-1">
            <div>{x.label}</div>
            <div className="num text-subtle-foreground">收盘跌破 {fmtPrice(x.price, e.market)} · 距离 {fmtPct(x.distance, 1)} · {shortDate(x.since)} 起</div>
          </div>
          <Button size="xs" variant="outline" disabled={busy} onClick={() => onPick({ kind: "fail", text: x.label, op: "<", price: x.price })}>用这条</Button>
        </div>
      ))}
    </div>
  )
}

function ConditionRow({ c, market, onRemove }: { c: WatchCondition; market: Market; onRemove: () => void }) {
  const Icon = ICON[c.kind]
  const sub = c.fired_day
    ? c.kind === "fail" ? `${shortDate(c.fired_day)} 收 ${fmtPrice(c.fired_value, market)}，触发了` : `${shortDate(c.fired_day)} 到了复核日`
    : c.kind === "fail" ? `收盘${c.op === "<" ? "跌破" : "涨破"} ${fmtPrice(c.price, market)} · 距离 ${fmtPct(c.distance, 1)}`
      : c.kind === "review" ? `${c.due} · ${c.days_left != null && c.days_left >= 0 ? `还有 ${c.days_left} 天` : "已过"}`
        : "没有数据源能对照，要自己看"
  return (
    <div className="flex items-start gap-2 py-1.5 text-[0.875rem]">
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div>{c.text}</div>
        <div className={cn("text-[0.8125rem]", c.fired_day ? "text-warning" : c.kind === "manual" ? "text-muted-foreground" : "text-subtle-foreground")}>{sub}</div>
      </div>
      <Button size="icon-xs" variant="ghost" aria-label={`去掉「${c.text}」`} onClick={onRemove}><X className="size-3.5" /></Button>
    </div>
  )
}

function Performance({ e }: { e: WatchEntry }) {
  const p = e.performance
  const cell = (label: string, v: number | null) => (
    <div className="min-w-0 pr-3">
      <div className="truncate text-[0.75rem] whitespace-nowrap text-subtle-foreground" title={label}>{label}</div>
      <div className={cn("num text-[1.0625rem] whitespace-nowrap", changeClass(v))}>{fmtPct(v, 1)}</div>
    </div>
  )
  return (
    <section className="mt-6">
      <h3 className="text-[0.875rem] font-semibold">加入之后</h3>
      {p ? <>
        <div className="mt-2 grid grid-cols-[repeat(auto-fit,minmax(5.5rem,1fr))] gap-y-2">
          {cell("本股", p.since)}{cell(`同期 ${indexName(p.benchmark)}`, p.bench)}{cell("超额", p.excess)}
        </div>
        <div className="mt-1 text-[0.75rem] text-subtle-foreground">{e.added_day} 收盘到 {p.day} 收盘，后复权</div>
      </> : <div className="mt-2 text-[0.8125rem] text-subtle-foreground">还没有加入之后的收盘</div>}
      <p className="mt-2 text-[0.8125rem] text-muted-foreground">「暂不考虑」的股票也会记录之后的走势，避免只记得成功的案例。</p>
    </section>
  )
}

/** The groups (自选分组): what the K-line page walks through with J / K. */
function Groups() {
  const { data: groups = [] } = useWatchlist()
  const qc = useQueryClient()
  const toggle = useToggleWatch()
  const [name, setName] = useState("")
  const addGroup = useMutation({
    mutationFn: (n: string) => endpoints.addGroup(n),
    onSuccess: () => { setName(""); void qc.invalidateQueries({ queryKey: ["watchlist"] }) },
    onError: (e) => toast.error(`新建分组失败：${String(e)}`),
  })
  const delGroup = useMutation({
    mutationFn: (id: number) => endpoints.deleteGroup(id),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["watchlist"] }); void qc.invalidateQueries({ queryKey: ["watch"] }) },
  })
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6">
      <form className="flex items-center gap-2 pt-4" onSubmit={(e) => { e.preventDefault(); if (name.trim()) addGroup.mutate(name.trim()) }}>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="新分组名" className="h-8 w-40" />
        <Button size="sm" variant="outline" type="submit"><Plus className="size-4" />新建分组</Button>
        <span className="text-[0.8125rem] text-subtle-foreground">分组里的股票就在观察里；移出所有分组就是关闭。</span>
      </form>
      {groups.map((g) => (
        <Section key={g.id} title={`${g.name} · ${g.items.length}`}
          hint={g.name === "养箱" ? "养箱：粗筛出来、有点意思的先养着，每天看它怎么走" : undefined}>
          <div className="mb-2 flex justify-end">
            {g.sort > 1 && (
              <Button size="xs" variant="ghost" onClick={async () => (await ask({ title: `删除分组「${g.name}」？`,
                text: "分组里的股票会移出这个分组；没在别的分组里的，观察条目会关闭（之后可以重新打开），记录都还在。", action: "删除分组" })) && delGroup.mutate(g.id)}>
                <Trash2 className="size-3.5" />删除分组
              </Button>
            )}
          </div>
          {g.items.length === 0 ? <Empty>空</Empty> : g.items.map((i) => (
            <div key={i.listing_id} className="flex h-8 items-center gap-3 rounded-md px-2 text-[0.875rem] hover:bg-accent">
              <Link to={`/k/${i.listing_id}`} className="flex flex-1 items-center gap-3">
                <span className="w-48 truncate">{i.name ?? codeOf(i.listing_id)}</span>
                <span className="num w-16 text-subtle-foreground">{codeOf(i.listing_id)}</span>
                <span className="num w-24 text-right">{fmtPrice(i.close, marketOf(i.listing_id))}</span>
                <span className={cn("num w-20 text-right", changeClass(i.change))}>{fmtPct(i.change)}</span>
                <span className="num w-16 text-right text-subtle-foreground">{shortDate(i.date)}</span>
              </Link>
              <Button size="icon-xs" variant="ghost" aria-label="移出" onClick={() => toggle.mutate({ groupId: g.id, listingId: i.listing_id, add: false })}>
                <X className="size-3.5" />
              </Button>
            </div>
          ))}
        </Section>
      ))}
    </div>
  )
}
