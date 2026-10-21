// 公司 (股票资料, spec §10.3): who the company is, how big, who holds it, what to watch and how the last report went —
// each part a line that says it, its figures under it. From Tushare, asked once a day; only what was announced by then
// (a report's holders from its announcement day). The holders are grouped by their type and, for 外资 and 国资, their name.
import { useQueryClient } from "@tanstack/react-query"
import { Loader2, RefreshCw } from "lucide-react"
import { type ReactNode, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { type HolderTable, type Profile, profileEndpoint, useProfile, yi } from "@/lib/profile"
import { stockIndustry } from "@/lib/marketWords"
import { cn } from "@/lib/utils"

const pct = (v: number | null | undefined, digits = 2, signed = false) =>
  v === null || v === undefined ? "—" : `${signed && v > 0 ? "+" : ""}${v.toFixed(digits)}%`
const STATE_CLASS: Record<string, string> = { 新进: "text-up", 增持: "text-up", 减持: "text-down", 不变: "text-muted-foreground" }

function Part({ title, line, children }: { title: string; line: string | undefined; children: ReactNode }) {
  return (
    <section className="border-b border-border pb-3">
      <h3 className="text-[0.75rem] text-subtle-foreground">{title}</h3>
      <p className="mt-0.5 text-[0.875rem] leading-relaxed">{line ?? "—"}</p>
      <details className="mt-1 text-[0.8125rem]">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">明细</summary>
        <div className="mt-2 flex flex-col gap-2">{children}</div>
      </details>
    </section>
  )
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="flex gap-2"><span className="w-20 shrink-0 text-muted-foreground">{k}</span><span className="min-w-0 break-words">{v}</span></div>
}

function Holders({ t, title, base }: { t: HolderTable | null | undefined; title: string; base: string }) {
  if (!t) return <p className="text-muted-foreground">{title}：没有取到</p>
  return (
    <div>
      <div className="text-muted-foreground">{title} · {t.period} 报告期（{t.known} 公告）</div>
      <Table className="text-[0.8125rem]">
        <TableHeader><TableRow><TableHead>股东</TableHead><TableHead>类型</TableHead><TableHead className="text-right">{base}</TableHead><TableHead className="text-right">本期</TableHead></TableRow></TableHeader>
        <TableBody>
          {t.rows.map((r) => (
            <TableRow key={r.name}>
              <TableCell className="max-w-[11rem] truncate" title={r.name}>{r.name}</TableCell>
              <TableCell title={r.type ? `Tushare 的类型：${r.type}` : undefined}>{r.group}</TableCell>
              <TableCell className="num text-right">{pct(r.ratio)}</TableCell>
              <TableCell className={cn("text-right", STATE_CLASS[r.state])}>{r.state}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {t.left.length > 0 && <p className="mt-1 text-muted-foreground">上期在、这期不在前十：{t.left.join("、")}</p>}
    </div>
  )
}

function Body({ p }: { p: Profile }) {
  const c = p.company!, h = p.holders!, r = p.risk!, f = p.finance, s = p.scale
  const ah = h.float10?.rows.some((x) => x.group === "H股代持") ?? false
  return (
    <>
      <Part title="做什么的" line={p.summary?.company}>
        {c.main_business && <Row k="主营" v={c.main_business} />}
        {c.segments && (
          <div>
            <div className="text-muted-foreground">收入构成 · {c.segments.period}（{c.segments.known} 公告）</div>
            {c.segments.items.slice(0, 6).map((x) => (
              <div key={x.name} className="flex items-center gap-2">
                <span className="w-28 shrink-0 truncate" title={x.name}>{x.name}</span>
                <span className="h-1.5 flex-1 rounded bg-muted"><span className="block h-1.5 rounded bg-structure" style={{ width: `${Math.max(1, (x.share ?? 0) * 100)}%` }} /></span>
                <span className="num w-12 text-right">{x.share === null ? "—" : `${(x.share * 100).toFixed(0)}%`}</span>
                <span className="num w-16 text-right text-muted-foreground" title="毛利率">{x.margin === null ? "" : `毛利 ${(x.margin * 100).toFixed(0)}%`}</span>
              </div>
            ))}
          </div>
        )}
        <Row k="行业" v={stockIndustry(c.industry, c.industry2) ?? "—"} />
        <Row k="地区" v={c.region ?? "—"} />
        <Row k="成立" v={c.setup_date ?? "—"} />
        <Row k="员工" v={c.employees ? `${c.employees.toLocaleString("zh-CN")} 人` : "—"} />
        <Row k="董事长" v={c.chairman ?? "—"} />
        {c.introduction && <Row k="简介" v={c.introduction} />}
      </Part>
      <Part title="规模和估值" line={p.summary?.scale}>
        {s ? (
          <>
            <Row k="总市值" v={yi(s.total_mv, "元")} />
            <Row k="流通市值" v={yi(s.circ_mv, "元")} />
            <Row k="市盈率 TTM" v={s.pe_ttm === null ? "为负（近四个季度合计亏损）" : s.pe_ttm.toFixed(1)} />
            <Row k="市净率" v={s.pb === null ? "—" : s.pb.toFixed(2)} />
            <Row k="股息率 TTM" v={pct(s.dv_ttm)} />
            <p className="text-subtle-foreground">{s.date} 收盘</p>
          </>
        ) : <p className="text-muted-foreground">没有估值数据</p>}
      </Part>
      <Part title="谁在持有" line={p.summary?.holders}>
        <Holders t={h.float10} title="前十大流通股东" base={ah ? "占 A 股流通股" : "占流通股"} />
        <Holders t={h.top10} title="前十大股东" base="占总股本" />
        {ah && <p className="text-muted-foreground">H股代持：香港中央结算（代理人）替 H 股持有人登记的股份，背后是谁看不到，不算作外资。</p>}
        <div>
          <div className="text-muted-foreground">外资</div>
          {h.foreign.hk ? (
            <p>陆股通（北向）持有 A 股的 {pct(h.foreign.hk.ratio)}（{h.foreign.hk.date}）；近几期：{h.foreign.hk.series.map((x) => `${x.date.slice(0, 7)} ${pct(x.ratio)}`).join("，")}。港交所 2024 年 8 月起只按季度公布。</p>
          ) : <p className="text-muted-foreground">没有陆股通持股记录</p>}
          {h.foreign.named.filter((x) => !x.name.includes("香港中央结算")).map((x) => <p key={x.name}>{x.name}：占流通股 {pct(x.ratio)}</p>)}
        </div>
        <div>
          <div className="text-muted-foreground">股东户数（户数减少，筹码在集中）</div>
          {h.numbers.slice().reverse().map((n) => (
            <div key={n.period} className="flex gap-2">
              <span className="num w-24 shrink-0">{n.period}</span>
              <span className="num w-20 text-right">{n.number?.toLocaleString("zh-CN") ?? "—"}</span>
              <span className={cn("num w-16 text-right", n.change !== null && (n.change < 0 ? "text-up" : "text-down"))}>
                {n.change === null ? "" : `${n.change > 0 ? "+" : ""}${(n.change * 100).toFixed(1)}%`}</span>
            </div>
          ))}
        </div>
      </Part>
      <Part title="要留意" line={p.summary?.risk}>
        <Row k="股权质押" v={r.pledge ? `${pct(r.pledge.ratio)}（${r.pledge.date}，${r.pledge.count ?? "—"} 笔）` : "没有"} />
        <div>
          <div className="text-muted-foreground">未来 90 天限售解禁</div>
          {r.unlocks.length ? r.unlocks.map((u) => (
            <p key={u.date}>{u.date}：{yi(u.shares, "股")}（占总股本 {pct(u.ratio)}，{u.holders} 位股东，{u.types.join("、")}）</p>
          )) : <p className="text-muted-foreground">没有</p>}
        </div>
        <div>
          <div className="text-muted-foreground">近一年股东和高管增减持（按公告日）</div>
          {r.trades.length ? r.trades.slice(0, 12).map((t, i) => (
            <p key={`${t.ann_date}-${t.holder}-${i}`}>
              <span className="num">{t.ann_date}</span> {t.holder}（{t.holder_type}）
              <span className={t.direction === "增持" ? "text-up" : "text-down"}> {t.direction}</span> {yi(t.shares, "股")}
              {t.ratio !== null && `，占 ${t.ratio.toFixed(2)}%`}{t.avg_price ? `，均价 ${t.avg_price.toFixed(2)}` : ""}
            </p>
          )) : <p className="text-muted-foreground">没有</p>}
        </div>
      </Part>
      <Part title="财务摘要" line={p.summary?.finance}>
        {f ? (
          <>
            <Row k="报告期" v={`${f.period_name}（${f.known} 公告）`} />
            <Row k="营收同比" v={pct(f.revenue_yoy, 1, true)} />
            <Row k="净利润同比" v={pct(f.profit_yoy, 1, true)} />
            <Row k="毛利率" v={pct(f.gross_margin, 1)} />
            <Row k="净利率" v={pct(f.net_margin, 1)} />
            <Row k="ROE" v={pct(f.roe)} />
            <Row k="资产负债率" v={pct(f.debt_to_assets, 1)} />
          </>
        ) : <p className="text-muted-foreground">没有取到</p>}
      </Part>
    </>
  )
}

export function CompanyProfile({ listingId }: { listingId: string }) {
  const q = useProfile(listingId)
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const again = async () => {
    setBusy(true)
    try {
      qc.setQueryData(["profile", listingId], await profileEndpoint(listingId, true))
    } catch (e) {
      toast.error(`没取到：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }
  if (q.isLoading) return <p className="py-8 text-center text-[0.875rem] text-muted-foreground">读取…（当天第一次打开要几秒：从 Tushare 取）</p>
  if (q.error || !q.data) return <p className="py-8 text-center text-[0.875rem] text-error">没取到公司资料：{String(q.error)}</p>
  const p = q.data
  if (p.unavailable) return <p className="py-8 text-center text-[0.875rem] text-muted-foreground">{p.unavailable}</p>
  const errors = Object.entries(p.errors)
  return (
    <div className="flex flex-col gap-3 py-3" data-testid="company-profile">
      {p.company?.name && <div className="text-[0.9375rem] font-medium">{p.company.name}</div>}
      <Body p={p} />
      {errors.length > 0 && (
        <div className="text-[0.75rem] text-warning">这几部分没取到：{errors.map(([k, v]) => `${k}（${v}）`).join("；")}</div>
      )}
      <div className="flex items-center gap-2 pb-2 text-[0.75rem] text-subtle-foreground">
        <span className="flex-1">来源 Tushare · {p.fetched_at?.replace("T", " ").slice(0, 16)} 取 · 只含 {p.as_of} 为止已公布的</span>
        <Button size="sm" variant="ghost" onClick={() => void again()} disabled={busy}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}重新获取</Button>
      </div>
    </div>
  )
}
