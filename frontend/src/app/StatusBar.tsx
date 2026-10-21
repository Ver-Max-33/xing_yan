// Always-visible state (design principle "状态始终在场"): data watermark per market, data checks, the daily
// scan, whether notices reach Feishu, which profile each market runs on, and a newer version published on GitHub (and
// how far its download is: 设置 → 关于 installs it).
import { Activity, ArrowUpCircle, CircleAlert, CircleCheck, CircleDashed, ScanLine, Send, ShieldCheck } from "lucide-react"
import { Link } from "react-router"
import { type MarketStatus, useStatus } from "@/lib/api"
import { shortDate } from "@/lib/format"
import { doing, type JobState, useJobs } from "@/lib/jobs"
import { feedAge, lateText, useLive, useNow } from "@/lib/live"
import { useBoardAt } from "@/lib/market"
import { updateChip, useUpdate, useUpdatedNotice, useUpdateDownload } from "@/lib/update"
import { cn } from "@/lib/utils"

function marketLine(m: MarketStatus): { text: string; tone: "ok" | "warn" | "none" } {
  if (!m.snapshot) return { text: `${m.market} 无数据`, tone: "none" }
  const at = m.snapshot.created_at.slice(11, 16)
  if (m.pending_units > 0) return { text: `${m.market} ${shortDate(m.snapshot.as_of)} · 有 ${m.pending_units} 个单元未发布`, tone: "warn" }
  return { text: `${m.market} ${shortDate(m.snapshot.as_of)} 已发布 ${at}`, tone: "ok" }
}

const RUNNING: Record<string, string> = { update: "更新中", scan: "扫描中", research: "AI 研究中", check: "抽样核对中", backfill: "补历史中" }

function scanLine(j: JobState): { text: string; tone: "ok" | "warn" | "error" } {
  if (j.running) {   // what the turn is at; a scan, how far (the whole line is the item's title)
    const p = j.progress
    const share = p?.step === "scan" && p.fraction != null ? ` ${Math.round(p.fraction * 100)}%` : "…"
    const now = p?.cancelling ? "取消中" : p?.pausing ? "暂停中" : p?.waiting ? "等数据" : RUNNING[p?.step ?? ""] ?? "准备中"
    return { text: `${j.market} ${now}${share}`, tone: "ok" }
  }
  if (j.status === "paused") return { text: `${j.market} 扫描已暂停`, tone: "warn" }
  if (j.status === "cancelled") return { text: `${j.market} 扫描已取消`, tone: "warn" }
  if (j.status === "delayed") return { text: `${j.market} 数据延迟`, tone: "warn" }
  if (j.status === "interrupted") return { text: `${j.market} 补历史没取完`, tone: "warn" }
  if (j.status === "error") return { text: `${j.market} 扫描出错`, tone: "error" }
  const rules = Object.keys(j.scan_summary).length
  return { text: j.scan_at ? `${j.market} 扫描 ${j.scan_at.slice(11, 16)} · ${rules} 条规则` : `${j.market} 未扫描`, tone: "ok" }
}

// a link's focus ring drawn inside it: the bar clips anything outside its 28 px
const FOCUS = "outline-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset"

export function StatusBar() {
  const { data, isError } = useStatus()
  const { data: jobs } = useJobs()
  const { data: update } = useUpdate()
  const { data: download } = useUpdateDownload()
  useUpdatedNotice(update?.current)
  const chip = updateChip(update, download)
  const markets = data?.markets ?? []
  const box = jobs?.outbox
  const live = (useLive().data?.markets ?? []).filter((m) => m.session.state === "trading" || m.session.state === "lunch")
  const now = useNow(), boardAt = useBoardAt("CN").data
  return (
    <footer className="flex h-7 shrink-0 items-center gap-4 border-t border-border bg-sidebar px-3 text-[0.75rem] whitespace-nowrap text-subtle-foreground">
      {/* when the bar is short of room, these drop out whole from the right (each wraps to a line that is cut off);
          the Feishu state and the profiles on the right are always there */}
      <div className="flex h-full min-w-0 flex-1 flex-wrap items-center gap-x-4 overflow-hidden">
      {isError && <span className="flex h-full items-center gap-1 text-error"><CircleAlert className="size-3.5" />后端未连接</span>}
      {markets.map((m) => {
        const l = marketLine(m)
        const Icon = l.tone === "ok" ? CircleCheck : l.tone === "warn" ? CircleAlert : CircleDashed
        return (
          <Link key={m.market} to="/data" className={cn("flex h-full items-center gap-1 hover:text-foreground", FOCUS, l.tone === "warn" && "text-warning")}>
            <Icon className="size-3.5" /> <span className="num">{l.text}</span>
          </Link>
        )
      })}
      {markets.filter((m) => m.crosscheck.checked > 0).map((m) => (
        <Link key={`cc-${m.market}`} to="/data" title="与独立来源抽样核对（只报告，从不改数据）"
          className={cn("flex h-full items-center gap-1 hover:text-foreground", FOCUS, m.crosscheck.ok < m.crosscheck.checked && "text-error")}>
          <ShieldCheck className="size-3.5" /> <span className="num">{m.market} 核对 {m.crosscheck.ok}/{m.crosscheck.checked}</span>
        </Link>
      ))}
      {jobs && (
        <Link to="/" className={cn("flex h-full items-center gap-3 hover:text-foreground", FOCUS)} data-testid="status-scan">
          {jobs.markets.filter((j) => j.latest || j.running || j.status === "interrupted").map((j) => {   // a first 补历史 too
            const l = scanLine(j)
            return <span key={j.market} title={j.running ? doing(j) : j.message ?? ""} className={cn("flex items-center gap-1", l.tone === "warn" && "text-warning", l.tone === "error" && "text-error")}>
              <ScanLine className="size-3.5" /><span className="num">{l.text}</span></span>
          })}
        </Link>
      )}
      {live.length > 0 && (   // each market's session and its prices' time; amber when either feed has stopped 3 minutes (10-12 rule)
        <Link to="/live" className={cn("flex h-full items-center gap-3 hover:text-foreground", FOCUS)} data-testid="status-live">
          {live.map((m) => {
            const age = feedAge(m, m.market === "CN" ? boardAt : null, now)
            return age?.late ? (
              <span key={m.market} className="flex items-center gap-1 text-warning"
                title={`盯盘轮询 ${m.at.slice(11, 19)}${m.quote_at || boardAt ? ` · 全市场行情 ${(boardAt ?? m.quote_at ?? "").slice(11, 19)}` : ""}`}>
                <Activity className="size-3.5" />{m.market} {lateText(age)}</span>
            ) : (
              <span key={m.market} className="flex items-center gap-1"><Activity className="size-3.5" />{m.market} {m.session.label}{" "}
                {m.session.state === "trading" && <span className="num">{m.at.slice(11, 16)}</span>}</span>
            )
          })}
        </Link>
      )}
      </div>
      <div className="flex shrink-0 items-center gap-4">
      {chip && (   // 设置 → 关于: the release notes and 更新到 x.y.z
        <Link to="/settings?s=about" data-testid="status-update"
          title={update?.newer ? `形研 ${update.latest}${update.published_at ? `（${update.published_at.slice(0, 10)} 发布）` : ""}：在 设置 → 关于 里更新` : undefined}
          className={cn("num flex h-full items-center gap-1 text-structure hover:text-foreground", FOCUS)}>
          <ArrowUpCircle className="size-3.5" />{chip}
        </Link>
      )}
      {jobs && (
        <Link to="/settings?s=notify" title={box?.last_error ?? "飞书机器人（设置 → 通知）"}
          className={cn("flex h-full items-center gap-1 hover:text-foreground", FOCUS, jobs.feishu && (box?.given_up || box?.last_error) && "text-warning")}>
          <Send className="size-3.5" />
          {!jobs.feishu ? "飞书 未配置" : box?.given_up ? `飞书 ${box.given_up} 条没发出` : box?.pending ? `飞书 ${box.pending} 条待发` : "飞书 正常"}
        </Link>
      )}
      {markets.map((m) => (
        <span key={`p-${m.market}`} className={cn(m.profile === "test" && "text-warning")}>
          {m.market} {m.profile === "test" ? "测试档" : "正式档"}
        </span>
      ))}
      </div>
    </footer>
  )
}
