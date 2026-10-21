// 校准 (spec §6.5): a grid over the parameters marked tune: true, scored on your Y/N marks in the gallery and,
// when a tuned parameter sets a box, on your hand-drawn samples. Marks are split 7:3 into a tune and a test
// group once and for all; the grid is judged on the tune group, both groups are shown. Nothing is applied by
// itself: "用这组" moves the sliders, and saving makes it a version. It runs in 验证's queue (1.33): its progress,
// 暂停 / 继续 / 取消 here, and the last result stays with the rule — leaving the tab or the page loses nothing.
import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"
import { RunNow } from "@/components/RunNow"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import {
  BOX_CATEGORIES, type Calibration, type CalibrationSettings, type CalPoint, type CalScores, NOT_BOX_CATEGORIES, type RuleInfo, ruleEndpoints,
  type RuleRef, useLabels,
} from "@/lib/rules"
import { cn } from "@/lib/utils"
import { useRunsOf } from "@/lib/validate"

const SAMPLE = 300
const CATEGORY_NAME: Record<string, string> = { ...BOX_CATEGORIES, ...NOT_BOX_CATEGORIES }
const pct = (x: number | null) => (x === null ? "—" : `${Math.round(x * 100)}%`)
const show = (v: number | boolean) => (typeof v === "boolean" ? (v ? "开" : "关") : String(Number(v.toPrecision(12))))

interface Props {
  ruleRef: RuleRef | null; ruleId: string; info: RuleInfo | null; seed: number
  onUse: (values: Record<string, number | boolean>) => void
}

/** A share with what it is of: 67%（2/3）— 67% of 3 marks is not 67% of 30 (older results without counts: the share). */
const ofN = (x: number | null, n: number | undefined) => (x === null || n === undefined ? pct(x) : `${pct(x)}（${Math.round(x * n)}/${n}）`)

function Scores({ s, boxes, n }: { s: CalScores; boxes: boolean; n?: { right: number; wrong: number; boxes: number } }) {
  return <>
    <TableCell className="num">{ofN(s.right, n?.right)}</TableCell>
    <TableCell className="num">{ofN(s.wrong, n?.wrong)}</TableCell>
    {boxes && <TableCell className="num">{ofN(s.boxes, n?.boxes)}</TableCell>}
  </>
}

export function CalibrateTab({ ruleRef, ruleId, info, seed, onUse }: Props) {
  const qc = useQueryClient()
  const { active, shown, act } = useRunsOf(ruleId, "calibrate")
  const [starting, setStarting] = useState(false)
  const labelsQ = useLabels(undefined, "qfq")
  const tuned = (info?.params ?? []).filter((p) => p.tune)
  const busy = !!active && (active.status === "queued" || active.status === "running" || active.status === "paused")
  const r = shown?.result as Calibration | undefined
  const ran = shown?.settings as unknown as CalibrationSettings | undefined
  // a result belongs to the rule (its text or saved version) and the sample — not to the slider values
  const sameRule = !!(shown && ran && ruleRef && (ruleRef.text ?? null) === ran.text && (ruleRef.text != null || ruleRef.version === shown.version))
  const current = sameRule && ran!.sample.seed === seed
  const start = async () => {
    if (!ruleRef) return
    setStarting(true)
    try {
      await ruleEndpoints.calibrate(ruleRef, { n: SAMPLE, seed })
      await qc.invalidateQueries({ queryKey: ["validate", "runs"] })
    } catch (e) {
      toast.error(`没开始校准：${String(e)}`)
    } finally {
      setStarting(false)
    }
  }
  const boxes = (r?.structural.length ?? 0) > 0
  // the current setting on the front is said on its own row, not listed again with a 用这组 that changes nothing
  const others = (r?.candidates ?? []).filter((c) => r!.params.some((k) => c.values[k] !== r!.current.values[k]))
  const onFront = !!r && others.length < r.candidates.length
  const row = (p: CalPoint, label: string | null) => (
    <TableRow key={label ?? JSON.stringify(p.values)} className={cn(label && "text-muted-foreground")}>
      <TableCell>{label ?? ""}</TableCell>
      {r!.params.map((k) => <TableCell key={k} className="num">{show(p.values[k])}</TableCell>)}
      <Scores s={p.tune} boxes={boxes} n={r!.samples.by_group?.tune} />
      <Scores s={p.test} boxes={boxes} n={r!.samples.by_group?.test} />
      <TableCell className="num">{p.hits}</TableCell>
      <TableCell>{!label && current && <Button variant="outline" size="sm" className="h-7" onClick={() => onUse(p.values)}>用这组</Button>}</TableCell>
    </TableRow>
  )
  const labelOf = (id: string) => (labelsQ.data ?? []).find((l) => l.id === id)
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4 text-[0.875rem]" data-testid="calibrate">
      <div className="flex flex-wrap items-center gap-3">
        <div className="font-semibold">校准</div>
        <div className="text-muted-foreground">
          {tuned.length ? tuned.map((p) => `${p.id}（${p.range ? `${p.range[0]}–${p.range[1]}` : "开 / 关"}${p.step ? `，步长 ${p.step}` : ""}）`).join("、")
            : "这条规则没有标 tune: true 的参数"}
        </div>
        <Button size="sm" disabled={busy || starting || !ruleRef || tuned.length === 0} onClick={() => void start()}
          title={busy ? "这条规则的校准还没结束：在下面继续或取消它" : !ruleRef ? "规则有错误，先在「规则」里改好" : undefined}>
          {r ? "重新校准" : "开始校准"}</Button>
      </div>
      {busy && <div className="-mx-6 mt-3 border-y border-border">
        <RunNow run={active} title="校准" noun="校准" after={r ? "下面还是上一次校准的结果" : "之后可以重新校准"} onAct={act} testid="calibrate-now" />
      </div>}
      {active?.status === "error" && <div className="mt-3 text-[0.8125rem] text-error" role="alert">最近一次校准没算完：{active.error}{r ? "。下面是上一次的结果" : ""}</div>}
      {active?.status === "cancelled" && r && <div className="mt-3 text-[0.8125rem] text-subtle-foreground">最近一次已取消。下面是上一次的结果</div>}
      <p className="mt-2 max-w-[47.5rem] text-[0.8125rem] text-subtle-foreground">
        只在标了 tune: true 的参数（最多 3 个、最多 500 种组合）上做网格搜索。打分用你在「样例图集」里按 Y / N 标的对错：对的要找回来，错的要排除掉；
        校准的参数改箱体时，再加上和你在「箱体标注」里画的样例一致。标注按 7:3 固定分成调参组和检验组，只按调参组挑，两组都列出来；结果不会自动用上。
      </p>
      {r && <>
        {r.warning && <div className="mt-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-[0.8125rem] text-warning">{r.warning}</div>}
        <div className="mt-3 text-[0.8125rem] text-muted-foreground">
          {shown!.finished_at?.slice(5, 16).replace("T", " ")} 校准{ran?.text ? "（未保存的草稿）" : ` v${shown!.version}`} · {r.combos} 种组合 · 标注：对 {r.samples.right}、错 {r.samples.wrong}{boxes ? `、箱体样例 ${r.samples.boxes}` : ""}（调参组 {r.samples.tune}、检验组 {r.samples.test}）· 样本 {ran?.sample.n ?? SAMPLE} 只 × 近 {ran?.years ?? 2} 年的命中数
        </div>
        {!current && <div className="mt-2 text-[0.8125rem] text-warning" data-testid="calibrate-stale">
          {sameRule ? "样本换过了：这是另一组样本上的结果" : "规则改过了：这是改之前的规则上的结果"}，重新校准后才能「用这组」</div>}
        <Table className="mt-2" data-testid="calibrate-result">
          <TableHeader>
            <TableRow>
              <TableHead />
              {r.params.map((k) => <TableHead key={k}>{k}</TableHead>)}
              <TableHead>调参组 · 对的找回</TableHead><TableHead>错的排除</TableHead>{boxes && <TableHead>箱体一致</TableHead>}
              <TableHead>检验组 · 对的找回</TableHead><TableHead>错的排除</TableHead>{boxes && <TableHead>箱体一致</TableHead>}
              <TableHead>样本命中</TableHead><TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {row(r.current, onFront ? "现在 · 在前沿上" : "现在")}
            {others.map((c) => row(c, null))}
          </TableBody>
        </Table>
        <p className="mt-2 text-[0.8125rem] text-subtle-foreground">列出的是帕累托前沿：没有别的一组在调参组上每一项都不差、至少一项更好；同样分数的几组只留离现在最近的。
          {others.length === 0 ? "网格里没有比现在更好的一组。" : ""}</p>
        {r.explained.length > 0 && <div className="mt-4" data-testid="calibrate-explained">
          <div className="pb-1 font-semibold">和现在的设置不一致的箱体样例</div>
          {r.explained.map((e) => {
            const l = labelOf(e.label)
            return (
              <div key={e.label} className="text-[0.8125rem] text-muted-foreground">
                {l ? `${l.listing_id} · ${CATEGORY_NAME[l.category] ?? l.category} · ${l.start} → ${l.end}` : e.label}：
                {e.values ? `把 ${Object.entries(e.values).filter(([k, v]) => v !== r.current.values[k]).map(([k, v]) => `${k} 改成 ${show(v)}`).join("、")} 就一致`
                  : "网格里没有一组设置能让它一致（可能要改别的参数，或者这个样例的画法和定义不同）"}
              </div>
            )
          })}
        </div>}
      </>}
    </div>
  )
}
