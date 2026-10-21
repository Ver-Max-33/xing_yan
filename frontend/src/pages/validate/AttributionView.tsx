// 候选条件归因 (spec §9.5): each candidate feature read on the signal day, cut at its in-sample quintiles, both halves'
// 20-day excess by group; a filter is suggested only where both halves are ordered the same way, and adopting one is
// an edit in the rule's editor (§6.4): it is previewed and checked against your labels there, and saving makes the version.
import { Loader2 } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { DiffView } from "@/components/rules/DiffView"
import { Button } from "@/components/ui/button"
import { aiEndpoints } from "@/lib/ai"
import { fmtPct, MARKET_NAME } from "@/lib/format"
import { type RuleSummary, useRule } from "@/lib/rules"
import { cn } from "@/lib/utils"
import { adoptAdd, type AttrSuggestion, type Attribution, groupMedians } from "@/lib/validate"
import { Parts } from "../common"
import { copyText } from "../rules/ruleText"

const rate = (v: number | null | undefined) => fmtPct(v, 1, false)
const ORDER = { up: "单调 ↑（越大越好）", down: "单调 ↓（越小越好）" } as const

export function AttributionView({ a, rule, metric = "20 日", by = "median" }: { a: Attribution; rule?: RuleSummary; metric?: string
  by?: "median" | "mean" }) {   // by the plan: each group's expectancy per signal day
  const navigate = useNavigate()
  const { data: doc } = useRule(rule?.id)
  const [busy, setBusy] = useState<string | null>(null)
  const [proposed, setProposed] = useState<{ feature: string; text: string; diff: string } | null>(null) // seen before it is used
  const draftKey = rule ? `wb.rule.draft.${rule.id}` : ""
  const adopt = async (s: AttrSuggestion) => {
    if (!rule || !doc) return
    setBusy(s.feature)
    try {
      let base = doc.text
      try { base = localStorage.getItem(draftKey) ?? doc.text } catch { /* no storage: from the saved version */ }
      const { text, diff } = await aiEndpoints.apply(base, [], [adoptAdd(a, s)])
      setProposed({ feature: s.feature, text, diff })
    } catch (e) {
      toast.error(`加不进规则：${String(e)}`)
    } finally {
      setBusy(null)
    }
  }
  const use = (text: string) => {
    if (!rule || !doc) return
    if (doc.builtin) {        // a built-in rule is copied first: the copy is yours to save
      sessionStorage.setItem("rule.import", copyText(text))
      navigate(`/rules/new?from=${rule.id}`)
    } else {                  // your unsaved draft: the editor starts from it
      localStorage.setItem(draftKey, text)
      navigate(`/rules/${rule.id}?tab=rule`)
    }
  }
  return (
    <div className="mt-2 text-[0.8125rem]">
      <div data-testid="suggestions">
        {a.suggestions.length === 0 ? (
          <p className="text-subtle-foreground">{(a.markets?.length ?? 1) > 1
            ? "两个市场各自分组，只给数字；只选一个市场再体检，才给加过滤的建议（一个门槛只对一个市场）。"
            : "没有可以建议的过滤：没有一个特征在样本内外都按同一个方向一组比一组好（后续收益打乱后这里也应该是空的）。"}</p>
        ) : a.suggestions.map((s) => (
          <div key={s.feature} className="mb-2 rounded-md border border-border px-3 py-2">
            <div className="flex items-center gap-2">
              <span>加一条：<span className="num">{s.condition}</span><span className="ml-1 text-subtle-foreground">（{s.label}，去掉{s.op === ">" ? "最低" : "最高"}的五分之一）</span></span>
              <Button size="sm" variant="outline" className="ml-auto h-7" disabled={!doc || busy !== null || !a.stage} onClick={() => void adopt(s)}>
                {busy === s.feature && <Loader2 className="size-3.5 animate-spin" />}采纳到规则里</Button>
            </div>
            {proposed?.feature === s.feature && (
              <div className="mt-2">
                <p className="mb-1 text-subtle-foreground">规则会这样改（程序重新写出整条规则：格式可能变，注释不保留）：</p>
                <DiffView diff={proposed.diff} />
                <div className="mt-1.5 flex gap-2">
                  <Button size="sm" className="h-7" onClick={() => use(proposed.text)}>放进编辑器</Button>
                  <Button size="sm" variant="ghost" className="h-7" onClick={() => setProposed(null)}>取消</Button>
                </div>
              </div>
            )}
            <div className="mt-1 text-subtle-foreground">
              信号 <span className="num">{s.before.signals.toLocaleString()} → {s.after.signals.toLocaleString()}</span>；
              {metric}胜率 样本内 <span className="num">{rate(s.before.win_in)} → {rate(s.after.win_in)}</span>，
              样本外 <span className="num text-foreground">{rate(s.before.win_out)} → {rate(s.after.win_out)}</span>
            </div>
          </div>
        ))}
        {a.suggestions.length > 0 && <p className="text-subtle-foreground">采纳只是改到规则编辑器里（没有保存）：在那里预览、看你标过的样例丢了哪些，保存才成为新版本。</p>}
      </div>
      <div className="mt-3 overflow-x-auto"><table className="w-full text-[0.8125rem]" data-testid="attribution">
        <thead className="text-muted-foreground">
          {/* when narrow, the five figures go onto more lines (between figures) and the long heading onto two */}
          <tr className="h-8 border-b border-border [&>th]:px-1 [&>th]:font-normal">
            <th className="text-left whitespace-nowrap">特征（信号当天）</th>{(a.markets?.length ?? 1) > 1 && <th className="text-left whitespace-nowrap">市场</th>}
            <th className="text-right"><Parts sep="" parts={["样本内：", by === "mean" ? "五组按交易计划的平均收益" : `五组的${metric === "20 日" ? " 20 日" : "（按规则自己的判断）"}超额中位数`, "（小 → 大）"]} /></th>
            <th className="text-right whitespace-nowrap">样本外</th><th className="pl-3 text-left whitespace-nowrap">结论</th></tr>
        </thead>
        <tbody>
          {a.features.map((f) => (
            <tr key={`${f.id}-${f.market}`} className={cn("h-8 border-b border-border/60", f.direction && "bg-primary/5")}>
              <td className="whitespace-nowrap" title={f.expr}>{f.label}</td>
              {(a.markets?.length ?? 1) > 1 && <td className="whitespace-nowrap">{MARKET_NAME[f.market]}</td>}
              <td className="num text-right">{f.groups.length ? <Parts sep=" · " parts={groupMedians(f, "in", by).split(" · ")} /> : "—"}</td>
              <td className="num text-right">{f.groups.length ? <Parts sep=" · " parts={groupMedians(f, "out", by).split(" · ")} /> : "—"}</td>
              <td className={cn("whitespace-nowrap pl-3", f.direction ? "text-foreground" : "text-subtle-foreground")}>
                {f.direction ? ORDER[f.direction] : f.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {a.skipped.length > 0 && (
        <p className="mt-1 text-subtle-foreground">没算：{a.skipped.map((s) => `${s.label}（${MARKET_NAME[s.market]}：${s.why.includes("算不出") ? "信号当天都算不出" : "数据里没有"}）`).join("、")}</p>
      )}
      <p className="mt-1 text-subtle-foreground">每个市场各自分组：分界取它样本内的五分位，样本外用同样的分界；每一组至少 30 个有效样本、两段都一组比一组好（或一组比一组差）才建议。
        {a.stage ? "" : "这条规则的信号不在事件阶段上，加不了“当天”的过滤，只看数字。"}行业和大盘状态的分组在事件研究里。</p>
    </div>
  )
}
