// 版本对比 (spec §6.3, §6.5): two versions of the rule — or a version and what you are editing now — on the
// same sample plus every stock you marked a hit on: the hits one has and the other does not, and which of
// your right hits are lost and which wrong ones come back.
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { codeOf } from "@/lib/format"
import { type Compare, type Hit, ruleEndpoints, type RuleDoc, type RuleRef } from "@/lib/rules"
import { compareMarkets } from "./hits"

const SAMPLE = 300
type Side = number | "current"

interface Props {
  ruleId: string; doc: RuleDoc | null; current: RuleRef | null; dirty: boolean; markets: string[]; seed: number
  onOpen: (listingId: string, day: string) => void
}

function HitTable({ title, hits, onOpen }: { title: string; hits: Hit[]; onOpen: Props["onOpen"] }) {
  return (
    <div>
      <div className="pb-1 text-[0.875rem] font-semibold">{title} <span className="num font-normal text-subtle-foreground">{hits.length}</span></div>
      {hits.length === 0 ? <div className="py-3 text-[0.8125rem] text-subtle-foreground">没有</div> : (
        <Table>
          <TableHeader><TableRow><TableHead>股票</TableHead><TableHead>代码</TableHead><TableHead>信号日</TableHead></TableRow></TableHeader>
          <TableBody>
            {hits.map((h) => (
              <TableRow key={`${h.listing_id}|${h.signal_date}`} className="cursor-pointer" onClick={() => onOpen(h.listing_id, h.signal_date)}>
                <TableCell>{h.name ?? codeOf(h.listing_id)}</TableCell><TableCell className="num">{h.listing_id}</TableCell><TableCell className="num">{h.signal_date}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

export function CompareTab({ ruleId, doc, current, dirty, markets, seed, onOpen }: Props) {
  const saved = (doc?.versions ?? []).map((v) => v.version)
  const versions = saved.length ? saved : doc ? [doc.version] : []   // a built-in template: its one version
  const latest = versions.at(-1) ?? null
  const [a, setA] = useState<Side | null>(dirty ? latest : versions.at(-2) ?? latest)
  const [b, setB] = useState<Side | null>(dirty ? "current" : latest)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ for: string; data: Compare; skipped: string[] } | null>(null)
  const options: [Side, string][] = [...versions.map((v) => [v, `v${v}`] as [Side, string]), ...(dirty && current ? [["current", "当前编辑"] as [Side, string]] : [])]
  // a side that is gone (no edit left after a save or 撤销改动) falls back to the two latest versions
  const offered = (s: Side | null) => options.some(([v]) => v === s)
  const [sa, sb] = offered(a) && offered(b) ? [a, b] : [versions.at(-2) ?? latest, latest]
  const refOf = (s: Side | null): RuleRef | null => (s === "current" ? current : s === null ? null : { rule_id: ruleId, version: s })
  const key = JSON.stringify([refOf(sa), refOf(sb), markets, seed])
  const run = async () => {
    const [ra, rb] = [refOf(sa), refOf(sb)]
    if (!ra || !rb) return
    setBusy(true)
    try {
      const n = Math.round(SAMPLE / Math.max(1, markets.length))
      const { data, skipped } = await compareMarkets(markets, (m) => ruleEndpoints.compare(ra, rb, m, { n, seed }))
      setResult({ for: key, data, skipped })
    } catch (e) {
      toast.error(`没比出来：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }
  const r = result?.for === key ? result.data : null
  const pick = (value: Side | null, set: (s: Side) => void, label: string) => (
    <select aria-label={label} value={value === null ? "" : String(value)} onChange={(e) => set(e.target.value === "current" ? "current" : Number(e.target.value))}
      className="h-8 rounded-md border border-input px-2 text-[0.875rem]">
      {options.map(([v, l]) => <option key={String(v)} value={String(v)}>{l}</option>)}
    </select>
  )
  if (!doc) return <div className="p-8 text-center text-[0.875rem] text-subtle-foreground">规则保存过至少一个版本后，才能比较版本</div>
  if (options.length < 2) return (
    <p data-testid="compare-only" className="px-6 py-4 text-[0.875rem] text-subtle-foreground">
      {doc.builtin ? `内置模板只有 v${latest}：在「单股调试」里拖了参数以后，可以拿「当前编辑」和它比；要存自己的版本，先复制为新规则。`
        : `现在只有 v${latest}：改了规则或拖了参数以后，可以拿「当前编辑」和它比；保存出新版本以后，可以比任意两个版本。`}
    </p>
  )
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <div className="flex flex-wrap items-center gap-2 text-[0.875rem]">
        <span className="text-muted-foreground">比较</span>{pick(sa, setA, "旧版本")}<span className="text-muted-foreground">和</span>{pick(sb, setB, "新版本")}
        <Button size="sm" disabled={busy || sa === null || sb === null || sa === sb || !refOf(sa) || !refOf(sb)} onClick={() => void run()}>{busy ? "比较中…（要几秒）" : "比较"}</Button>
        <span className="text-[0.8125rem] text-subtle-foreground">样本 {SAMPLE} 只 × 近 2 年（第 {seed + 1} 批），加上你标过的股票</span>
      </div>
      {r && (
        <div className="mt-5 space-y-5" data-testid="compare-result">
          {result!.skipped.map((x) => <div key={x} className="text-[0.8125rem] text-warning" data-testid="compare-skipped">{x}</div>)}
          <div className="num flex gap-8 text-[0.875rem]">
            <div><div className="text-[0.8125rem] text-muted-foreground">命中</div>{r.hits[0]} → {r.hits[1]}</div>
            <div><div className="text-[0.8125rem] text-muted-foreground">两个版本都有</div>{r.kept}</div>
            <div><div className="text-[0.8125rem] text-muted-foreground">你标为「对」的</div>{r.right[0]} → {r.right[1]}{r.right_lost.length ? <span className="text-warning">（丢 {r.right_lost.length}）</span> : ""}</div>
            <div><div className="text-[0.8125rem] text-muted-foreground">你标为「错」的</div>{r.wrong[0]} → {r.wrong[1]}{r.wrong_added.length ? <span className="text-warning">（新增 {r.wrong_added.length}）</span> : ""}</div>
            <div><div className="text-[0.8125rem] text-muted-foreground">算在</div>{r.listings} 只上</div>
          </div>
          <div className="grid grid-cols-2 gap-6">
            <HitTable title="新版本才有的" hits={r.added} onOpen={onOpen} />
            <HitTable title="旧版本才有的" hits={r.lost} onOpen={onOpen} />
          </div>
        </div>
      )}
    </div>
  )
}
