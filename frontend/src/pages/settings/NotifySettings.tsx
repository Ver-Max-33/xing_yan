// 设置 → 通知 (spec §7.1, 1.34): the 开盘前简报 — each market's on / off and time, how much of each section, a preview
// and a copy sent now; 盘中提醒 (only from a realtime source); a test card and the outbox — what was sent, what waits
// for the next try and why. The bot's address and signing secret are keys like any other (above this).
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowDown, ArrowUp, Send, X } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { NumberField } from "@/components/NumberField"
import { Switch } from "@/components/ui/switch"
import { type Market, type Settings, usePutSetting, useSettings } from "@/lib/api"
import { MARKET_NAME, MARKETS } from "@/lib/format"
import { type Brief, type BriefItem, BRIEF_MODE, type BriefMode, briefOn, itemLink, jobEndpoints, useBriefItems, useNotify } from "@/lib/jobs"
import { cn } from "@/lib/utils"

const TAG: Record<string, string> = {
  brief: "简报", signal: "新信号", advance: "潜伏推进", invalid: "失效", summary: "摘要", delay: "数据", watch: "观察提醒", live: "盘中", test: "测试",
}
const ZONE: Record<Market, string> = { CN: "北京时间", JP: "东京时间" }

function BriefView({ b }: { b: Brief }) {
  return (
    <div className="flex flex-col gap-3 text-[0.8125rem]" data-testid="brief-preview">
      <div className="text-[0.9375rem] font-semibold">{b.title}</div>
      {b.warning && <div className="font-medium text-warning">{b.warning}</div>}
      <div className="flex flex-col gap-0.5 text-muted-foreground">{b.head.map((h) => <div key={h}>{h}</div>)}</div>
      {b.sections.map((s) => (
        <div key={s.title} className="flex flex-col gap-0.5 border-t border-border pt-2">
          <div className="font-medium">{s.title}</div>
          {s.lines.map((l, i) => (
            <div key={i} className={cn(l.kind === "group" && "mt-1 font-medium", l.kind === "note" && "text-subtle-foreground")}>
              {l.kind === "stock" ? <><span className="font-medium">{l.name}</span> <span className="num text-subtle-foreground">{l.code}</span> {l.text}</> : l.text}
            </div>
          ))}
        </div>
      ))}
      <div className="border-t border-border pt-2 text-subtle-foreground">{b.footer}</div>
    </div>
  )
}

/** 简报里看哪些 (2026-10-10): the 选股 views put into the briefing, in its order — each listed or counted, moved, taken
 *  out; one the briefing cannot use now says why. */
function BriefItems() {
  const { data: items } = useBriefItems()
  const qc = useQueryClient()
  const change = useMutation({
    mutationFn: (f: () => Promise<BriefItem[]>) => f(),
    onSuccess: (list) => qc.setQueryData(["brief-items"], list),
    onError: (e) => toast.error(String(e)),
  })
  if (!items) return null
  return (
    <div className="flex max-w-[47.5rem] flex-col rounded-md border border-border text-[0.8125rem]" data-testid="brief-items">
      <div className="border-b border-border px-3 py-1.5 text-muted-foreground">简报里看哪些选股（按这个顺序）</div>
      {items.length === 0 && (
        <div className="px-3 py-2 text-subtle-foreground">还没有：在选股页设好规则和附加条件，点「放进开盘前简报」。没放进来的规则照常每天扫描，在「今日」和「选股」里看。</div>
      )}
      {items.map((it, k) => (
        <div key={it.id} className="flex items-center gap-2 border-b border-border/60 px-3 py-1.5 last:border-b-0">
          <div className="min-w-0 flex-1">
            <Link to={itemLink(it)} className="hover:underline" title="到选股页看这个视图">{it.name}</Link>
            {it.markets.length > 0 && <span className="ml-2 text-subtle-foreground">{it.markets.map((m) => MARKET_NAME[m]).join("、")}</span>}
            {it.problem && <div className="text-warning">{it.problem}：简报里会说一句，没有它的结果</div>}
          </div>
          <select aria-label={`${it.name} 在简报里`} value={it.mode} disabled={change.isPending}
            onChange={(e) => { const mode = e.target.value as BriefMode; change.mutate(() => jobEndpoints.briefItemMode(it.id, mode)) }}
            className="h-7 rounded-md border border-input px-1.5">
            {(Object.keys(BRIEF_MODE) as BriefMode[]).map((m) => <option key={m} value={m}>{BRIEF_MODE[m]}</option>)}
          </select>
          <Button size="icon-xs" variant="ghost" aria-label={`上移 ${it.name}`} disabled={k === 0 || change.isPending}
            onClick={() => change.mutate(() => jobEndpoints.moveBriefItem(it.id, -1))}><ArrowUp className="size-3.5" /></Button>
          <Button size="icon-xs" variant="ghost" aria-label={`下移 ${it.name}`} disabled={k === items.length - 1 || change.isPending}
            onClick={() => change.mutate(() => jobEndpoints.moveBriefItem(it.id, 1))}><ArrowDown className="size-3.5" /></Button>
          <Button size="icon-xs" variant="ghost" aria-label={`从简报里去掉 ${it.name}`} disabled={change.isPending}
            onClick={() => change.mutate(() => jobEndpoints.removeBriefItem(it.id))}><X className="size-3.5" /></Button>
        </div>
      ))}
    </div>
  )
}

export function NotifySettings() {
  const { data: settings } = useSettings()
  const put = usePutSetting()
  const { data: notify } = useNotify()
  const qc = useQueryClient()
  const [shown, setShown] = useState<Brief | null>(null)
  const [previewing, setPreviewing] = useState<Market | null>(null)   // the market data is read for (seconds on a whole market)
  const ready = notify?.channels.feishu?.ready ?? false
  const test = useMutation({
    mutationFn: jobEndpoints.testNotify,
    onSuccess: () => toast.success("测试消息已发出，去飞书群里看看"),
    onError: (e) => toast.error(String(e)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["notify"] }),
  })
  const sendNow = useMutation({
    mutationFn: jobEndpoints.sendBrief,
    onSuccess: () => toast.success("发出去了，去飞书群里看看"),
    onError: (e) => toast.error(String(e)),
  })
  if (!settings) return null
  const save = (key: keyof Settings, value: string | boolean | number) => put.mutate({ key, value }, { onError: (e) => toast.error(String(e)) })
  const preview = async (m: Market) => {
    setPreviewing(m)
    try {
      const { brief } = await jobEndpoints.brief(m)
      if (brief) setShown(brief)
      else toast("还没有可以预览的：这个市场没有数据")
    } catch (e) {
      toast.error(String(e))
    } finally {
      setPreviewing(null)
    }
  }
  const st = notify?.stats
  return (
    <div className="mt-4 flex flex-col gap-5 text-[0.875rem]">
      <section className="flex flex-col gap-2" data-testid="brief-settings">
        <div className="font-medium">开盘前简报</div>
        <p className="max-w-[47.5rem] text-[0.8125rem] text-subtle-foreground">
          每个交易日开盘前一张卡片：你的观察里有事的；你放进来的选股（下面的列表）的新信号、快到突破的潜伏或数量；大盘。等早上的订正做完再发，最晚开盘前 10 分钟；那时应用没开，就在你打开时补发（收盘前）。
        </p>
        {MARKETS.map((m) => (
          <div key={m} className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Switch checked={briefOn(settings, m)} aria-label={`${MARKET_NAME[m]}的开盘前简报`}
              onCheckedChange={(v) => save(`brief.${m}`, v ? "on" : "off")} />
            <span className="w-10">{MARKET_NAME[m]}</span>
            <Input type="time" className="h-8 w-28" aria-label={`${MARKET_NAME[m]}简报的时间`} value={settings[`brief.time.${m}`]}
              onChange={(e) => /^\d\d:\d\d$/.test(e.target.value) && save(`brief.time.${m}`, e.target.value)} />
            <span className="text-[0.8125rem] text-subtle-foreground">{ZONE[m]}{settings[`brief.${m}`] === "auto" ? " · 默认：正式档开、测试档关" : ""}</span>
            <Button size="sm" variant="ghost" aria-label={`预览 ${MARKET_NAME[m]}的简报`} disabled={previewing !== null} onClick={() => void preview(m)}>
              {previewing === m ? "排版中…" : "预览"}</Button>
            <Button size="sm" variant="ghost" disabled={!ready || sendNow.isPending} onClick={() => sendNow.mutate(m)}
              title={ready ? "用最新的数据现在发一份（测试用，不算当天那一份；要取公告，一两分钟）" : "先填上面的 Webhook 地址"}>
              {sendNow.isPending && sendNow.variables === m ? "发送中…" : "发一份到飞书"}</Button>
          </div>
        ))}
        <BriefItems />
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.8125rem]">
          <label className="flex items-center gap-1.5">每项最多列
            <NumberField commitOn="blur" rule={{ int: true, min: 1, max: 10 }} className="h-7 w-16" aria-label="每项最多列几只"
              value={settings["brief.per_rule"]} onChange={(v) => save("brief.per_rule", v!)} />只</label>
          <label className="flex items-center gap-1.5">快到突破：离上沿
            <NumberField commitOn="blur" rule={{ int: true, min: 1, max: 10 }} className="h-7 w-16" aria-label="离上沿多少以内"
              value={settings["brief.near_pct"]} onChange={(v) => save("brief.near_pct", v!)} />% 以内，最多
            <NumberField commitOn="blur" rule={{ int: true, min: 1, max: 20 }} className="h-7 w-16" aria-label="快到突破最多几只"
              value={settings["brief.near_max"]} onChange={(v) => save("brief.near_max", v!)} />只</label>
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={settings["brief.empty"]}
            onChange={(e) => save("brief.empty", e.target.checked)} />没有要看的一天也发一张</label>
        </div>
      </section>
      <section className="flex flex-col gap-1">
        <label className="flex items-center gap-2"><input type="checkbox" checked={settings["notify.live"]} aria-label="盘中提醒"
          onChange={(e) => save("notify.live", e.target.checked)} />盘中提醒</label>
        <span className="text-[0.8125rem] text-subtle-foreground">
          只从实时行情发：{MARKETS.map((m) => `${MARKET_NAME[m]}${notify?.live?.[m]?.realtime ? "是实时的" : "不是（不发）"}`).join("，")}。
        </span>
      </section>
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={!ready || test.isPending} onClick={() => test.mutate()}><Send className="size-4" />发测试消息</Button>
        <span className={cn("text-[0.8125rem]", notify?.channels.feishu?.problem ? "text-warning" : "text-subtle-foreground")}>
          {ready ? "只发到你自己的机器人。"
            : `${notify?.channels.feishu?.problem ?? "先填上面的 Webhook 地址"}；在那之前提醒先存着，能发了再发。`}
        </span>
      </div>
      {st && (
        <div className="text-[0.8125rem] text-muted-foreground">
          待发 <span className="num">{st.pending}</span> · 放弃 <span className="num">{st.given_up}</span>
          {st.last_error && <span className="text-warning"> · 最近一次出错：{st.last_error}</span>}
        </div>
      )}
      {!!notify?.recent.length && (
        <div className="max-h-[15rem] overflow-y-auto rounded-md border border-border">
          {notify.recent.map((n) => (
            <div key={n.id} className="flex items-center gap-3 border-b border-border/60 px-3 py-1.5 text-[0.8125rem] last:border-b-0">
              <span className="num w-28 shrink-0 text-subtle-foreground">{n.created_at.slice(5, 16).replace("T", " ")}</span>
              <span className="w-14 shrink-0 text-muted-foreground">{TAG[n.kind] ?? n.kind}</span>
              <span className="min-w-0 flex-1 truncate" title={n.payload.line}>{n.payload.title}</span>
              <span className={cn("shrink-0", n.sent_at ? "text-down" : n.attempts >= 12 ? "text-error" : "text-subtle-foreground")} title={n.last_error ?? ""}>
                {n.sent_at ? "已发" : n.attempts >= 12 ? "放弃" : n.attempts ? `待重试（${n.attempts} 次）` : "待发"}
              </span>
            </div>
          ))}
        </div>
      )}
      <Dialog open={shown !== null} onOpenChange={(o) => { if (!o) setShown(null) }}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>简报预览</DialogTitle>
            <DialogDescription>现在发会是这样（预览不取公告，发的时候才取）</DialogDescription>
          </DialogHeader>
          {shown && <BriefView b={shown} />}
        </DialogContent>
      </Dialog>
    </div>
  )
}
