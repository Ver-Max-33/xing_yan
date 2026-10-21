// 盯盘 (spec §8.5, §13 P5): the stocks you follow with each market's session and the index strip; outside a session
// (and always on demo data, which is never sent to a real source) the rows are the latest close; a stock's 分时图
// draws the minutes kept for its last day with the average price, the previous close and its box top, and the small
// boxes in those minutes (分时小箱) with each break's volume.
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

// Toyota's morning on the last demo day, as a poll would have kept it: half an hour up, a box 766–772 for an hour and
// a half, then up through its top on twice the volume of the bars near it (on 5-minute bars)
const MINUTES = `
import math, sys
from datetime import datetime, timedelta
import polars as pl
from wb.config import get_paths
from wb.realtime.providers.yfinance_rt import MINUTES
d = get_paths("test", sys.argv[1]).market_dir("JP") / "intraday"
d.mkdir(parents=True, exist_ok=True)
px = lambda k: 760.0 + k * 0.2 if k < 30 else 769.0 + 3.0 * math.sin(k / 4.0) if k < 120 else 779.0 + 0.4 * (k - 120)
pl.DataFrame([("JP:7203", datetime(2026, 9, 30, 9, 0) + timedelta(minutes=k), px(k), px(k) + 0.2, px(k) - 0.2, px(k),
               6000.0 if k == 120 else 1000.0) for k in range(150)], schema=MINUTES, orient="row").write_parquet(d / "date=2026-09-30.parquet")
# the day before, kept too, lower (the days side by side)
pl.DataFrame([("JP:7203", datetime(2026, 9, 29, 9, 0) + timedelta(minutes=k), 740.0 + (k % 7), 741.0 + (k % 7), 739.0 + (k % 7),
               740.0 + (k % 7), 800.0) for k in range(60)], schema=MINUTES, orient="row").write_parquet(d / "date=2026-09-29.parquet")
`

test.beforeAll(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-live-"))
  const uv = (...args) => execFileSync("uv", ["run", "--project", path.join(repo, "backend"), ...args], { stdio: "inherit" })
  uv("wb", "--data-dir", dataDir, "devdata")
  uv("python", "-c", MINUTES, dataDir)
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("the stocks you follow, each market's session, and a stock's minutes", async () => {
  const groups = await win.evaluate(() => fetch("/api/watchlist").then((r) => r.json()))
  expect((await win.evaluate((g) => fetch(`/api/watchlist/${g}/items`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listing_id: "JP:7203" }),
  }).then((r) => r.status), groups[0].id))).toBe(200)
  await go("#/live")
  await expect(win.getByTestId("live-session-JP")).toContainText("东京")
  await expect(win.getByTestId("live-session-CN")).toContainText("沪深")
  await expect(win.getByTestId("live-indexes")).toContainText("日经225")
  const row = win.getByTestId("live-row").filter({ hasText: "トヨタ自動車" })
  await expect(row).toContainText("观察中")   // a stock added to the watchlist is 观察中 (its watch status) till you change it
  await win.getByRole("tab", { name: /自选/ }).click()
  await expect(win.getByTestId("live-row")).toHaveCount(1)
  await row.click()
  const detail = win.getByTestId("live-detail")
  await expect(detail).toContainText("トヨタ自動車")
  await expect(detail.getByRole("link", { name: "打开 K 线" })).toHaveAttribute("href", "#/k/JP:7203")
  await expect(detail.getByRole("img", { name: /分时图/ })).toBeVisible()
  await expect(detail).toContainText("09-30")
  await expect(detail).toContainText(/均价 7\d\d/)
  await expect(detail).toContainText("昨收")
})

test("the K-line page's 分时 shows the stock's minutes in place, and one not followed says why it has none", async () => {
  await go("#/k/JP:7203")                                                     // followed since the first test
  await win.getByRole("radio", { name: "分时" }).click()
  const pane = win.getByTestId("kline-intraday")
  await expect(pane.getByRole("img", { name: /分时图/ })).toBeVisible()
  expect(await win.evaluate(() => location.hash)).toBe("#/k/JP:7203")       // still the K-line page
  await go("#/k/CN:000001")                                                   // 分时 stays chosen from stock to stock
  await expect(win.getByTestId("kline-intraday-empty")).toContainText("开盘期间打开过的 A 股都有")   // any A share (design 2026-10-12 §9)
  await go("#/k/JP:6758")
  await expect(win.getByTestId("kline-intraday-empty")).toContainText("盘中只记下盯盘的股票")   // 日股: yours only (10-12)
  await go("#/k/JP:7203")
  await pane.getByRole("link", { name: /波段量和提醒在盯盘页/ }).click()   // 盯盘 has the rest
  await expect(win.getByTestId("live-detail")).toContainText("トヨタ自動車")
  await go("#/k/JP:7203")
  await win.getByRole("radio", { name: "日", exact: true }).click()           // the K-line back, for the tests after
  await expect(win.getByTestId("kline-intraday")).toHaveCount(0)
  await go("#/live?lid=JP%3A7203")                                            // where the next test looks
  await expect(win.getByTestId("live-detail")).toContainText("トヨタ自動車")
})

test("the small boxes in a stock's minutes, each break with its volume, and what resonates", async () => {
  const detail = win.getByTestId("live-detail")
  const periods = detail.getByRole("radiogroup", { name: "分时小箱" })
  await expect(periods.getByRole("radio", { name: "5 分", exact: true })).toHaveAttribute("aria-checked", "true")   // the small box's period
  await expect(detail.getByTestId("micro-box").first()).toBeVisible()
  // the scripted break at 11:00; the box's swings make small boxes of their own that break on even volume (量 1.0×)
  const breakout = detail.getByTestId("micro-break").filter({ hasText: "11:00" })
  await expect(breakout).toContainText("量 2.0×")
  await expect(breakout).toContainText("箱沿附近最大量的 2.0 倍")   // what the number means, on the mark
  await expect(detail.getByTestId("resonance")).toContainText("微观 · 5 分钟小箱上破 · 量 2.0 倍")
  await expect(detail).toContainText("只能实时看，不能回测")
  // each wave's volume against the wave before; the nearest box edges left of the price; the days side by side
  await expect(detail.getByTestId("wave").first()).toBeAttached()
  await expect(detail.getByTestId("intraday-notes")).toContainText(/上方阻力|下方支撑/)
  await detail.getByRole("radiogroup", { name: "几天" }).getByRole("radio", { name: "5 日" }).click()
  await expect(detail.getByRole("img", { name: /分时图 09-29—09-30/ })).toBeVisible()
  await detail.getByRole("radiogroup", { name: "几天" }).getByRole("radio", { name: "当天" }).click()
  await periods.getByRole("radio", { name: "关" }).click()
  await expect(detail.getByTestId("micro-box")).toHaveCount(0)
  await expect(detail.getByTestId("resonance")).toHaveCount(0)
  await periods.getByRole("radio", { name: "5 分", exact: true }).click()
  await expect(detail.getByTestId("micro-box").first()).toBeVisible()
})

test("the intraday sources in Settings: Tachibana logs in only when you click, once configured", async () => {
  await go("#/settings?s=live")
  const row = win.getByTestId("tachibana-login")
  await expect(row).toContainText("先配置上面的认证 ID 和私钥文件")
  await expect(row.getByRole("button", { name: "登录立花证券" })).toBeDisabled()
  await expect(win.getByText("立花 私钥文件")).toBeVisible()
})

test("the K-line can end with today's bar still forming — a normal candle — and the switch takes it away", async () => {
  // demo data is never polled and the clock cannot be set: the forming bar the backend would add while the day is open
  // is stood in for here, on the answer to /api/bars?live=true — what is checked is that the page asks for it and shows it
  await win.route(/\/api\/bars\?.*live=true/, async (route) => {
    const r = await route.fetch()
    const body = await r.json()
    const last = body.rows.at(-1), t = last.t + 3 * 86_400_000
    body.rows.push({ ...last, date: new Date(t).toISOString().slice(0, 10), t, open: last.close, close: last.close * 1.02,
      high: last.close * 1.03, low: last.close * 0.995, temp: true })
    await route.fulfill({ response: r, json: body })
  })
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.bars().length > 0)
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp)).toBe(true)
  await win.getByRole("button", { name: "指标", exact: true }).click()   // the switch is in the 指标 menu, with the layers
  const ghost = win.getByRole("menuitemcheckbox", { name: "盘中当天那根" })   // drawn as any candle (10-12: no dashed box)
  await expect(ghost).toHaveAttribute("aria-checked", "true")
  await expect(ghost).toHaveAttribute("title", /开盘期间打开过的 A 股都有/)
  await ghost.click()
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp ?? false)).toBe(false)
  await ghost.click()
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp)).toBe(true)
  await win.keyboard.press("Escape")
  await expect(win.getByRole("menu")).toHaveCount(0)
  // a sample box drawn now stands on the last official bar, never on the one still forming
  await win.getByRole("button", { name: "矩形 / 样例框" }).click()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  for (const [fx, fy] of [[0.55, 0.3], [0.75, 0.5]]) {   // as structures.spec's clickChart
    await win.waitForTimeout(600)
    await win.mouse.move(box.x + box.width * fx - 20, box.y + box.height * fy - 10)
    await win.mouse.move(box.x + box.width * fx, box.y + box.height * fy, { steps: 5 })
    await win.mouse.down()
    await win.mouse.up()
  }
  const dialog = win.getByRole("dialog", { name: "设为样例框" })
  await expect(dialog).toContainText("截止日 2026-09-30")
  await win.keyboard.press("Escape")
  await go("#/k/CN:000001")                                     // an A share in no watch group asks for it and shows it too
  await win.waitForFunction(() => window.wbDebug?.ready() === "CN:000001" && window.wbDebug.bars().length > 0)
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp)).toBe(true)
  await win.unroute(/\/api\/bars\?.*live=true/)
})

test("J / K and back from K 线 keep your place on 盯盘", async () => {
  const groups = await win.evaluate(() => fetch("/api/watchlist").then((r) => r.json()))
  await win.evaluate((g) => fetch(`/api/watchlist/${g}/items`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ listing_id: "CN:600519" }) }), groups[0].id)
  await go("#/live")
  await win.reload()
  const rows = win.getByTestId("live-row")
  await expect.poll(() => rows.count(), { timeout: 30_000 }).toBeGreaterThan(1)
  await win.evaluate(() => document.activeElement?.blur())
  const at = async () => win.evaluate(() => [...document.querySelectorAll("[data-testid=live-row]")].findIndex((r) => r.getAttribute("aria-current") === "true"))
  const from = await at()
  await win.keyboard.press("j")
  await expect.poll(at).toBe((from + 1) % (await rows.count()))
  const lid = await rows.nth(await at()).getAttribute("data-lid")
  await win.keyboard.press("Enter")
  await win.waitForFunction((l) => location.hash === `#/k/${l}`, lid)
  await win.goBack()
  await expect(win.locator(`[data-testid=live-row][data-lid="${lid}"]`)).toHaveAttribute("aria-current", "true")   // back on your row
})

test("观察's prices follow each market fetch, as the K-line's forming bar does", async () => {
  // the 观察 list once stood at the day before's close all session while the chart moved
  const groups = await win.evaluate(() => fetch("/api/watchlist").then((r) => r.json()))
  await win.evaluate((g) => fetch(`/api/watchlist/${g}/items`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ listing_id: "CN:600519" }) }), groups[0].id)
  let price = 1691.08
  await win.route("**/api/watchlist", async (route) => {
    const res = await route.fetch()
    const json = await res.json()
    for (const g of json) for (const i of g.items) if (i.listing_id === "CN:600519") i.close = price
    await route.fulfill({ response: res, json })
  })
  await win.route("**/api/recent", async (route) => {   // 最近 reads the same quotes
    if (route.request().method() !== "GET") return route.continue()
    const res = await route.fetch()
    const json = (await res.json()).map((i) => (i.listing_id === "CN:600519" ? { ...i, close: price } : i))
    await route.fulfill({ response: res, json })
  })
  await win.route("**/api/live/stream", (r) => r.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" },
    body: `retry: 500\nevent: board\ndata: ${JSON.stringify({ market: "CN", at: "2026-10-01T10:00:30" })}\n\n` }))
  await win.reload()   // the feed connects to the routed stream: a market fetch at each reconnect
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
  await go("#/k/CN:600519")
  const row = win.getByTestId("kline-list").getByRole("button", { name: /贵州茅台/ })
  await expect(row).toContainText("1,691.08", { timeout: 30_000 })
  price = 1510
  await expect(row).toContainText("1,510.00", { timeout: 10_000 })   // asked again at the next fetch, not left as it was
  await win.getByRole("tab", { name: "最近" }).click()
  await expect(row).toContainText("1,510.00")
  price = 1520
  await expect(row).toContainText("1,520.00", { timeout: 10_000 })
  await win.unrouteAll({ behavior: "ignoreErrors" })
})

// ---- each live view follows the live feed (the 10-12 audit: a view that stops following shows a stale price as now) ----
const stream = (event, data) => (r) => r.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" },
  body: `retry: 500\nevent: ${event}\ndata: ${JSON.stringify(typeof data === "function" ? data() : data)}\n\n` })   // a reconnect each 0.5 s: an event each time
const fresh = async () => { await win.reload(); await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 }) }

test("the K-line's forming bar takes each market fetch's price, without a reload", async () => {
  let price = null
  await win.route(/\/api\/bars\?.*live=true/, async (route) => {   // the forming bar the backend adds in the session, at `price`
    const r = await route.fetch()
    const body = await r.json()
    const last = body.rows.at(-1), t = last.t + 3 * 86_400_000
    price ??= Math.round(last.close * 102) / 100
    body.rows.push({ ...last, date: new Date(t).toISOString().slice(0, 10), t, open: last.close, close: price,
      high: Math.max(price, last.close) * 1.01, low: Math.min(price, last.close) * 0.99, temp: true })
    await route.fulfill({ response: r, json: body })
  })
  await win.route("**/api/live/stream", stream("board", () => ({ market: "CN", at: new Date().toISOString() })))
  await fresh()
  await go("#/k/CN:000001")
  await win.waitForFunction(() => window.wbDebug?.ready() === "CN:000001" && window.wbDebug.bars().length > 0)
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp)).toBe(true)
  const first = await win.evaluate(() => window.wbDebug.bars().at(-1).close)
  price = Math.round(first * 103) / 100
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).close), { timeout: 10_000 }).toBe(price)
  await win.unrouteAll({ behavior: "ignoreErrors" })
})

test("分钟 K 线: the kept minutes as 5-minute bars stamped with their end, the forming one moving with each poll; the market only", async () => {
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await fresh()
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.bars().length > 0)
  await win.getByTestId("kline-minute").click()
  await win.getByRole("menuitemradio", { name: "5分钟", exact: true }).click()
  await win.waitForFunction(() => window.wbDebug.view()?.period === "m5" && window.wbDebug.ready() === "JP:7203")
  const dates = await win.evaluate(() => window.wbDebug.bars().map((b) => b.date))
  // 9-29's hour and 9-30's morning (MINUTES above): 12 + 30 bars, each stamped with its end, none across the two days
  expect([dates.length, dates[0], dates[11], dates[12], dates.at(-1)])
    .toEqual([42, "2026-09-29T09:05", "2026-09-29T10:00", "2026-09-30T09:05", "2026-09-30T11:30"])
  await expect(win.getByTestId("kline-minute")).toHaveText(/5分/)
  // a minute chart shows the market only: the drawing tools, 对比 and 回放 are the daily chart's, and so are structures
  for (const name of ["趋势线", "提醒线（盘中价格穿过时提醒；右键任何线条可开关提醒）", "清除这只股票的全部标注", "对比", "回放"]) {
    await expect(win.getByRole("button", { name, exact: true })).toBeDisabled()
  }
  await expect(win.getByTestId("found-period")).toContainText("5 分钟线自己的结构")   // its own (2026-10-12), rules a day's
  // the bar forming, as the backend adds it in the session (demo data is never polled), at `price`: each 盯盘 poll moves it
  let price = null
  await win.route(/\/api\/bars\?.*period=m5.*live=true/, async (route) => {
    const r = await route.fetch()
    const body = await r.json()
    const last = body.rows.at(-1), t = last.t + 5 * 60_000
    price ??= Math.round(last.close * 101) / 100
    body.rows.push({ ...last, date: new Date(t).toISOString().slice(0, 16), t, open: last.close, close: price,
      high: Math.max(price, last.close) + 1, low: Math.min(price, last.close) - 1, temp: true })
    await route.fulfill({ response: r, json: body })
  })
  const jp = (await win.evaluate(() => fetch("/api/live").then((r) => r.json()))).markets.find((m) => m.market === "JP")
  await win.route("**/api/live/stream", stream("quotes", () => ({ ...jp, session: { ...jp.session, state: "trading" }, source: "yfinance_rt",
    error: null, at: new Date().toISOString() })))
  await fresh()
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.view()?.period === "m5")
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).temp)).toBe(true)
  expect(await win.evaluate(() => window.wbDebug.bars().at(-1).date)).toBe("2026-09-30T11:35")
  const first = await win.evaluate(() => window.wbDebug.bars().at(-1).close)
  price = Math.round(first * 102) / 100
  await expect.poll(() => win.evaluate(() => window.wbDebug.bars().at(-1).close), { timeout: 10_000 }).toBe(price)
  expect(await win.evaluate(() => window.wbDebug.bars().length)).toBe(43)   // in place: not a bar more
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await go("#/k/JP:6758")   // no minutes kept: said why, not an empty canvas
  await expect(win.getByTestId("kline-minute-empty")).toContainText("日股只有盯盘的股票")
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.bars().length > 0)
  await expect(win.getByTestId("kline-minute-empty")).toHaveCount(0)
  // 1 → 日 (the times go), 5 → the minute period picked last
  await win.keyboard.press("1")
  await win.waitForFunction(() => window.wbDebug.view()?.period === "day" && /^\d{4}-\d{2}-\d{2}$/.test(window.wbDebug.bars().at(-1)?.date ?? ""))
  await win.keyboard.press("5")
  await win.waitForFunction(() => window.wbDebug.view()?.period === "m5")
  await win.keyboard.press("1")   // the daily chart for the tests after
  await win.waitForFunction(() => window.wbDebug.view()?.period === "day")
})

test("每个周期自己的结构: a week's on its weekly bars, 5 分钟's with the daily lines over them, 分时 with its 分时小箱", async () => {
  // design 2026-10-12-structures-every-period: the day's algorithm on each period's own bars
  await go("#/k/CN:600519")
  await win.waitForFunction(() => window.wbDebug?.ready() === "CN:600519" && window.wbDebug.bars().length > 0)
  await win.keyboard.press("2")   // 周
  await win.waitForFunction(() => window.wbDebug.view()?.period === "week" && window.wbDebug.ready() === "CN:600519")
  await expect.poll(() => win.evaluate(() => window.wbDebug.structures().structures.period)).toBe("week")
  const week = await win.evaluate(() => ({ s: window.wbDebug.structures().structures, bars: window.wbDebug.bars().map((b) => b.date) }))
  expect(week.s.boxes.length).toBeGreaterThan(0)
  expect(week.s.boxes.every((b) => week.bars.includes(b.start) || b.start < week.bars[0])).toBe(true)   // on the weekly bars
  await expect(win.getByTestId("found-period")).toContainText("周线自己的结构")
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.bars().length > 0)
  await win.getByTestId("kline-minute").click()
  await win.getByRole("menuitemradio", { name: "5分钟", exact: true }).click()
  await win.waitForFunction(() => window.wbDebug.view()?.period === "m5" && window.wbDebug.ready() === "JP:7203")
  await expect.poll(() => win.evaluate(() => window.wbDebug.structures().structures.period)).toBe("m5")
  const m5 = await win.evaluate(() => ({ s: window.wbDebug.structures(), bars: window.wbDebug.bars().map((b) => b.date) }))
  expect(m5.s.structures.boxes.length).toBeGreaterThan(0)                       // the morning's box 766–772 (MINUTES above)
  expect(m5.s.structures.boxes.every((b) => m5.bars.includes(b.start))).toBe(true)
  expect(m5.s.structures.tol).toBeCloseTo(2 / 770, 3)                           // two ticks of a 770-yen stock
  await expect(win.getByTestId("found-period")).toContainText("5 分钟线自己的结构")
  await expect.poll(() => win.evaluate(() => window.wbDebug.structures().dayLines?.length ?? 0)).toBeGreaterThan(0)
  expect((await win.evaluate(() => window.wbDebug.structures().dayLines)).every((l) => l.text.startsWith("日线"))).toBe(true)
  await win.getByRole("button", { name: "指标", exact: true }).click()
  await win.getByRole("menuitemcheckbox", { name: "日线价位" }).click()        // the layer off: not drawn
  await win.keyboard.press("Escape")
  await expect.poll(() => win.evaluate(() => window.wbDebug.structures().layers.dayLines)).toBe(false)
  await win.getByRole("button", { name: "指标", exact: true }).click()
  await win.getByRole("menuitemcheckbox", { name: "日线价位" }).click()
  await win.keyboard.press("Escape")
  await win.getByRole("radio", { name: "分时" }).click()   // 分时: the 分时小箱 as 盯盘 draws them
  const pane = win.getByTestId("kline-intraday")
  await expect(pane.getByRole("radio", { name: "5 分", exact: true })).toHaveAttribute("aria-checked", "true")
  await expect(pane.getByTestId("micro-box").first()).toBeVisible()
  await win.getByRole("radio", { name: "日", exact: true }).click()
  await win.waitForFunction(() => window.wbDebug.view()?.period === "day")
})

test("a menu over the toolbar gets its clicks: none of its items sits in the window's drag area (1分钟 could not be picked, 10-12)", async () => {
  // the toolbars drag the window; in the desktop app the system takes a click there before the page sees it — a test's
  // click never goes through the system, so what is checked is where each item is: its centre in a drag box, in no no-drag box
  const dragged = () => win.evaluate(() => {
    const region = (el) => getComputedStyle(el).getPropertyValue("-webkit-app-region")
    const all = [...document.querySelectorAll("*")]
    const boxes = (v) => all.filter((el) => region(el) === v).map((el) => el.getBoundingClientRect())
    const [drag, free] = [boxes("drag"), boxes("no-drag")]
    const inside = (x, y, b) => x >= b.left && x <= b.right && y >= b.top && y <= b.bottom
    return [...document.querySelectorAll("[role=menu] [role^=menuitem], [role=menu] input")].filter((el) => {
      const b = el.getBoundingClientRect(), x = b.left + b.width / 2, y = b.top + b.height / 2
      return drag.some((d) => inside(x, y, d)) && !free.some((d) => inside(x, y, d))
    }).map((el) => el.textContent || el.getAttribute("aria-label"))
  })
  const size = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getContentSize())
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203")
  for (const [w, h, font] of [[1200, 760, 18], [1280, 800, 16]]) {   // the toolbar on two or three lines
    await app.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), [w, h])
    await win.evaluate((f) => { document.documentElement.style.fontSize = `${f}px` }, font)
    for (const menu of [win.getByTestId("kline-minute"), win.getByRole("button", { name: "指标", exact: true }),
      win.getByRole("button", { name: "前复权" }), win.getByRole("button", { name: "对比", exact: true })]) {
      await menu.click()
      await expect(win.getByRole("menu")).toBeVisible()
      expect(await dragged(), `${w}×${h}@${font}`).toEqual([])
      await win.keyboard.press("Escape")
      await expect(win.getByRole("menu")).toHaveCount(0)
    }
  }
  await win.evaluate(() => document.documentElement.style.removeProperty("font-size"))
  await app.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), size)
})

test("the K-line's 分时 is asked again at each 盯盘 poll", async () => {
  const cn = (await win.evaluate(() => fetch("/api/live").then((r) => r.json()))).markets.find((m) => m.market === "CN")
  let asked = 0
  await win.route(/\/api\/live\/CN:000001\/minutes/, (route) => { asked += 1; return route.continue() })
  await win.route("**/api/live/stream", stream("quotes", () => ({ ...cn, at: new Date().toISOString() })))
  await fresh()
  await go("#/k/CN:000001")
  await win.getByRole("radio", { name: "分时" }).click()
  await expect.poll(() => asked).toBeGreaterThan(0)
  const before = asked
  await expect.poll(() => asked, { timeout: 10_000 }).toBeGreaterThan(before + 1)
  await win.unrouteAll({ behavior: "ignoreErrors" })
})

test("the 盯盘 page shows each pushed poll's prices at once", async () => {
  const cn = (await win.evaluate(() => fetch("/api/live").then((r) => r.json()))).markets.find((m) => m.market === "CN")
  const row = cn.rows.find((r) => r.listing_id === "CN:600519")
  expect(row).toBeTruthy()
  await win.route("**/api/live/stream", stream("quotes", { ...cn, rows: cn.rows.map((r) => (r === row ? { ...r, last: 1234.56 } : r)) }))
  await fresh()
  await go("#/live")
  await expect(win.locator('[data-testid=live-row][data-lid="CN:600519"]')).toContainText("1,234.56", { timeout: 10_000 })
  await win.unrouteAll({ behavior: "ignoreErrors" })
})

// ---- a live price not updated for 3 minutes is said (the 10-12 rule) ----------------------------------------------------
const NOW_1013 = new Date("2026-10-13T10:42:30+08:00")
const TRADING = { state: "trading", label: "上午盘", day: "2026-10-13", elapsed: 72, total: 240 }
/** 盯盘's state routed as A 股 trading at 10:42:30 with its poll at `at` and the whole-market quote at `quote`, the other
 *  markets closed (on a real 10-13 日股 trades and its own feed would be measured against the held clock); the feed
 *  silent (the backend's own state would replace it); the clock held there. */
async function tradingAt(at, quote) {
  const real = await win.evaluate(() => fetch("/api/live").then((r) => r.json()))
  const cn = { ...real.markets.find((m) => m.market === "CN"), session: TRADING, error: null, at, quote_at: quote }
  const others = real.markets.filter((m) => m.market !== "CN").map((m) => ({ ...m, session: { ...m.session, state: "closed", label: "休市" } }))
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await win.route("**/api/live", (r) => r.fulfill({ json: { ...real, markets: [cn, ...others] } }))
  await win.route("**/api/live/stream", (r) => r.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: "retry: 600000\n\n" }))
  await win.clock.setFixedTime(NOW_1013)
  await fresh()
}

test("the status bar says when a market's prices stop updating, either feed", async () => {
  await tradingAt("2026-10-13T10:38:10+08:00", "2026-10-13T10:42:20")             // 盯盘's poll 4 min 20 s old
  await expect(win.getByTestId("status-live")).toContainText("CN 行情 4 分钟没更新")
  await tradingAt("2026-10-13T10:42:00+08:00", "2026-10-13T10:37:00")             // the whole-market quote 5 min 30 s old
  await expect(win.getByTestId("status-live")).toContainText("CN 行情 5 分钟没更新")
  await tradingAt("2026-10-13T10:42:00+08:00", "2026-10-13T10:42:20")             // both fresh: the session and the poll's time
  await expect(win.getByTestId("status-live")).toContainText("CN 上午盘 10:42")
  await expect(win.getByTestId("status-live")).not.toContainText("没更新")
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await win.clock.setFixedTime(new Date())
})

test("the 盯盘 page says when its prices stop updating", async () => {
  await tradingAt("2026-10-13T10:38:10+08:00", "2026-10-13T10:42:20")
  await go("#/live")
  await expect(win.getByTestId("live-session-CN")).toContainText("行情 4 分钟没更新")
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await win.clock.setFixedTime(new Date())
})

test("观察 on the K-line page says how old its prices are, and when they stop", async () => {
  await tradingAt("2026-10-13T10:42:00+08:00", "2026-10-13T10:42:20")
  let at = "2026-10-13T10:42:20"
  await win.route("**/api/watchlist", async (route) => {
    const res = await route.fetch()
    const json = await res.json()
    for (const g of json) for (const i of g.items) if (i.listing_id === "CN:600519") Object.assign(i, { date: "2026-10-13", at, close: 1510 })
    await route.fulfill({ response: res, json })
  })
  await go("#/k/CN:600519")
  await win.getByRole("tab", { name: /观察/ }).click()   // the panel keeps the tab last chosen (an earlier test chose 最近)
  const age = win.getByTestId("kline-list-age")
  await expect(age).toHaveText("价格 10:42:20")
  at = "2026-10-13T10:37:40"                                                     // 4 min 50 s old
  await win.reload()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
  await go("#/k/CN:600519")
  await expect(age).toHaveText("行情 4 分钟没更新")
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await win.clock.setFixedTime(new Date())
})

test("the 盯盘 page follows your stocks, and its 潜伏 tab lists the others by the whole-market quote", async () => {
  // 10-12: 盯盘 follows your own stocks; a 潜伏 stock you do not watch is listed by the whole-market quote, never judged
  await tradingAt("2026-10-13T10:42:00+08:00", "2026-10-13T10:42:20")
  await win.route("**/api/live/CN/latent", (r) => r.fulfill({ json: { market: "CN", session: TRADING, rows: [{ listing_id: "CN:000002",
    name: "万科Ａ", rule: "上品", rule_id: "tpl_shang_pin", stage: "中箱", top: 10.0, line: 10.15, last: 9.95, chg: 0.012, to_top: -0.005,
    at: "2026-10-13T10:42:20" }] } }))
  await go("#/live")
  await expect(win.getByRole("tab", { name: /全部/ })).toHaveCount(0)
  await expect(win.getByRole("tab", { name: /自选/ })).toHaveAttribute("aria-selected", "true")   // the first and the default
  await win.getByRole("tab", { name: /潜伏/ }).click()
  await expect(win.locator('[data-testid=live-row][data-lid="CN:000002"]')).toContainText("万科Ａ")
  await expect(win.getByTestId("live-latent-age")).toHaveText("价格 10:42:20")
  await win.unrouteAll({ behavior: "ignoreErrors" })
  await win.clock.setFixedTime(new Date())
})
