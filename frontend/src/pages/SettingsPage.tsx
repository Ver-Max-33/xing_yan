// 设置 (design 11): data profile per market, data sources & capabilities, intraday sources (P5), AI (P3), notifications
// (P4), appearance, shortcuts, about.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Activity, ArrowUpCircle, Bot, CircleCheck, CircleDashed, Database, Info, Keyboard, Loader2, Palette, RefreshCw, Send } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { endpoints, type Market, type Profile, type SecretSource, usePutSetting, useSettings } from "@/lib/api"
import { USES, useDataUse } from "@/lib/datause"
import { desktop } from "@/app/platform"
import { MARKET_NAME } from "@/lib/format"
import { cn } from "@/lib/utils"
import { PageHeader, Section } from "./common"
import { jobEndpoints, useNotify } from "@/lib/jobs"
import { checkUpdate, downloadLine, updateLine, useInstallUpdate, useUpdate, useUpdateDownload } from "@/lib/update"
import { AISettings } from "./settings/AISettings"
import { TachibanaLogin } from "./settings/LiveSettings"
import { NotifySettings } from "./settings/NotifySettings"

const PROFILE_TEXT: Record<Market, Record<Profile, string>> = {
  CN: { test: "Tushare 免费档 + 交易所股票列表：只有未复权日线。", prod: "Tushare 2000 积分以上：复权因子、涨跌停价、停牌、估值、行业、指数；财务报告按公告日生效（5000 积分的按期接口）。" },
  JP: { test: "yfinance 约 300 只样本 + JPX 一览：延迟数据，只用于开发和熟悉功能。", prod: "J-Quants 订阅档：全市场日线、估值、指数、财务报告摘要（按披露时刻生效）；盘中行情接立花证券。" },
}
const SOURCE_TEXT: Record<Exclude<SecretSource, null>, string> = { env: "环境变量", ".env": "仓库 .env 文件", keychain: "系统钥匙串" }
const SECRETS: [string, string, string][] = [
  ["tushare_token", "Tushare Pro", "A 股：在 tushare.pro 个人中心获取"],
  ["jquants_api_key", "J-Quants", "日股：在 J-Quants 后台获取 API key（V2）"],
]
const LIVE_SECRETS: [string, string, string][] = [
  ["tickflow_api_key", "TickFlow", "A 股盘中：在 tickflow.org 控制台创建 API key（Pro 档才有分钟线）；填了就用它，不用 Tushare 实时日线"],
  ["tachibana_auth_id", "立花 认证 ID", "e支店 API 的公开键认证 ID（正式和演示环境各一套）"],
  ["tachibana_key_file", "立花 私钥文件", "私钥 PEM 文件在本机的完整路径；只在登录时读取"],
  ["tachibana_url", "立花 接入地址", "不填就是演示环境；正式环境填你账户的 e支店 地址"],
  ["tachibana_key_pass", "立花 私钥口令", "私钥加了口令才需要"],
]
const AI_SECRETS: [string, string, string][] = [
  ["deepseek_api_key", "DeepSeek", "在 platform.deepseek.com 创建 API key"],
  ["openrouter_api_key", "OpenRouter", "在 openrouter.ai 的 Keys 页面创建"],
]

function SecretRow({ name, label, hint, source, onSaved }: {
  name: string; label: string; hint: string; source: SecretSource | undefined; onSaved?: () => void
}) {
  const [value, setValue] = useState("")
  const [editing, setEditing] = useState(false)
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: () => endpoints.putSecret(name, value),
    onSuccess: (s) => {
      qc.setQueryData(["secrets"], s)
      const where = s[name] === ".env" ? "仓库 .env 文件（它在那里，就改那里）" : "系统钥匙串"
      void qc.invalidateQueries({ queryKey: ["notify"] })   // a channel's readiness follows its keys
      void qc.invalidateQueries({ queryKey: ["jobs"] })
      void qc.invalidateQueries({ queryKey: ["live"] })   // the Tachibana session's readiness follows its keys
      setEditing(false)
      setValue("")
      toast.success(`${label} 已存入${where}`)
      onSaved?.()
    },
    onError: (e) => toast.error(String(e)),
  })
  return (
    <div className="flex min-h-12 items-center gap-3 border-b border-border/60 py-2 text-[0.875rem] last:border-b-0">
      <div className="w-32 shrink-0 font-medium">{label}</div>
      <div className="flex min-w-0 flex-1 items-center gap-1.5 text-muted-foreground">   {/* the state whole; the hint gives way */}
        {source ? <CircleCheck className="size-4 shrink-0 text-down" /> : <CircleDashed className="size-4 shrink-0" />}
        <span className="shrink-0 whitespace-nowrap">{source ? `已配置 · 来自${SOURCE_TEXT[source]}` : "未配置"}</span>
        <span className="min-w-0 truncate text-subtle-foreground" title={hint}> · {hint}</span>
      </div>
      {editing ? (
        <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (value.trim()) save.mutate() }}>
          <Input type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="粘贴密钥" className="h-8 w-64" />
          <Button size="sm" type="submit" disabled={!value.trim() || save.isPending}>{source === ".env" ? "写入 .env" : "存入钥匙串"}</Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(false)}>取消</Button>
        </form>
      ) : source === "env" ? <span className="shrink-0 whitespace-nowrap text-[0.8125rem] text-subtle-foreground" title={`改环境变量 WB_SECRET_${name.toUpperCase()}，或去掉它再在这里存`}>在这里改不了</span>
        : <Button size="sm" variant="outline" onClick={() => setEditing(true)}>{source ? "更换" : "配置"}</Button>}
    </div>
  )
}

/** The published source (AGPL §6): each release's tree under its tag there. */
const SOURCE = "https://github.com/Ver-Max-33/xing_yan"

/** 检查更新: the version running, what GitHub has published, and checking again. A newer one: in the installed app
 * 更新到 x.y.z (download, check, close, install, open again), with how far it is; run from the repository, its page. */
function UpdateRow() {
  const qc = useQueryClient()
  const { data: u } = useUpdate()
  const { data: d } = useUpdateDownload()
  const install = useInstallUpdate()
  const again = useMutation({ mutationFn: () => checkUpdate(true), onSuccess: (r) => qc.setQueryData(["update"], r),
    onError: (e) => toast.error(`检查失败：${String(e)}`) })
  const line = updateLine(u)
  const dl = downloadLine(d)
  const canInstall = !!u?.newer && u.installable && desktop.inElectron
  const busy = install.isPending || d?.state === "downloading" || d?.state === "ready"
  return (
    <div className="border-b border-border/60 py-2 text-[0.875rem]" data-testid="update-row">
      <div className="flex min-h-8 items-center gap-3">
        <div className="w-32 shrink-0 font-medium">版本</div>
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 text-muted-foreground">
          <span className="num shrink-0">当前 {u?.current ?? "…"}</span>
          <span className={cn("min-w-0", line.tone === "new" && "text-structure", line.tone === "warn" && "text-warning")}>· {line.text}</span>
          {u?.newer && u.url && !canInstall && <a href={u.url} target="_blank" rel="noreferrer" className="underline">打开发布页下载</a>}
          {u && <span className="num shrink-0 text-subtle-foreground">· {u.checked_at.slice(5, 16).replace("T", " ")} 查的</span>}
        </div>
        {canInstall && (
          <Button size="sm" disabled={busy} onClick={() => install.mutate()} data-testid="update-install">
            {busy ? <Loader2 className="size-4 animate-spin" /> : <ArrowUpCircle className="size-4" />}更新到 {u.latest}</Button>
        )}
        <Button size="sm" variant="outline" disabled={again.isPending || busy} onClick={() => again.mutate()}>
          <RefreshCw className={cn("size-4", again.isPending && "animate-spin")} />检查更新</Button>
      </div>
      {dl && (
        <p className={cn("num mt-1 ml-[8.75rem] text-[0.8125rem]", dl.tone === "warn" ? "text-warning" : dl.tone === "new" ? "text-structure" : "text-muted-foreground")}
          data-testid="update-download">{dl.text}</p>
      )}
      {u?.newer && u.notes && <p className="mt-1 ml-[8.75rem] text-[0.8125rem] whitespace-pre-wrap text-subtle-foreground">{u.notes}</p>}
    </div>
  )
}

/** 数据使用开关 (spec §4.10): four switches a data source; the one each market's prices come from now is marked. */
function DataUseTable() {
  const { data } = useDataUse()
  const put = usePutSetting()
  if (!data) return null
  const now = new Set(Object.values(data.markets))
  return (
    <Table data-testid="datause">
      <TableHeader><TableRow><TableHead>数据源</TableHead>
        {USES.map(([k, label, hint]) => <TableHead key={k} title={hint} className="whitespace-nowrap">{label}</TableHead>)}</TableRow></TableHeader>
      <TableBody>
        {data.sources.map((s) => (
          <TableRow key={s.id}>
            <TableCell className="whitespace-normal">{s.name}{now.has(s.id) && <span className="ml-1.5 text-[0.75rem] whitespace-nowrap text-subtle-foreground">现在的行情来源</span>}</TableCell>
            {USES.map(([k, label]) => (
              <TableCell key={k}>
                <input type="checkbox" aria-label={`${s.name} ${label}`} checked={s[k]}
                  onChange={(e) => put.mutate({ key: `datause.${s.id}.${k}`, value: e.target.checked }, { onError: (err) => toast.error(String(err)) })} />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** How many days a plan's latest date is behind today (spec §4.9 延迟: e.g. a free plan 12 weeks behind). */
const lag = (day: string) => Math.floor((Date.now() - new Date(`${day}T00:00:00`).getTime()) / 86_400_000)

function Capabilities({ market }: { market: Market }) {
  const qc = useQueryClient()
  const { data = [] } = useQuery({ queryKey: ["capabilities", market], queryFn: () => endpoints.capabilities(market) })
  const probe = useMutation({   // the plans asked again now: an upgrade shows without waiting for tomorrow's update
    mutationFn: () => jobEndpoints.probe(market),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["capabilities", market] }); toast.success(`${market} 的数据源探测完了`) },
    onError: (e) => toast.error(`探测出错：${String(e)}`),
  })
  const head = (
    <div className="flex items-center gap-3 text-[0.8125rem]">
      <span className="font-medium">{market}</span>
      {data.length === 0 && <span className="text-subtle-foreground">还没有探测记录（每天第一次更新时自动探测）</span>}
      <Button size="sm" variant="outline" className="h-7" disabled={probe.isPending} onClick={() => probe.mutate()}>
        {probe.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}{data.length ? "重新探测" : "探测"}</Button>
    </div>
  )
  if (data.length === 0) return head
  return (
    <div className="flex flex-col gap-1">{head}
    <Table>
      <TableHeader><TableRow><TableHead>数据集</TableHead><TableHead>来源</TableHead><TableHead>历史起点</TableHead><TableHead>最新</TableHead><TableHead className="text-right">次/分钟</TableHead><TableHead>状态</TableHead><TableHead>最近一次错误</TableHead></TableRow></TableHeader>
      <TableBody>
        {data.map((c) => (
          <TableRow key={`${c.provider}-${c.dataset}`}>
            <TableCell className="num">{c.dataset}</TableCell><TableCell>{c.provider}</TableCell>
            <TableCell className="num">{c.history_start ?? "—"}</TableCell>
            <TableCell className="num">{c.latest_date ?? "—"}{c.latest_date && lag(c.latest_date) > 7 && <span className="text-warning">（晚 {lag(c.latest_date)} 天）</span>}</TableCell>
            <TableCell className="num text-right">{c.rate_per_min ?? "—"}</TableCell>
            <TableCell className={cn("max-w-[22.5rem] truncate", c.available ? "text-muted-foreground" : "text-warning")} title={c.detail}>
              {c.available ? (c.detail || "可用") : (c.detail || "不可用")}
            </TableCell>
            <TableCell className="max-w-[18.75rem] truncate text-[0.8125rem] text-muted-foreground"
              title={c.last_error ? `${c.last_error_at?.replace("T", " ")} 取 ${c.last_error_unit} 时：${c.last_error}` : undefined}>
              {c.last_error ? <><span className="num">{c.last_error_at?.slice(5, 16).replace("T", " ")}</span> {c.last_error}</> : "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
    </div>
  )
}

/** 字号: the root size in px (the body text is 2px smaller), as the choice reads. 标准 = the components' own. */
const SIZES: [number, string][] = [[15, "小"], [16, "标准"], [17, "大"], [18, "特大"]]

/** The kinds of settings, one at a time (the address keeps it: /settings?s=ai): [key, name, what is in it, icon]. */
const KINDS: [string, string, string, typeof Database][] = [
  ["data", "数据", "数据档、密钥和套餐、数据使用", Database], ["live", "盘中行情", "立花证券、实时行情", Activity],
  ["ai", "AI", "模型、思考强度、预算", Bot], ["notify", "通知", "飞书机器人、推送哪些", Send],
  ["appearance", "外观", "主题、字体、字号、涨跌配色", Palette], ["keys", "快捷键", "键盘操作", Keyboard], ["about", "关于", "免责声明、版本", Info],
]

export default function SettingsPage() {
  const [params] = useSearchParams()
  const kind = KINDS.some(([k]) => k === params.get("s")) ? params.get("s")! : "data"
  const { data: settings } = useSettings()
  const put = usePutSetting()
  const { data: secrets } = useQuery({ queryKey: ["secrets"], queryFn: endpoints.secrets })
  const { data: notify } = useNotify()
  const version = useUpdate().data?.current   // the backend's: the one running (wb.__version__)
  if (!settings) return null
  const set = (key: Parameters<typeof put.mutate>[0]["key"], value: string) => put.mutate({ key, value }, { onError: (e) => toast.error(String(e)) })
  return (
    <div className="flex h-full flex-col">
      <PageHeader title={<span>设置<span className="font-normal text-subtle-foreground"> · {KINDS.find(([k]) => k === kind)?.[1]}</span></span>} />
      <div className="flex min-h-0 flex-1">
      <nav aria-label="设置的分类" className="w-[12.5rem] shrink-0 overflow-y-auto border-r border-border p-2">
        {KINDS.map(([k, label, hint, Icon]) => (
          <Link key={k} to={`/settings?s=${k}`} aria-current={kind === k ? "page" : undefined}
            className={cn("flex items-start gap-2.5 rounded-md px-2.5 py-2", kind === k ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground")}>
            <Icon className="mt-0.5 size-4 shrink-0" strokeWidth={1.75} />
            <span className="min-w-0"><span className="block text-[0.875rem]">{label}</span>
              <span className="block truncate text-[0.75rem] text-subtle-foreground">{hint}</span></span>
          </Link>
        ))}
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto px-6" key={kind}>
        <div className="max-w-[80rem]">   {/* a form reads best at a line's length, however wide the window */}
        {kind === "data" && <>
        <Section title="数据档" hint="每个市场单独选择。规则、标注、画线、笔记两档共用；行情数据和结论分开存放。">
          {/* one row per choice across both markets (subgrid): a longer text on one side does not push the other side's
              正式档 out of line */}
          <div className="grid grid-cols-2 grid-rows-[auto_auto_auto] gap-x-6">
            {(["CN", "JP"] as Market[]).map((mk) => (
              <div key={mk} className="row-span-3 grid grid-rows-subgrid gap-y-2">
                <div className="text-[0.875rem] font-medium">{MARKET_NAME[mk]}</div>
                <RadioGroup className="row-span-2 grid-rows-subgrid" value={settings[`profile.${mk}`]} onValueChange={(v) => set(`profile.${mk}`, v)}>
                  {(["test", "prod"] as Profile[]).map((p) => (
                    <label key={p} className={cn("flex cursor-pointer items-start gap-3 rounded-md border p-3", settings[`profile.${mk}`] === p ? "border-ring" : "border-border")}>
                      <RadioGroupItem value={p} className="mt-0.5" />
                      <span className="text-[0.875rem]">
                        <span className="font-medium">{p === "test" ? "测试档" : "正式档"}</span>
                        <span className="mt-0.5 block text-[0.8125rem] text-muted-foreground">{PROFILE_TEXT[mk][p]}</span>
                      </span>
                    </label>
                  ))}
                </RadioGroup>
              </div>
            ))}
          </div>
        </Section>
        <Section title="数据源与能力" hint="密钥只保存在本机（环境变量、仓库 .env 或系统钥匙串），不会显示、不会写进日志。套餐由系统探测，不用手填。">
          {SECRETS.map(([n, l, h]) => <SecretRow key={n} name={n} label={l} hint={h} source={secrets?.[n]} />)}
          <div className="mt-4 flex flex-col gap-4"><Capabilities market="CN" /><Capabilities market="JP" /></div>
        </Section>
        <Section title="数据使用" hint="每个数据源四个开关：本地存储（关掉就不从它取数据）、发送给 LLM（证据包里放不放它算出的事实）、出现在通知中（飞书消息里写不写它的价格）、导出（CSV 里有没有它的行）。是否符合各数据源的条款由你判断，默认值只是保守起点，不是法律意见：J-Quants 默认不导出；发给 LLM 的只是规则判定、指标值这类派生事实，不发成批的原始行情。">
          <DataUseTable />
        </Section>
        </>}
        {kind === "live" && <>
        <Section title="盘中行情" hint="测试档：yfinance 的 1 分钟线，约晚 20 分钟，只用来看链路。正式档：A 股填了 TickFlow 的 key 就用它的 1 分钟线（真实的分钟 K 线和成交额），没填用 Tushare 实时日线 rt_k（要在 Tushare 单独开通“A股日线RT”，分钟线由每次轮询的快照近似拼成），日股用立花证券 e支店——只取行情、不能下单，也从不需要第二密码。">
          {LIVE_SECRETS.map(([n, l, h]) => <SecretRow key={n} name={n} label={l} hint={h} source={secrets?.[n]} />)}
          <TachibanaLogin />
          <div className="mt-5 flex flex-col gap-1" data-testid="market-tape-setting">
            <div className="flex items-center gap-2 text-[0.875rem]">
              <Switch checked={settings["market.tape"]} aria-label="市场页的盘中记录"
                onCheckedChange={(v) => put.mutate({ key: "market.tape", value: v }, { onError: (e) => toast.error(String(e)) })} />市场页的盘中记录（A 股）</div>
            <span className="text-[0.8125rem] text-subtle-foreground">正式档配了 TickFlow 时，开盘期间每分钟取一次全市场行情（约 5,500 只），记下每分钟的涨停、炸板、上证这些数，一天约 140 MB 流量；市场页开着时每 10 秒一次，一小时约 210 MB。关了以后市场页用收盘数据，没有昨天同一时间的对照和盘面变化，不在盯盘名单里的 A 股也没有当天那根 K 线。</span>
          </div>
          <div className="mt-4 flex flex-col gap-1" data-testid="minutes-prefetch-setting">
            <div className="flex items-center gap-2 text-[0.875rem]">
              <Switch checked={settings["minutes.prefetch"]} aria-label="提前取好分钟线"
                onCheckedChange={(v) => put.mutate({ key: "minutes.prefetch", value: v }, { onError: (e) => toast.error(String(e)) })} />提前取好分钟线（A 股）</div>
            <span className="text-[0.8125rem] text-subtle-foreground">正式档配了 TickFlow 时，收盘以后（和开盘前）把全市场最近 60 个交易日的 1 分钟线取好，打开任何一只的分钟 K 线都不用等，MACD、KDJ 这些指标也用够长的历史算（和通达信一致）。第一次补齐约半小时，之后每天收盘后约 2 分钟；占硬盘约 1.5–2 GB。开盘期间不取，不占盯盘的额度。关了以后，打开分钟 K 线时才取那一只。</span>
          </div>
        </Section>
        </>}
        {kind === "ai" && <>
        <Section title="AI" hint="只支持 DeepSeek 和 OpenRouter（OpenAI 兼容接口）。AI 只翻译、归纳和解释，计算都由程序完成；没有 AI 时其余功能照常可用。">
          {AI_SECRETS.map(([n, l, h]) => <SecretRow key={n} name={n} label={l} hint={h} source={secrets?.[n]} />)}
          <div className="mt-4"><AISettings /></div>
        </Section>
        </>}
        {kind === "notify" && <>
        <div data-testid="notify-settings">
          <Section title="通知" hint="只用飞书自定义机器人：带签名校验的 webhook，消息是卡片。只发给你自己的机器人。">
            {Object.values(notify?.channels ?? {}).flatMap((c) => c.fields).map((f) => (
              <SecretRow key={f.name} name={f.name} label={f.label} hint={f.hint} source={secrets?.[f.name]} />
            ))}
            <NotifySettings />
          </Section>
        </div>
        </>}
        {kind === "appearance" && <>
        <Section title="外观与涨跌配色">
          {/* one grid: the names in one column, the choices lined up beside them */}
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] items-center gap-x-4 gap-y-3 text-[0.875rem]">
            <span className="text-muted-foreground">主题</span>
            <RadioGroup className="flex w-auto flex-wrap gap-4" value={settings.theme} onValueChange={(v) => set("theme", v)}>
              {[["dark", "深色"], ["light", "浅色"], ["system", "跟随系统"]].map(([v, l]) => (
                <label key={v} className="flex items-center gap-2"><RadioGroupItem value={v} />{l}</label>
              ))}
            </RadioGroup>
            <span className="text-muted-foreground">字体</span>
            <RadioGroup className="flex w-auto flex-wrap gap-4" value={settings["ui.font"]} onValueChange={(v) => set("ui.font", v)}>
              <label className="flex items-center gap-2"><RadioGroupItem value="system" />系统字体
                <span className="text-[0.8125rem] text-subtle-foreground">（苹方 / 微软雅黑，和系统界面一致）</span></label>
              <label className="flex items-center gap-2" style={{ fontFamily: '"Geist Variable", "PingFang SC", "Microsoft YaHei", sans-serif' }}>
                <RadioGroupItem value="geist" />Geist</label>
            </RadioGroup>
            <span className="text-muted-foreground">字号</span>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <RadioGroup className="flex w-auto flex-wrap gap-4" value={String(settings["ui.size"])}
                onValueChange={(v) => put.mutate({ key: "ui.size", value: Number(v) }, { onError: (e) => toast.error(String(e)) })}>
                {SIZES.map(([px, label]) => (
                  <label key={px} className="flex items-center gap-2"><RadioGroupItem value={String(px)} />{label}
                    <span className="num text-[0.75rem] text-subtle-foreground">{px - 2}px</span></label>
                ))}
              </RadioGroup>
              <span className="text-[0.8125rem] text-subtle-foreground">正文字号；按钮、间距和图表上的字一起缩放</span>
            </div>
            <span className="text-muted-foreground">涨跌配色</span>
            <RadioGroup className="flex w-auto flex-wrap gap-4" value={settings.colors} onValueChange={(v) => set("colors", v)}>
              <label className="flex items-center gap-2"><RadioGroupItem value="red_up" /><span className="text-up">红涨</span><span className="text-down">绿跌</span></label>
              <label className="flex items-center gap-2"><RadioGroupItem value="green_up" /><span className="text-up">绿涨</span><span className="text-down">红跌</span></label>
            </RadioGroup>
          </div>
        </Section>
        </>}
        {kind === "keys" && <>
        <Section title="快捷键">
          <div className="grid max-w-[56rem] grid-cols-[repeat(auto-fit,minmax(22rem,1fr))] gap-x-6 gap-y-2 text-[0.875rem]">
            {[[`${desktop.mod}K`, "搜索或跳转"], ["J / K", "下一只 / 上一只（今日：下一条 / 上一条）"], ["W", "加入或移出观察"], ["↵", "今日：打开 K 线"],
              ["E", "今日：这一条今天不再显示"], ["1 / 2 / 3", "日 / 周 / 月"], ["]", "K 线：隐藏或显示右栏"], ["Esc", "退出画线"]].map(([k, l]) => (
              <div key={k} className="flex min-w-0 items-center gap-3"><Kbd className="shrink-0">{k}</Kbd><span className="min-w-0 text-muted-foreground">{l}</span></div>
            ))}
          </div>
        </Section>
        </>}
        {kind === "about" && <>
        <Section title="更新" hint="看 GitHub 上有没有发布新版本：打开应用时查一次，之后每 6 小时一次，有新版本时底部状态栏会提示。点「更新到 x.y.z」：下载这一版的安装包、和发布页上的 SHA256SUMS.txt 核对，然后关闭形研、安装、自动重新打开（Windows 大约半分钟）；正在跑的扫描会中断。从仓库运行的开发版不自己安装，git pull 后重新打开。">
          <UpdateRow />
        </Section>
        <Section title="关于与免责声明" className="border-b-0">
          <div className="flex flex-col gap-1 text-[0.8125rem] text-muted-foreground">
            <p>本系统只做研究和盯盘：不下单、不连接任何交易接口，也从不索要立花证券的第二密码。</p>
            <p>J-Quants 数据仅供个人使用：不分发、不导出原始数据，飞书通知只发给本人。</p>
            <p>分析结果不是投资建议。<Badge variant="outline" className="ml-1 font-normal">v{version ?? "…"}</Badge></p>
            <p>形研是开源软件，Copyright (C) 2026 Ver-Max-33，按 GNU AGPL 第 3 版或更新版本发布，不提供任何担保。可以按这个协议使用、修改、转给别人；改过的版本给别人用（包括放在网上让人用），也要公开它的源代码。</p>
            <p data-testid="source">源代码：<a href={SOURCE} target="_blank" rel="noreferrer" className="underline">{SOURCE.replace("https://", "")}</a>，这一版是其中的标签 v{version ?? "…"}；协议全文在那里，也随安装包附在 LICENSE 里。</p>
            <p>价格行为分析含 PA_Agent 的代码和提示词（Copyright (C) 2026 PA Agent Contributors，同样是 AGPL），按 A 股改过，改了哪些见源代码里的 backend/pa/NOTICE.md。</p>
          </div>
        </Section>
        </>}
        </div>
      </div>
      </div>
    </div>
  )
}
