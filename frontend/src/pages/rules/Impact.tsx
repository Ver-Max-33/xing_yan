// 这次改动的影响 (spec §6.5 标注即回归测试, design 06): the saved version against what you are trying now,
// on the same sample plus every stock you marked a hit on — the hits gained and lost, and which of your
// right hits it would lose and which wrong ones it would bring back.
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { type Compare, ruleEndpoints, type RuleRef } from "@/lib/rules"
import type { Focus } from "./GalleryTab"
import { compareMarkets, who } from "./hits"

const SAMPLE = 300

interface Props { savedRef: RuleRef | null; ruleRef: RuleRef | null; markets: string[]; seed: number; onShowHits: (focus: Focus) => void }

export function Impact({ savedRef, ruleRef, markets, seed, onShowHits }: Props) {
  const [result, setResult] = useState<{ key: string; data: Compare; skipped: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const key = JSON.stringify([savedRef, ruleRef, markets, seed])
  const changed = !!savedRef && !!ruleRef && JSON.stringify(savedRef) !== JSON.stringify(ruleRef)
  const run = async () => {
    setBusy(true)
    try {
      const n = Math.round(SAMPLE / Math.max(1, markets.length))
      const { data, skipped } = await compareMarkets(markets, (m) => ruleEndpoints.compare(savedRef!, ruleRef!, m, { n, seed }))
      setResult({ key, data, skipped })
    } catch (e) {
      toast.error(`没算出来：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }
  const r = result?.key === key ? result.data : null
  const delta = (a: number, b: number) => (b === a ? "" : b > a ? `+${b - a}` : `${b - a}`)
  return (
    <section className="border-b border-border px-4 py-3" data-testid="impact">
      <div className="flex items-baseline pb-2 text-[0.875rem] font-semibold">这次改动的影响
        <span className="ml-auto text-[0.75rem] font-normal text-subtle-foreground">样本 {SAMPLE} 只 × 近 2 年</span></div>
      {!savedRef ? <div className="text-[0.8125rem] text-subtle-foreground">规则还没保存过；保存后，之后的每次改动都和保存的版本比</div>
        : !changed ? <div className="text-[0.8125rem] text-subtle-foreground">还没有改动。拖参数或改规则后，这里比较改动前后在样本上的命中，以及你标过对错的那些</div>
          : !r ? (
            <Button variant="outline" size="sm" disabled={busy} onClick={() => void run()}>{busy ? "计算中…（要几秒）" : "算一下这次改动的影响"}</Button>
          ) : (
            <div className="space-y-1.5 text-[0.8125rem]">
              <div className="flex"><span className="flex-1 text-muted-foreground">规则命中</span><span className="num">{r.hits[0]} → {r.hits[1]}</span><span className="num w-12 text-right">{delta(r.hits[0], r.hits[1])}</span></div>
              <div className="flex"><span className="flex-1 text-muted-foreground">你标为「对」的</span><span className="num">{r.right[0]} → {r.right[1]}</span>
                <span className="w-12 text-right">{r.right_lost.length ? <span className="text-warning">丢 {r.right_lost.length}</span> : "保留"}</span></div>
              <div className="flex"><span className="flex-1 text-muted-foreground">你标为「错」的</span><span className="num">{r.wrong[0]} → {r.wrong[1]}</span>
                <span className="w-12 text-right">{r.wrong_added.length ? <span className="text-warning">新增 {r.wrong_added.length}</span> : ""}</span></div>
              {[...r.right_lost.map((h) => `${who(h)} 曾被你标为「对」，这次改动后不再命中。`),
                ...r.wrong_added.map((h) => `${who(h)} 曾被你标为「错」，这次改动后会再次命中。`)].map((x) => (
                <div key={x} className="rounded-md border border-warning/40 bg-warning/5 px-2 py-1.5 text-warning">{x}</div>
              ))}
              <div className="flex gap-2 pt-1">
                {r.added.length > 0 && <Button variant="outline" size="sm" onClick={() => onShowHits({ title: "这次新增", hits: r.added, ref: ruleRef! })}>看这 {r.added.length} 个新命中</Button>}
                {r.lost.length > 0 && <Button variant="ghost" size="sm" onClick={() => onShowHits({ title: "这次丢掉", hits: r.lost, ref: savedRef! })}>丢掉的 {r.lost.length} 个</Button>}
              </div>
              <div className="text-[0.75rem] text-subtle-foreground">算在 {r.listings} 只上（样本加上你标过的）</div>
              {result!.skipped.map((x) => <div key={x} className="text-[0.75rem] text-warning" data-testid="impact-skipped">{x}</div>)}
            </div>
          )}
    </section>
  )
}
