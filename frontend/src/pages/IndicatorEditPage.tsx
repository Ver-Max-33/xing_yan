// 指标库 · one indicator (design 2026-10-10 §5): its name, words, where it is drawn, markets, parameters, reference lines
// and the formula as 通达信 / 同花顺 write it. 检查 runs while you type: what is wrong by line (a click goes to the line;
// a name the formula does not know can become a parameter), what this system has no data for (and the formula without
// it), what it draws and where, and its last bars on a stock. A built-in one is read-only: copy it to change it.
import { useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, Copy, FileUp, LineChart, Minus, Plus } from "lucide-react"
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { toast } from "sonner"
import { ListingPicker } from "@/components/rules/ListingPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { type Market, useDataIn } from "@/lib/api"
import { codeOf, MARKET_NAME } from "@/lib/format"
import {
  type CheckResult, EMPTY_FORM, fromIndicator, type IndicatorForm, indicatorEndpoints, looksLikeYaml, type Pane, toYaml, useIndicator,
} from "@/lib/indicators"
import { readPersisted, writePersisted } from "@/lib/usePersisted"
import { cn, readText } from "@/lib/utils"

const KIND: Record<string, string> = {
  line: "线", dots: "点", stick: "柱", colorstick: "红绿柱", volstick: "量柱", linestick: "线加柱", stickline: "柱（STICKLINE）",
  icon: "图标", text: "文字", number: "数字", band: "填色", zone: "区间", level: "水平线",
}
const EXAMPLE = "例如，把通达信 / 同花顺的公式直接粘进来：\nMA5:MA(CLOSE,5),COLORRED;\nMA20:MA(CLOSE,N);\nDRAWICON(CROSS(MA5,MA20),LOW,1);"

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[0.8125rem] text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="text-[0.75rem] text-subtle-foreground">{hint}</span>}
    </label>
  )
}

export default function IndicatorEditPage() {
  const { id = "new" } = useParams()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const from = search.get("from")
  const doc = useIndicator(id === "new" ? from ?? undefined : id)
  const builtin = id !== "new" && !!doc.data?.builtin
  const [form, setForm] = useState<IndicatorForm>(EMPTY_FORM)
  const [loadedFor, setLoadedFor] = useState<string | null>(null)
  const [picked, setStock] = useState<string | null>(() => readPersisted<string>("kline.last", "") || null)
  const dataIn = useDataIn()
  const stock = picked && dataIn.has(picked.split(":")[0] as Market) ? picked : null   // not one whose market has no data now
  const [checked, setCheck] = useState<CheckResult | null>(null)
  const [checking, setChecking] = useState(false)
  const [saving, setSaving] = useState(false)
  const formula = useRef<HTMLTextAreaElement>(null)
  const file = useRef<HTMLInputElement>(null)

  // the indicator opened (or copied) fills the form once
  const key = `${id}|${from ?? ""}|${doc.data?.version ?? ""}`
  if (doc.data && loadedFor !== key) {
    setLoadedFor(key)
    const f = fromIndicator(doc.data.indicator)
    setForm(id === "new" ? { ...f, id: "", name: `${f.name}（副本）` } : f)
  }
  useEffect(() => { if (search.get("paste")) formula.current?.focus() }, [search])

  const text = useMemo(() => toYaml(form), [form])
  const market: Market | undefined = stock ? (stock.split(":")[0] as Market) : undefined
  useEffect(() => {   // 检查 a moment after the typing stops
    if (!form.formula.trim()) return
    let gone = false
    const t = window.setTimeout(() => {
      setChecking(true)
      indicatorEndpoints.check(text, stock, market)
        .then((r) => { if (!gone) setCheck(r) })
        .catch((e) => { if (!gone) setCheck({ ok: false, errors: [{ line: 0, message: String(e) }], unsupported: [], reduced: null, items: [], sample: null, indicator: null }) })
        .finally(() => { if (!gone) setChecking(false) })
    }, 600)
    return () => { gone = true; window.clearTimeout(t) }
  }, [text, stock, market, form.formula])

  const check = form.formula.trim() ? checked : null   // nothing written: nothing checked
  const set = <K extends keyof IndicatorForm>(k: K, v: IndicatorForm[K]) => setForm((f) => ({ ...f, [k]: v }))
  const setParam = (i: number, patch: Partial<IndicatorForm["params"][number]>) =>
    setForm((f) => ({ ...f, params: f.params.map((p, j) => (j === i ? { ...p, ...patch } : p)) }))
  const addParam = (name = "") => setForm((f) => ({ ...f, params: [...f.params, { id: name, value: 1, range: null, step: null }] }))

  /** The cursor to a line of the formula (an error's). */
  const toLine = (line: number) => {
    const el = formula.current
    if (!el || line < 1) return
    const lines = el.value.split("\n")
    const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0)
    el.focus()
    el.setSelectionRange(start, start + (lines[line - 1]?.length ?? 0))
  }
  // a name the formula does not know, written like 通达信's parameters (N, M1, SHORT): offered as a parameter
  const unknown = (m: string) => {
    const name = /不认识的名字 ([A-Za-z_][A-Za-z0-9_]*)/.exec(m)?.[1]
    return name && /^[A-Z][A-Z0-9_]{0,7}$/.test(name) && !form.params.some((p) => p.id.toUpperCase() === name) ? name : null
  }

  const save = async () => {
    if (!form.name.trim()) { toast.error("先给指标起个名字"); return }
    setSaving(true)
    try {
      const r = await indicatorEndpoints.save(text, id === "new")
      await qc.invalidateQueries({ queryKey: ["indicators"] })
      await qc.invalidateQueries({ queryKey: ["indicator", r.id] })
      toast(r.created ? `已保存为 v${r.version}` : "内容没变，没有新版本")
      if (id === "new") navigate(`/indicators/${r.id}`, { replace: true })
    } catch (e) {
      toast.error(`没保存：${String(e)}`)
    } finally {
      setSaving(false)
    }
  }
  const showOnChart = (iid: string) => {
    const on = readPersisted<string[]>("kline.lib", [])
    writePersisted("kline.lib", [...on.filter((x) => x !== iid), iid])
    navigate(`/k/${stock ?? ""}`)
  }
  const importYaml = async (f: File | undefined) => {
    if (!f) return
    try {
      const text = await readText(f)   // a formula 通达信 / 同花顺 saved on Windows is GBK
      if (!looksLikeYaml(text)) {        // a formula alone (a .txt): it goes into the formula, as if pasted
        setForm({ ...form, name: form.name || f.name.replace(/\.[^.]+$/, ""), formula: text.replace(/\r\n?/g, "\n").trim() })
        return
      }
      const r = await indicatorEndpoints.check(text)
      if (!r.indicator) { toast.error(`读不出来：${r.errors[0]?.message ?? ""}`); return }
      setForm({ ...fromIndicator(r.indicator), id: id === "new" ? "" : form.id })
      toast(`已读入「${r.indicator.name}」`)
    } catch (e) {
      toast.error(`读不出来：${String(e)}`)
    }
  }

  const title = id === "new" ? (from ? "复制指标" : "新建指标") : form.name || "指标"
  return (
    <div className="flex h-full flex-col">
      <header className="drag clear-window-buttons flex min-h-11 shrink-0 flex-wrap items-center gap-2 border-b border-border px-4 py-1.5">
        <Button variant="ghost" size="icon-sm" className="no-drag" aria-label="回到指标库" asChild>
          <Link to="/indicators"><ArrowLeft className="size-4" /></Link>
        </Button>
        <h1 className="no-drag truncate text-[0.9375rem] font-medium">{title}</h1>
        {builtin && <Badge variant="outline" className="h-5 px-1.5 text-[0.75rem] font-normal text-muted-foreground">内置 · 只读</Badge>}
        {id !== "new" && !builtin && doc.data && <Badge variant="outline" className="num h-5 px-1.5 text-[0.75rem] font-normal">v{doc.data.version}</Badge>}
        <span className="flex-1" />
        <input ref={file} type="file" accept=".yaml,.yml,.txt" className="hidden" aria-label="选择 YAML 或公式文件"
          onChange={(e) => { void importYaml(e.target.files?.[0]); e.target.value = "" }} />
        {!builtin && <Button variant="ghost" size="sm" className="no-drag" onClick={() => file.current?.click()}
          title="指标的 YAML，或通达信 / 同花顺公式存成的 .txt（GBK 也认得）"><FileUp className="size-4" />导入文件</Button>}
        {id !== "new" && <Button variant="outline" size="sm" className="no-drag" onClick={() => navigate(`/indicators/new?from=${encodeURIComponent(id)}`)}>
          <Copy className="size-4" />复制为新指标</Button>}
        {id !== "new" && <Button variant="outline" size="sm" className="no-drag" onClick={() => showOnChart(id)}><LineChart className="size-4" />在 K 线里看</Button>}
        {!builtin && <Button size="sm" className="no-drag" disabled={saving || !check?.ok} onClick={() => void save()}
          title={check?.ok ? undefined : "检查通过才能保存"}>{saving ? "保存中…" : "保存"}</Button>}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto grid max-w-[90rem] gap-6 p-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <fieldset disabled={builtin} className="flex min-w-0 flex-col gap-4">
            <Field label="名称"><Input aria-label="名称" value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="例如：主力动能" /></Field>
            <Field label="说明" hint="一两句话：它看什么、怎么看">
              <Textarea aria-label="说明" rows={3} value={form.doc} onChange={(e) => set("doc", e.target.value)} />
            </Field>
            <Field label="画在" hint="自动：画出的都是价格（均线、通道）就叠在 K 线上，否则另开一个副图">
              <ToggleGroup type="single" variant="outline" size="sm" value={form.pane} className="justify-start"
                onValueChange={(v) => { if (v) set("pane", v as Pane) }}>
                <ToggleGroupItem value="auto">自动</ToggleGroupItem><ToggleGroupItem value="main">主图</ToggleGroupItem>
                <ToggleGroupItem value="sub">副图</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            <div className="flex flex-col gap-1">
              <span className="text-[0.8125rem] text-muted-foreground">市场</span>
              {(["CN", "JP"] as Market[]).map((m) => (
                <label key={m} className="flex h-7 items-center justify-between text-[0.875rem]">{MARKET_NAME[m]}
                  <Switch checked={form.markets.includes(m)} aria-label={MARKET_NAME[m]}
                    onCheckedChange={(on) => set("markets", on ? [...new Set([...form.markets, m])].sort() as Market[] : form.markets.filter((x) => x !== m))} />
                </label>
              ))}
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center text-[0.8125rem] text-muted-foreground">参数
                <span className="ml-1 text-[0.75rem] text-subtle-foreground">（公式里直接写名字，例如 N）</span>
                {!builtin && <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="加一个参数" onClick={() => addParam()}><Plus className="size-4" /></Button>}
              </div>
              {form.params.length === 0 && <span className="text-[0.8125rem] text-subtle-foreground">没有参数</span>}
              {form.params.map((p, i) => (
                <div key={i} className="grid grid-cols-[minmax(0,1fr)_5rem_1.75rem] items-center gap-1.5">
                  <Input aria-label={`参数 ${i + 1} 的名字`} value={p.id} className="num h-8" placeholder="N"
                    onChange={(e) => setParam(i, { id: e.target.value.trim() })} />
                  <Input aria-label={`参数 ${p.id || i + 1} 的值`} type="number" value={Number.isFinite(p.value) ? p.value : ""} className="num h-8"
                    onChange={(e) => setParam(i, { value: Number(e.target.value) })} />
                  {!builtin && <Button variant="ghost" size="icon-sm" aria-label={`去掉参数 ${p.id}`}
                    onClick={() => setForm((f) => ({ ...f, params: f.params.filter((_, j) => j !== i) }))}><Minus className="size-4" /></Button>}
                </div>
              ))}
            </div>
            <Field label="参考线" hint="副图里的水平虚线，逗号隔开，例如 20, 50, 80">
              <Input aria-label="参考线" value={form.levels} onChange={(e) => set("levels", e.target.value)} className="num" />
            </Field>
            <Field label="出处"><Input aria-label="出处" value={form.source} onChange={(e) => set("source", e.target.value)} placeholder="可不填" /></Field>
          </fieldset>
          <div className="flex min-w-0 flex-col gap-3">
            <div className="flex items-baseline gap-2 text-[0.8125rem] text-muted-foreground">
              <span className="shrink-0">公式</span>
              <span className="min-w-0 text-[0.75rem] text-subtle-foreground">通达信 / 同花顺的写法：名字:=公式 是中间变量，名字:公式 画线，画线属性跟在逗号后面</span>
            </div>
            <Textarea ref={formula} aria-label="公式" spellCheck={false} wrap="off" rows={16} readOnly={builtin}
              className="num min-h-[18rem] font-mono text-[0.8125rem] leading-6" placeholder={EXAMPLE}
              value={form.formula} onChange={(e) => set("formula", e.target.value)} />
            <section aria-label="检查结果" className="flex flex-col gap-3 text-[0.875rem]" data-testid="indicator-check">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">检查</span>
                <span className={cn("text-[0.8125rem]", !check ? "text-subtle-foreground" : check.ok ? "text-down" : "text-error")}>
                  {checking ? "检查中…" : !check ? "写了公式就检查" : check.ok ? `能画：${check.pane === "main" ? "叠在 K 线上" : "另开一个副图"}` : "还不能画"}</span>
                <span className="ml-auto flex items-center gap-2 text-[0.8125rem] text-muted-foreground">在这只股票上算<ListingPicker value={stock} onPick={setStock} /></span>
              </div>
              {check && check.errors.length > 0 && (
                <ul className="flex flex-col gap-1" data-testid="indicator-errors">
                  {check.errors.map((e, i) => {
                    const name = unknown(e.message)
                    return (
                      <li key={i} className="flex flex-wrap items-baseline gap-x-2 text-error">
                        {e.line > 0 ? <button type="button" className="num shrink-0 underline-offset-2 hover:underline" onClick={() => toLine(e.line)}>第 {e.line} 行</button> : null}
                        <span className="min-w-0">{e.message}</span>
                        {name && !builtin && <Button variant="outline" size="sm" className="h-6 px-2 text-[0.75rem]" onClick={() => addParam(name)}>把 {name} 加成参数</Button>}
                      </li>
                    )
                  })}
                </ul>
              )}
              {check && check.unsupported.length > 0 && (
                <div className="flex flex-col gap-1.5 rounded-md border border-warning/40 px-3 py-2" data-testid="indicator-unsupported">
                  {check.unsupported.map((u) => (
                    <div key={u.why}><span className="text-warning">{u.why}：</span><span className="num">{u.names.join("、")}</span></div>
                  ))}
                  {check.reduced ? (
                    <div className="flex flex-wrap items-center gap-2 text-muted-foreground">去掉要用到它们的语句，剩下的还能画。
                      {!builtin && <Button variant="outline" size="sm" className="h-6 px-2 text-[0.75rem]" onClick={() => set("formula", check.reduced!)}>只保留能算的部分</Button>}
                    </div>
                  ) : <div className="text-muted-foreground">要画的线都用到了它们，去掉以后没有剩下能画的。</div>}
                </div>
              )}
              {check && check.items.length > 0 && (
                <div className="flex flex-wrap gap-1.5" aria-label="要画的东西">
                  {check.items.map((it, i) => (
                    <span key={i} className="inline-flex h-6 items-center gap-1.5 rounded-md border border-border px-2 text-[0.8125rem]">
                      <span aria-hidden className="inline-block size-2 rounded-full" style={{ background: it.color === "up" ? "var(--up)" : it.color === "down" ? "var(--down)" : it.color ?? "var(--muted-foreground)" }} />
                      {it.name}<span className="text-subtle-foreground">{KIND[it.kind] ?? it.kind}{it.hidden ? " · 只显示数值" : ""}</span>
                    </span>
                  ))}
                </div>
              )}
              {check?.sample?.error && <div className="text-muted-foreground">{codeOf(check.sample.listing_id)}：{check.sample.error}</div>}
              {check?.sample?.rows && check.sample.rows.length > 0 && (
                <div className="overflow-x-auto" data-testid="indicator-sample">
                  <table className="num text-[0.8125rem]">
                    <thead>
                      <tr className="text-subtle-foreground">
                        <th className="pr-4 text-left font-normal">日期</th>
                        {Object.keys(check.sample.rows[0]).filter((k) => k !== "t").map((k) => <th key={k} className="pr-4 text-right font-normal">{k}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {check.sample.rows.map((r) => (
                        <tr key={r.t}>
                          <td className="pr-4">{new Date(r.t as number).toISOString().slice(0, 10)}</td>
                          {Object.entries(r).filter(([k]) => k !== "t").map(([k, v]) => (
                            <td key={k} className="pr-4 text-right">{typeof v === "number" ? Number(v.toPrecision(6)).toString() : "—"}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {(check.sample.zones || check.sample.levels) ? <div className="mt-1 text-[0.8125rem] text-subtle-foreground">
                    另有区间 {check.sample.zones ?? 0} 个、水平线 {check.sample.levels ?? 0} 条（在 K 线里看）</div> : null}
                </div>
              )}
            </section>
          </div>
        </div>
      </div>
    </div>
  )
}
