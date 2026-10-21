// P4 acceptance, the daily side (spec §7.1, §13): an enabled rule is scanned (here by hand — demo data is never
// fetched into, and what it finds is never sent), the 今日 feed lists what the scan found with the chart and the
// reasons, W puts the stock on the watch list, the 选股 page shows the funnel by stage and the listings that got
// anywhere, and the notification settings refuse a webhook that is not Feishu's and keep the switches you set.
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, "..", "..")
let app, win, dataDir

test.beforeAll(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-daily-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })   // nothing reaches a real bot
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const send = (method, url, body) => win.evaluate(async ([m, u, b]) => {
  const r = await fetch(u, { method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
  return { status: r.status, body: await r.json() }
}, [method, url, body])
const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)
/** The rule's run on the last day before the data's last that `listing` was not a signal (its close not above its 20-day
 *  mean, as the rule below asks): what a scan compares with — a rule's first scan lists no changes, and a new signal is
 *  one that was not a signal then (10-13). */
const dayBefore = async (rule, market, listing) => {
  const rows = await win.evaluate((l) => fetch(`/api/bars?listing_id=${l}&limit=60`).then((r) => r.json()).then((j) => j.rows), listing)
  const c = rows.map((r) => r.close)
  let i = rows.length - 2
  while (i >= 19 && c[i] > c.slice(i - 19, i + 1).reduce((a, b) => a + b, 0) / 20) i--
  expect(i).toBeGreaterThanOrEqual(19)
  expect((await send("POST", "/api/runs", { rule_id: rule, market, day: rows[i].date })).status).toBe(200)
}

const RULE = [
  "id: daily_e2e", "name: 端到端 · 站上均线", "markets: [CN, JP]",
  "stages:", '  - {id: today, kind: event, when: "C > MA(C, 20)"}',
  "signal_at: today.at", "",
].join("\n")

test("an enabled rule's scan is in the feed with its chart and reasons, and W watches the stock", async () => {
  expect((await send("POST", "/api/rules", { text: RULE })).status).toBe(200)
  expect((await send("PUT", "/api/rules/daily_e2e/state", { enabled: true })).status).toBe(200)
  await dayBefore("daily_e2e", "CN", "CN:600519")                              // A 股 has a day to compare with; 日股 not
  await go("#/")
  await win.reload()
  const steps = win.getByTestId("first-steps")                                 // nothing scanned yet: the last step left
  await expect(steps).toContainText("已启用 1 条规则")
  await steps.getByRole("button", { name: "扫描全部市场" }).click()
  const feed = win.getByTestId("today-feed")
  await expect(feed.getByTestId("event").first()).toBeVisible({ timeout: 60_000 })
  // 日股's first scan: what the rule found, not a change per stock (thousands on a whole market), and where to see it
  const first = feed.getByTestId("today-JP").getByTestId("first-scans")
  await expect(first).toContainText("首次扫描 1 条规则", { timeout: 60_000 })
  await expect(first.getByRole("link", { name: /端到端 · 站上均线/ })).toHaveAttribute("href", /screen\?rule=daily_e2e/)
  await expect(feed.getByTestId("today-JP").getByTestId("event")).toHaveCount(0)
  await expect(win.getByRole("tab", { name: /新信号/ })).toContainText(/[1-9]/)
  await feed.getByTestId("event").first().click()
  const detail = win.getByTestId("event-detail")
  await expect(detail).toContainText("端到端 · 站上均线")
  await expect(detail.getByTestId("detail-card")).toBeVisible({ timeout: 30_000 })   // 命中原因, from the rule itself
  await win.keyboard.press("w")
  await expect(win.getByText(/已加入/)).toBeVisible()
  await expect(win.getByTestId("status-scan")).toContainText("扫描")
})

test("J / K keep your place on 今日: the row stays in view, E moves on, back from K 线 returns to it, read rows go quiet", async () => {
  const events = Array.from({ length: 40 }, (_, i) => ({ kind: "watch", market: "CN", listing_id: i % 2 ? "CN:600519" : "CN:000001",
    name: `第 ${i} 只`, rule_id: "daily_e2e", rule_name: "端到端 · 站上均线", version: 1, day: "2026-09-30", run_id: "x",
    stage: `s${i}`, stage_label: "信号" }))
  await win.route("**/api/today", (r) => r.fulfill({ json: { events, first: [] } }))
  await go("#/")
  await win.reload()
  const rows = win.getByTestId("event")
  await expect(rows).toHaveCount(40)
  await win.evaluate(() => document.activeElement?.blur())
  for (let i = 0; i < 30; i++) await win.keyboard.press("j")
  await expect(rows.nth(30)).toHaveAttribute("aria-current", "true")
  await expect(rows.nth(30)).toBeInViewport()                                 // it was left below the fold
  await win.keyboard.press("e")                                               // set aside: on to the next, not the first
  await expect(rows).toHaveCount(39)
  await expect(rows.nth(30)).toHaveAttribute("aria-current", "true")
  await expect(rows.nth(30)).toContainText("第 31 只")
  await win.keyboard.press("Enter")
  await win.waitForFunction(() => location.hash.startsWith("#/k/"))
  await win.goBack()
  await expect(rows.nth(30)).toHaveAttribute("aria-current", "true")          // back where you were
  await expect(rows.nth(30)).toContainText("第 31 只")
  await expect(rows.nth(30)).toBeInViewport()
  await expect(rows.nth(29)).toHaveAttribute("data-read", "true")             // passed over: read
  await expect(rows.nth(35)).not.toHaveAttribute("data-read", "true")
  await win.unroute("**/api/today")
  await win.reload()   // the page keeps a feed for a minute: the next test reads the real one
})

test("今日 keeps its list while a scan runs: each answer about the scan replaces it, it never goes blank", async () => {
  const events = Array.from({ length: 40 }, (_, i) => ({ kind: "watch", market: "CN", listing_id: "CN:600519", name: `第 ${i} 只`,
    rule_id: "daily_e2e", rule_name: "端到端 · 站上均线", version: 1, day: "2026-09-30", run_id: "x", stage: `s${i}` }))
  let tick = 0
  await win.route("**/api/today", async (r) => { await new Promise((res) => setTimeout(res, 1500)); await r.fulfill({ json: { events, first: [] } }) })
  await win.route("**/api/jobs", async (r) => {   // a scan under way: asked for every 2 s, and each answer moves its scan_at on
    const res = await r.fetch()
    const j = await res.json()
    j.markets[0] = { ...j.markets[0], running: true, status: "scanning", scan_at: `2026-10-06T15:00:${String(tick++ % 60).padStart(2, "0")}` }
    await r.fulfill({ response: res, json: j })
  })
  await go("#/")
  await win.reload()
  const rows = win.getByTestId("event")
  await expect(rows).toHaveCount(40, { timeout: 30_000 })
  const seen = []
  for (let i = 0; i < 25; i++) { seen.push(await rows.count()); await win.waitForTimeout(200) }   // 5 s: two or three new answers
  expect(Math.min(...seen)).toBe(40)   // it went to 0 for each reload of the feed, and J / K pressed then were lost
  await win.unrouteAll({ behavior: "wait" })
  await win.reload()
})

const jobsNow = () => win.evaluate(() => fetch("/api/jobs").then((r) => r.json()).then((j) => j.markets))
const localNow = (ago) => win.evaluate((ms) => {   // the backend's clock as it writes it: local time, no zone
  const d = new Date(Date.now() - ms), p = (n) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}, ago)

test("今日 by market: one market's items, a scan of that market alone, and what a running scan is doing", async () => {
  await go("#/")
  const feed = win.getByTestId("today-feed")
  await expect(feed.getByTestId("today-CN")).toBeVisible({ timeout: 60_000 })     // both markets scanned: each its own part
  await expect(feed.getByTestId("today-JP")).toBeVisible({ timeout: 60_000 })
  await expect.poll(async () => (await jobsNow()).every((m) => !m.running), { timeout: 60_000 }).toBe(true)
  const markets = win.getByRole("tablist", { name: "市场" })
  await markets.getByRole("tab", { name: /A 股/ }).click()
  await expect(feed.getByTestId("today-JP")).toHaveCount(0)                         // A 股's items only
  expect((await feed.getByTestId("event").allTextContents()).every((t) => t.includes("CN"))).toBe(true)
  const [cn, jp] = await jobsNow()
  await expect(win.getByText("A 股扫描完成")).toHaveCount(0, { timeout: 15_000 })    // the scan-all's own notes gone first
  await win.getByRole("button", { name: "选择扫描哪个市场" }).click()                 // the market is chosen beside the button
  await win.getByRole("menuitem", { name: "扫描 A 股" }).click()                        // and scanned at once; the button says it from then on
  await expect.poll(async () => { const [c] = await jobsNow(); return !c.running && c.scan_at !== cn.scan_at }, { timeout: 60_000 }).toBe(true)
  await expect(win.getByText("A 股扫描完成")).toBeVisible({ timeout: 10_000 })       // said when it is done, with what it found
  expect((await jobsNow())[1].scan_at).toBe(jp.scan_at)                              // 日股 left alone
  // a scan under way, paused, given up — as the backend reports them (held still here, by one stand-in for /api/jobs)
  let say = null   // what A 股's state is made to be, or the backend's own
  await win.route("**/api/jobs", async (route) => {
    const res = await route.fetch()
    const j = await res.json()
    if (say) j.markets[0] = { ...j.markets[0], ...say }
    await route.fulfill({ response: res, json: j })
  })
  const since = await localNow(65_000)
  say = { running: true, status: "scanning", progress: { step: "scan", since, i: 1, n: 2,
    rule: "端到端 · 站上均线", phase: "整理命中原因", done: 1200, total: 4290, fraction: 0.28 } }
  await win.reload()
  const doing = win.getByTestId("doing-CN")
  await expect(doing).toContainText("扫描第 1 / 2 条规则「端到端 · 站上均线」：整理命中原因 1,200 / 4,290", { timeout: 30_000 })
  await expect(doing).toContainText("28%")
  await expect(doing).toContainText(/已用 1 分 0\d 秒/)
  await expect(doing.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "28")
  await expect(win.getByRole("button", { name: "扫描 A 股" })).toBeDisabled()         // one turn at a time
  await expect(win.getByTestId("status-scan")).toContainText("CN 扫描中 28%")
  const paused = win.waitForRequest((r) => r.url().endsWith("/api/jobs/CN/pause") && r.method() === "POST")
  await doing.getByRole("button", { name: "暂停" }).click()                          // stops at its next step, keeps what it did
  await paused
  await doing.getByRole("button", { name: "取消" }).click()                          // asked first: it would throw work away
  const ask = win.getByRole("dialog")
  await expect(ask).toContainText("取消这次 A 股扫描？")
  await expect(ask).toContainText("今日页还是上一次扫描的结果")
  await expect(ask).toContainText("改点「暂停」")                                     // the way to keep it, named
  await ask.getByRole("button", { name: "继续扫描" }).click()                         // changed one's mind: nothing sent
  await expect(ask).toBeHidden()
  const cancelled = win.waitForRequest((r) => r.url().endsWith("/api/jobs/CN/cancel") && r.method() === "POST")
  await doing.getByRole("button", { name: "取消" }).click()
  await win.getByRole("dialog").getByRole("button", { name: "取消扫描" }).click()      // stops at its next step, keeps nothing
  await cancelled
  // paused (or cut off by quitting the app): how far it got, 继续 from there — or 取消, to give it up
  say = { running: false, status: "paused", progress: null, message: "已暂停：1 / 2 条规则做完了，「继续」从下一条接着扫" }
  await win.reload()
  await expect(win.getByTestId("paused-CN")).toContainText("1 / 2 条规则做完了", { timeout: 30_000 })
  await expect(win.getByRole("button", { name: "继续扫描 A 股" })).toBeVisible()
  await expect(win.getByTestId("status-scan")).toContainText("CN 扫描已暂停")
  const resumed = win.waitForRequest((r) => r.url().endsWith("/api/jobs/CN/resume") && r.method() === "POST")
  await win.getByTestId("paused-CN").getByRole("button", { name: "继续" }).click()
  await resumed
  const givenUp = win.waitForRequest((r) => r.url().endsWith("/api/jobs/CN/cancel") && r.method() === "POST")
  await win.getByTestId("paused-CN").getByRole("button", { name: "取消" }).click()
  await expect(win.getByRole("dialog").getByRole("button", { name: "保持暂停" })).toBeVisible()
  await win.getByRole("dialog").getByRole("button", { name: "取消扫描" }).click()
  await givenUp
  // given up: the status bar says so, 今日 keeps the last scan, and the button scans afresh
  say = { running: false, status: "cancelled", progress: null, message: "已取消：这次扫描没有扫完，今日页还是上一次的结果；要扫就再点扫描" }
  await win.reload()
  await expect(win.getByTestId("status-scan")).toContainText("CN 扫描已取消", { timeout: 30_000 })
  await expect(win.getByRole("button", { name: "扫描 A 股" })).toBeEnabled()
  say = null
  await win.unrouteAll({ behavior: "wait" })   // a request under way finishes first
  await win.reload()
  await win.getByRole("tablist", { name: "市场" }).getByRole("tab", { name: /全部市场/ }).click()
})

test("the 数据 page runs the market's update in the app; on demo data 补历史 and 抽样核对 say why they cannot", async () => {
  await go("#/data")
  const snap = win.getByTestId("data-snapshot")
  await expect(snap).toContainText("演示数据")
  await expect(snap.getByRole("button", { name: "补历史…" })).toBeDisabled()
  await expect(win.getByRole("button", { name: "抽样核对" })).toBeDisabled()
  await expect(snap).not.toContainText("wb ")                                  // no command line to go and run
  await snap.getByRole("button", { name: "立即更新" }).click()
  await expect(win.getByText(/这次没有新的日线可取/)).toBeVisible()            // demo data: it says it only scans again
  await expect(snap.getByRole("button", { name: "立即更新" })).toBeEnabled({ timeout: 60_000 })   // the market's turn is over
  await expect(win.getByTestId("status-scan")).toContainText("扫描")
})

test("the 选股 page shows the latest scan: the funnel by stage and the listings that got anywhere", async () => {
  await go("#/screen?rule=daily_e2e")
  const funnel = win.getByTestId("funnel")
  await expect(funnel).toContainText("全部", { timeout: 30_000 })
  await expect(funnel).toContainText("① 信号")                                     // a stage by its name, never its formula …
  await expect(funnel.locator('button[title*="C > MA(C, 20)"]')).toContainText("信号")   // … which the hover still gives whole
  const rows = win.getByTestId("screen-row")
  await expect(rows.first()).toBeVisible()
  const n = await rows.count()
  await win.getByLabel("筛选名称或代码").fill("zzzz")
  await expect(rows).toHaveCount(0)
  await win.getByLabel("筛选名称或代码").fill("")
  await expect(rows).toHaveCount(n)
  await win.getByRole("tab", { name: "A 股" }).click()
  await expect(rows.first()).toContainText(/600519|000001/)                        // A 股's own (one market: no 「CN」 after the code)
  await win.getByRole("button", { name: /^命中/ }).click()
  await expect(rows.first()).toContainText("命中")
  for (const t of await rows.allTextContents()) expect(t).toContain("命中")       // the group lists only its own
  await expect(win.getByRole("button", { name: /^近 5 日信号/ })).toBeVisible()
  await go("#/screen?rule=no_such_rule")
  await expect(win.getByTestId("screen-error")).toContainText("没有规则 no_such_rule")   // said, not a blank page
})

test("a long name gives way in the 选股 table: cut at 9 rem, whole on hover (10-12)", async () => {
  // a Japanese company's name ran the 名称 column 350 px wide: 摘要 down to two cut lines, 涨跌幅 and 成交额 off the right
  const LONG = "クリエイト・レストランツ・ホールディングス"
  await win.route(/\/api\/screen\//, async (route) => {
    const res = await route.fetch()
    const json = await res.json()
    for (const r of json.rows ?? []) if (r.market === "JP") { r.name = LONG; break }
    await route.fulfill({ response: res, json })
  })
  try {
    await win.reload()   // the scan the test before read is cached: asked again, through the route
    await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
    await go("#/screen?rule=daily_e2e")
    await win.getByRole("tab", { name: "全部市场" }).click()
    const name = win.getByTestId("screen-row").getByRole("link", { name: LONG })
    await expect(name).toBeVisible({ timeout: 30_000 })
    const m = await name.evaluate((el) => ({ cut: el.scrollWidth > el.clientWidth + 1, width: el.getBoundingClientRect().width,
      rem: parseFloat(getComputedStyle(document.documentElement).fontSize) }))
    expect(m.cut).toBe(true)                                     // an ellipsis (the hover tip gives it whole)
    expect(m.width).toBeLessThanOrEqual(9 * m.rem + 1)           // before: whole, 294 px, its column 385 (摘要 squeezed to 135)
  } finally {
    await win.unroute(/\/api\/screen\//)
    await win.getByRole("tab", { name: "A 股" }).click()        // as the test before left it
  }
})

test("the 选股 table holds at every window width, warnings and all (10-17)", async () => {
  // 10-17: the 选股 table at times grew too wide with the window — on real lists the table ran past the
  // window: the warnings' tags came beside the names a moment after the list (a nowrap cell, 192 → 424 px), and from a
  // 1250 px window the 近 60 日 column came on (60 rem) without room for its fixed 80 px; at 18 px text 距离's
  // 「已越过上沿 14.6%」 never wrapped. Rows here as yours: the longest 距离 and 摘要, 再次, three warnings, a standing
  const W = (kind, label) => ({ kind, label, date: "2026-09-30", days: 0, new: true, predicted: false, reason: "…", risk: "…", brief: "" })
  const three = [W("unlock", "解禁前易走弱"), W("limit_up", "连板后易回落"), W("retail", "散户接盘易走弱")]
  const detail = (lid) => ({ listing_id: lid, date: "2026-09-30", status: "watch", deepest_stage: "g", closest_miss: null, unknown_reasons: [],
    bindings: [{ stage: "big", kind: "structure", id: "a", start: "2026-08-26", end: "2026-09-30", at: "2026-09-22", bars: 32 },
      { stage: "pre", kind: "window", id: null, start: "2026-07-29", end: "2026-09-22", at: "2026-09-22", bars: 40 },
      { stage: "g", kind: "structure", id: "a>b", start: "2026-09-23", end: "2026-09-30", at: "2026-09-28", bars: 12 }],
    conditions: [{ stage: "g", id: "xingmo", kind: "during", expr: "$xingmo == 0", op: "==", value: 1, threshold: 1, status: "pass" }],
    distances: [{ id: "break_up", value: 38.73, threshold: 34.31, edge: 33.8 }],
    names: { stages: { big: "大箱", pre: "小箱前两个月", g: "上品" }, conds: { "g.xingmo": "小箱形末量尽（开关 xingmo）" }, units: { "g.xingmo": "" }, ranks: [] } })
  await win.route(/\/api\/screen\//, async (route) => {
    const res = await route.fetch()
    const json = await res.json()
    for (const r of json.rows ?? []) Object.assign(r, { status: "watch", repeat: true, industry: "房地产", industry2: "房地产开发", detail: detail(r.listing_id) })
    await route.fulfill({ response: res, json })
  })
  await win.route("**/api/danger", async (r) => {
    const ids = JSON.parse(r.request().postData() ?? "{}").ids ?? []
    await r.fulfill({ json: { covered: true, why: null, day: "2026-09-30", unknown: {}, uncovered: {}, unlocks_from: null, history_from: null,
      warnings: Object.fromEntries(ids.map((id) => [id, three])) } })
  })
  const row = { chg: 0.0361, rel5: 0.04, prior15: -0.05, rel20: 0, limit_up: 7, small: false }
  await win.route("**/api/market/CN/industries", (r) => r.fulfill({ json: { market: "CN", mode: "close", day: "2026-09-30", time: null,
    of: { l1: 31, l2: 127 }, l1: [{ ...row, name: "房地产", rank: 2, tag: "转强", parent: null }], l2: [{ ...row, name: "房地产开发", rank: 35, tag: "转强", parent: "房地产" }] } }))
  const size = (s) => win.evaluate((v) => fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: "ui.size", value: v }) }), s)
  const spill = () => win.evaluate(() => { const box = document.querySelector("table").parentElement; return box.scrollWidth - box.clientWidth })
  const before = await win.evaluate(() => [innerWidth, innerHeight])
  try {
    for (const text of [16, 18]) {
      await size(text)
      await win.reload()
      await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
      await go("#/screen?rule=daily_e2e")
      await expect(win.getByTestId("screen-row").first().getByTestId("danger-tags")).toContainText("散户接盘易走弱", { timeout: 30_000 })
      await expect(win.getByTestId("screen-industry-cell").first()).toContainText("房地产开发")
      for (const width of [1200, 1250, 1270, 1290, 1440]) {   // the smallest window, just past 60 rem, and a usual one
        await win.setViewportSize({ width, height: 800 })
        await expect.poll(spill, { message: `${width} px at ${text} px text` }).toBeLessThanOrEqual(1)
      }
    }
    const m = await win.evaluate(() => {   // with the tags and without them, in one moment: nothing else can come between
      const cell = document.querySelector('[data-testid="screen-row"] td:nth-child(2)')
      const link = cell.querySelector("a"), tags = [...document.querySelectorAll('[data-testid="screen-row"] [data-testid="danger-tags"]')]
      const out = { with: cell.getBoundingClientRect().width, below: tags[0].getBoundingClientRect().top >= link.getBoundingClientRect().bottom - 1 }
      for (const t of tags) t.style.display = "none"
      out.without = cell.getBoundingClientRect().width
      for (const t of tags) t.style.display = ""
      return out
    })
    expect(m.with).toBeCloseTo(m.without, 0)          // the 名称 column as it is without them: nothing moves when they come
    expect(m.below).toBe(true)                        // under the name, not beside it
  } finally {
    await win.unroute(/\/api\/screen\//)
    await win.unroute("**/api/danger")
    await win.unroute("**/api/market/CN/industries")
    await size(16)
    await win.setViewportSize({ width: before[0], height: before[1] })
    await win.reload()
    await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
  }
})

test("notifications: a webhook that is not Feishu's is refused, and a switch stays as you set it", async () => {
  await go("#/settings?s=notify")
  const box = win.getByTestId("notify-settings")
  await box.getByRole("button", { name: "配置" }).first().click()
  await box.getByPlaceholder("粘贴").fill("https://example.com/hook")
  await box.getByRole("button", { name: "存入钥匙串" }).click()
  await expect(win.getByText(/只支持飞书/)).toBeVisible()
  await expect(box.getByRole("button", { name: "发测试消息" })).toBeDisabled()     // no bot yet
  await expect(box).toContainText("只从实时行情发：A 股不是（不发），日股不是（不发）")   // the test profile's sources are late
  await box.getByLabel("盘中提醒").click()                                     // flips at once; stored in the background
  await expect(box.getByLabel("盘中提醒")).not.toBeChecked()
  await expect.poll(async () => (await send("GET", "/api/settings")).body["notify.live"]).toBe(false)
  await win.reload()
  await expect(win.getByTestId("notify-settings").getByLabel("盘中提醒")).not.toBeChecked()
})

test("data use (§4.10): J-Quants is not exported unless you say so, and a switch stays as you set it", async () => {
  await go("#/settings?s=data")
  const table = win.getByTestId("datause")
  await expect(table.getByLabel("J-Quants 导出")).not.toBeChecked()
  await expect(table.getByLabel("Tushare 导出")).toBeChecked()
  await table.getByLabel("Tushare 出现在通知中").click()
  await expect.poll(async () => (await send("GET", "/api/settings")).body["datause.tushare.notify"]).toBe(false)
  await win.reload()
  await expect(win.getByTestId("datause").getByLabel("Tushare 出现在通知中")).not.toBeChecked()
})

test("the briefing: a market on, its time, a preview of what it would say; a 选股 view put into it, counted, taken out", async () => {
  await go("#/settings?s=notify")
  const brief = win.getByTestId("brief-settings")
  await expect(brief).toContainText("开盘前简报")
  await brief.getByRole("switch", { name: "A 股的开盘前简报" }).click()
  await expect.poll(async () => (await send("GET", "/api/settings")).body["brief.CN"]).toBe("on")
  await brief.getByLabel("A 股简报的时间").fill("08:45")
  await expect.poll(async () => (await send("GET", "/api/settings")).body["brief.time.CN"]).toBe("08:45")
  await brief.getByRole("button", { name: "预览 A 股的简报" }).click()
  const view = win.getByTestId("brief-preview")
  await expect(view).toContainText("A 股 ·")
  await expect(view).toContainText("演示数据：只能预览，不会发到飞书")
  await win.keyboard.press("Escape")
  await expect(brief.getByTestId("brief-items")).toContainText("还没有")              // nothing until a view is put in
  await go(`#/screen?rule=daily_e2e&where=${encodeURIComponent("MA(BAR_LEN, 20) <= 0.03")}`)
  const filters = win.getByTestId("screen-filters")
  await filters.getByRole("button", { name: "放进开盘前简报" }).click()
  await expect(filters.getByRole("link", { name: "已在开盘前简报里" })).toBeVisible()
  await expect.poll(async () => (await send("GET", "/api/brief-items")).body.map((i) => [i.rule_id, i.wheres, i.mode]))
    .toEqual([["daily_e2e", ["MA(BAR_LEN, 20) <= 0.03"], "list"]])
  await go("#/settings?s=notify")
  const items = win.getByTestId("brief-items")
  const named = "端到端 · 站上均线 · 近期 K 线短小"
  await expect(items).toContainText(named)
  await items.getByLabel(`${named} 在简报里`).selectOption("count")
  await expect.poll(async () => (await send("GET", "/api/brief-items")).body[0].mode).toBe("count")
  await items.getByRole("button", { name: `从简报里去掉 ${named}` }).click()
  await expect(items).toContainText("还没有")
})

test("a settings number is saved when you leave it, only if it is in range; out of range is said and nothing is saved", async () => {
  await go("#/settings?s=notify")
  const per = win.getByLabel("每项最多列几只")
  const before = await win.evaluate(() => fetch("/api/settings").then((r) => r.json()).then((s) => s["brief.per_rule"]))
  await per.click(); await per.press("ControlOrMeta+a"); await per.press("Delete")
  await per.pressSequentially("12")
  await expect(per).toHaveValue("12")
  await expect(win.getByText("要在 1 到 10 之间")).toBeVisible()
  await per.press("Tab")
  await expect.poll(() => win.evaluate(() => fetch("/api/settings").then((r) => r.json()).then((s) => s["brief.per_rule"]))).toBe(before)  // not the 1 typed on the way
  await per.click(); await per.press("ControlOrMeta+a"); await per.pressSequentially("7")
  await per.press("Tab")
  await expect.poll(() => win.evaluate(() => fetch("/api/settings").then((r) => r.json()).then((s) => s["brief.per_rule"]))).toBe(7)
})
