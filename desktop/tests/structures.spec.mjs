// P2 acceptance (spec §13): the chart draws the backend's structures; in replay a box appears only once
// it is established and nothing after the replay day is on screen; a sample box drawn on the chart is
// stored in exchange prices; clicking a signal bar explains it. Real Electron shell, deterministic demo data.
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, "..", "..")
const ID = "CN:600519"
let app, win, dataDir

test.beforeAll(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-p2-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const get = (url) => win.evaluate(async (u) => (await fetch(u)).json(), url)
const painted = () => win.evaluate(() => {
  const p = window.wbDebug.painted()
  return { struct: p.struct[0], rule: p.rule[0] }
})

// the chart for ID on daily bars, its structures painted, with the rule overlay and replay off
async function openChart() {
  await win.evaluate((h) => { location.hash = h }, `#/k/${ID}`)
  await win.waitForFunction((id) => window.wbDebug?.ready() === id && window.wbDebug.indicator("WB_MA")?.length > 0, ID)
  for (const name of ["规则叠加", "回放"]) {
    const b = win.getByRole("button", { name, exact: true })
    if (await b.getAttribute("aria-pressed") !== "true") continue
    const [n, day] = await win.evaluate(() => [window.wbDebug.inits(), window.wbDebug.replay()])
    await b.click()
    // leaving a replay day reloads the bars: wait for that load, or a drawing could start on a half-loaded chart
    if (name === "回放" && day !== null) await win.waitForFunction(([k, id]) => window.wbDebug.inits() > k && window.wbDebug.ready() === id, [n, ID])
  }
  await win.waitForFunction(() => window.wbDebug.replay() === null && (window.wbDebug.painted().struct[0]?.structures.boxes.length ?? 0) > 0)
}

// 以前的结构 unfolded in the side panel (folded at first: 当前形态 and its 价位梯 come first)
async function showHistory() {
  const fold = win.getByRole("button", { name: /^以前的结构/ })
  if (await fold.getAttribute("aria-expanded") !== "true") await fold.click()
  await expect(fold).toHaveAttribute("aria-expanded", "true")
}

// replay on, then ← until the chart shows `day` (one key per trading day, as a person would step)
async function replayTo(dates, day) {
  const replay = win.getByRole("button", { name: "回放", exact: true })
  if (await replay.getAttribute("aria-pressed") !== "true") {
    await replay.click()
    await expect(win.getByTestId("replay-bar")).toContainText(dates[dates.length - 1]) // the timeline has loaded
  }
  await win.evaluate(() => document.activeElement?.blur())
  const from = await win.evaluate(() => window.wbDebug.replay())
  const steps = (from === null ? dates.length - 1 : dates.indexOf(from)) - dates.indexOf(day)
  for (let i = 0; i < Math.abs(steps); i++) await win.keyboard.press(steps > 0 ? "ArrowLeft" : "ArrowRight")
  // stepping onto the last day is the latest again (the replay's day is then none)
  const want = day === dates[dates.length - 1] ? null : day
  await win.waitForFunction(([w, d]) => window.wbDebug.replay() === w && window.wbDebug.bars().at(-1)?.date === d, [want, day])
}

// a click on the chart at a fraction of its size. KLineChart tracks the pointer (move in first) and takes a
// click within 500 ms of the one before as a failed double-click.
async function clickChart(fx, fy) {
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const [x, y] = [box.x + box.width * fx, box.y + box.height * fy]
  await win.waitForTimeout(600)
  await win.mouse.move(x - 20, y - 10)
  await win.mouse.move(x, y, { steps: 5 })
  await win.mouse.down()
  await win.mouse.up()
}

test("the structure layer holds exactly the backend's structures", async () => {
  await openChart()
  const api = await get(`/api/structures?listing_id=${ID}&basis=qfq`)
  expect(api.boxes.length).toBeGreaterThan(0)
  // as of now: a box is redrawn as it grows and a 金品 is its big box's last small box, so a version a later one replaced
  // is not drawn
  expect((await painted()).struct.structures).toEqual({ ...api, boxes: api.boxes.filter((b) => !b.superseded_at),
    gold: api.gold.filter((g) => !g.superseded_at) })
})

test("当前形态 is drawn first with its prices at the right edge; a structure picked in the list is drawn in full, the rest faint", async () => {
  await openChart()
  const shown = () => win.evaluate(() => window.wbDebug.structures())
  const now = (await shown()).strong.slice().sort()
  expect(now.length).toBeGreaterThan(0)
  await expect.poll(async () => (await shown()).levels.length).toBeGreaterThan(0)   // 当前形态's prices (once the bars are in) …
  expect(await win.evaluate(() => window.wbDebug.room())).toBeGreaterThan(80)     // … in room kept right of the last bar
  // the side panel reads 当前形态 as a plan: its lines by price, 现价 among them, each line the structure it is
  const ladder = win.getByTestId("ladder")
  await expect(ladder).toContainText("现价")
  // nothing in it is cut short: a note that does not fit goes to the next line whole (your 10-10 screenshot)
  expect(await ladder.evaluate((el) => [...el.querySelectorAll("*")].filter((e) => e.scrollWidth > e.clientWidth + 1).length)).toBe(0)
  const edge = ladder.getByTestId("structure-row").filter({ hasText: /上沿|下沿/ }).first()   // the box's edge: its box
  expect(now).toContain(await edge.getAttribute("data-id"))
  // the pointer on a line of it: that line drawn on the chart as itself, its price among 当前形态's at the right edge (they
  // stay); its words beside the row, not over the next price (your 10-10 screenshot)
  const before = (await shown()).levels.length
  await edge.hover()
  await expect.poll(async () => (await shown()).mark?.text ?? null).not.toBeNull()
  const during = await shown()
  expect(during.levels.map((t) => t.text)).toContain(during.mark.text)
  expect(during.levels.length).toBeGreaterThanOrEqual(before)
  const tip = win.getByTestId("hover-tip")
  await expect(tip).toBeVisible()
  const [box, row] = [await tip.boundingBox(), await edge.boundingBox()]
  expect(box.x + box.width).toBeLessThanOrEqual(row.x)
  await win.mouse.move(5, 5)
  await expect.poll(async () => (await shown()).mark ?? null).toBeNull()
  await showHistory()
  const older = win.getByTestId("structure-row").filter({ hasText: "→" }).first()   // a box that came before: its dates
  const id = await older.getAttribute("data-id")
  await older.hover()
  await expect.poll(async () => (await shown()).strong).toContain(id)            // shown while the pointer rests on it
  await older.click()
  await win.mouse.move(5, 5)
  await expect.poll(async () => (await shown()).strong).toContain(id)            // picked: it stays
  // its prices against now would mislead: only the left pressure, now's whatever is picked, stays at the edge
  expect((await shown()).levels.every((t) => t.tone === "pressure" || t.tone === "short")).toBe(true)
  await expect(older).toHaveAttribute("aria-pressed", "true")
  await win.keyboard.press("Escape")
  await expect.poll(async () => (await shown()).strong.slice().sort()).toEqual(now)
})

test("a structure picked off the screen is brought into view and stays there until you go back to 当前形态", async () => {
  await openChart()
  const visible = () => win.evaluate(() => window.wbDebug.visible())
  const [first] = await visible()
  const { boxes } = (await win.evaluate(() => window.wbDebug.structures())).structures
  await showHistory()
  const listed = await win.getByTestId("structure-row").evaluateAll((rows) => rows.map((r) => r.dataset.id))
  const box = boxes.find((b) => listed.includes(b.id) && b.end !== null && b.end < first)   // over before the first day on screen
  expect(box).toBeTruthy()
  const onScreen = async () => { const [a, z] = await visible(); return a <= box.end && box.start <= z }
  await win.locator(`[data-testid=structure-row][data-id="${box.id}"]`).first().click()
  await expect.poll(onScreen).toBe(true)
  // a redraw (another row under the pointer) and a zoom leave the chart where it was put
  await win.getByTestId("structure-row").first().hover()
  const chart = await win.locator("[data-testid=kline-chart]").boundingBox()
  await win.mouse.move(chart.x + chart.width / 2, chart.y + chart.height / 3)
  const before = await visible()
  await win.mouse.wheel(0, -120)
  await expect.poll(visible).not.toEqual(before)   // zoomed in, about the pointer
  expect(await onScreen()).toBe(true)
  await win.keyboard.press("Escape")   // back to 当前形态: the latest bars, with their prices' room
  const last = await win.evaluate(() => window.wbDebug.bars().at(-1).date)
  await expect.poll(async () => (await visible())[1]).toBe(last)
  expect(await win.evaluate(() => window.wbDebug.room())).toBeGreaterThan(80)
})

test("the structure layers are in the 指标 menu, which stays open while you toggle them, and the chart follows", async () => {
  await openChart()
  await win.getByRole("button", { name: "指标", exact: true }).click()
  const gold = win.getByRole("group", { name: "结构图层" }).getByRole("menuitemcheckbox", { name: "金品" })
  await expect(gold).toHaveAttribute("aria-checked", "true")
  await gold.click()
  await expect(gold).toHaveAttribute("aria-checked", "false")
  await expect.poll(async () => (await painted()).struct.layers.gold).toBe(false)
  await gold.click()
  await expect.poll(async () => (await painted()).struct.layers.gold).toBe(true)
  await win.keyboard.press("Escape")
  await expect(win.getByRole("menu")).toHaveCount(0)
})

test("turning the rule overlay on with no rule chosen picks your enabled rule for this market", async () => {
  await win.evaluate(() => fetch("/api/rules/vol_burst/state", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true }) }))
  await win.evaluate(() => localStorage.removeItem("wb.kline.ruleId"))
  await win.reload()
  await openChart()
  await win.getByRole("button", { name: "规则叠加", exact: true }).click()
  await expect(win.getByRole("button", { name: "选择规则" })).toContainText("爆大量")   // not an empty picker and a chart unchanged
  await win.getByRole("button", { name: "规则叠加", exact: true }).click()
  await win.evaluate(() => fetch("/api/rules/vol_burst/state", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: false }) }))
})

test("Space on a focused K 线 button presses that button; it no longer toggles the replay", async () => {
  await openChart()
  // the chart takes its place in the Tab order, named (KLineChart made it tabindex 1: first of all, unseen)
  await expect(win.locator("[data-chart-root]")).toHaveAttribute("tabindex", "0")
  await expect(win.locator("[data-chart-root]")).toHaveAttribute("aria-label", /K 线图/)
  const overlay = win.getByRole("button", { name: "规则叠加", exact: true })   // off, after openChart
  await overlay.focus()
  await win.keyboard.press(" ")
  await expect(overlay).toHaveAttribute("aria-pressed", "true")                                  // pressed it …
  await expect(win.getByRole("button", { name: "回放", exact: true })).toHaveAttribute("aria-pressed", "false")   // … not the replay
  await overlay.click()   // as it was
})

test("in replay a box appears only once it is established, and nothing after the day shows", async () => {
  await openChart()
  const [api, dates] = await Promise.all([get(`/api/structures?listing_id=${ID}&basis=qfq`), get(`/api/dates?listing_id=${ID}`)])
  // the latest box established after the first bar, and the 雏形 it replaced, if any
  const box = api.boxes.filter((b) => b.status === "established" && dates.indexOf(b.known_at) > 0).at(-1)
  const proto = api.boxes.find((b) => b.status === "proto" && b.superseded_at === box.known_at && b.start === box.start)
  const before = dates[dates.indexOf(box.known_at) - 1]

  await replayTo(dates, before)
  let s = (await painted()).struct.structures
  expect(s.boxes.map((b) => b.id)).not.toContain(box.id)
  if (proto) expect(s.boxes.map((b) => b.id)).toContain(proto.id)
  for (const b of s.boxes) {
    expect(b.known_at <= before).toBe(true)
    expect(b.ended_at === null || b.ended_at <= before).toBe(true)
    for (const t of [...b.touches, ...b.events]) expect(t.known_at <= before).toBe(true)
    for (const g of b.segments) expect(g.to <= before).toBe(true)
  }
  for (const p of [...s.pivots, ...s.swings, ...s.gold, ...s.false_bottoms]) expect(p.known_at <= before).toBe(true)

  await replayTo(dates, box.known_at)
  s = (await painted()).struct.structures
  expect(s.boxes.map((b) => b.id)).toContain(box.id)
  if (proto) expect(s.boxes.map((b) => b.id)).not.toContain(proto.id)
})

test("a sample box drawn on the chart is stored in exchange prices", async () => {
  await openChart()
  const drawn = await win.evaluate(() => window.wbDebug.overlays().length)
  await win.getByRole("button", { name: "矩形 / 样例框" }).click()
  await clickChart(0.35, 0.2)
  await clickChart(0.55, 0.4)
  await expect(win.getByRole("dialog", { name: "设为样例框" })).toBeVisible()
  const rect = await win.evaluate(() => window.wbDebug.overlays().at(-1).points)
  const values = rect.map((p) => p.value)
  await win.getByRole("button", { name: "存为样例" }).click()
  await expect(win.getByRole("dialog")).toBeHidden()

  const [shown, raw] = await Promise.all([get(`/api/labels?listing_id=${ID}&basis=qfq`), get(`/api/labels?listing_id=${ID}&basis=raw`)])
  const label = shown.at(-1)
  expect(label.top).toBeCloseTo(Math.max(...values), 6)
  expect(label.bottom).toBeCloseTo(Math.min(...values), 6)
  // stored as exchange prices: the edges scale like that day's close does between the two bases
  const [q, r] = await Promise.all([get(`/api/bars?listing_id=${ID}&period=day&basis=qfq&limit=2000`), get(`/api/bars?listing_id=${ID}&period=day&basis=raw&limit=2000`)])
  const at = (rows) => rows.find((b) => b.date === label.start).close
  const stored = raw.find((l) => l.id === label.id)
  expect(stored.top / label.top).toBeCloseTo(at(r.rows) / at(q.rows), 9)
  // the rectangle gave way to the sample box, which the labels layer draws
  await expect.poll(async () => win.evaluate(() => window.wbDebug.overlays().length)).toBe(drawn)
  await expect.poll(async () => (await painted()).struct.labels.map((l) => l.id)).toContain(label.id)
  await win.evaluate(async (id) => fetch(`/api/labels/${id}`, { method: "DELETE" }), label.id)
})

// the chart with a simple rule laid over it (saved here; the same text again is not a new version)
async function chartWithRule() {
  const text = [
    "id: e2e_above_ma", "name: 端到端 · 站上均线", "markets: [CN, JP]",
    "params:", "  - {id: n, value: 20, range: [5, 60], step: 1, tune: true}",
    "stages:", '  - {id: today, kind: event, when: "C > MA(C, $n)", at: [{id: vol, expr: "V / MA(V, 5) >= 1.2"}]}',
    "signal_at: today.at", "",
  ].join("\n")
  const saved = await win.evaluate(async (t) => (await fetch("/api/rules", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) })).status, text)
  expect(saved).toBe(200)
  await win.reload() // the app read the rule list before this rule existed
  await openChart()
  await win.getByRole("button", { name: "规则叠加", exact: true }).click()
  await win.getByRole("button", { name: "选择规则" }).click()
  await win.getByRole("menuitem", { name: /端到端 · 站上均线/ }).click()
  await expect(win.getByRole("menu")).toBeHidden() // a click while it fades out would land on another rule
  await expect(win.getByTestId("found")).toContainText(/端到端 · 站上均线：\d+ 个信号/)
  await win.waitForFunction(() => (window.wbDebug.painted().rule[0]?.signals.length ?? 0) > 0)
}

test("clicking a signal bar explains it, and replay before the signal hides the explanation", async () => {
  await chartWithRule()
  const day = (await painted()).rule.signals.map((x) => x.signal_date).sort().at(-1)
  const at = await win.evaluate((d) => window.wbDebug.pixelOf(d), day)
  expect(at).not.toBeNull()
  await win.mouse.move(at.x - 10, at.y)
  await win.mouse.click(at.x, at.y)
  await expect(win.getByTestId("detail-card")).toContainText(day)
  await expect.poll(async () => (await painted()).rule.bands.length).toBeGreaterThan(0)

  const dates = await get(`/api/dates?listing_id=${ID}`)
  const before = dates[dates.indexOf(day) - 1]
  await replayTo(dates, before)
  await expect(win.getByTestId("detail-card")).toBeHidden()
  const rule = (await painted()).rule
  expect(rule.bands).toEqual([])
  expect(rule.signals.every((x) => x.signal_date <= before)).toBe(true)
  await win.getByRole("button", { name: "回放", exact: true }).click()
  await win.getByRole("button", { name: "规则叠加", exact: true }).click()
})

test("a drawing finished outside the price pane is dropped, not saved as a price", async () => {
  await openChart()
  const saved = async () => (await get(`/api/drawings?listing_id=${ID}`)).length
  const before = await saved()
  await win.getByRole("button", { name: "水平线" }).click()
  await clickChart(0.5, 0.9) // the MACD pane
  await expect(win.getByText("标注只能画在主图上")).toBeVisible()
  expect(await win.evaluate(() => window.wbDebug.overlays().length)).toBe(0)
  expect(await saved()).toBe(before)
})

test("comparing with another series, or on another period, replaces the series before it", async () => {
  await openChart()
  const compare = async (pick) => {
    await win.getByRole("button", { name: /^对比/ }).click()
    await pick()
    await expect(win.getByRole("menu")).toBeHidden()
  }
  await compare(() => win.getByRole("menuitem", { name: /^大盘/ }).click())
  await win.waitForFunction(() => window.wbDebug.painted().compare[0]?.name.length > 0)
  await compare(async () => {
    await win.getByLabel("对比的股票").fill("000001")
    await win.getByRole("menuitem", { name: /平安银行/ }).click()
  })
  await win.waitForFunction(() => window.wbDebug.painted().compare[0]?.name === "平安银行")
  const painted = (n) => win.waitForFunction((k) => Object.keys(window.wbDebug.painted().compare[0]?.closes ?? {}).length === k, n)
  for (const period of ["day", "week"]) { // weekly closes are fewer: daily ones left behind would show
    if (period === "week") {
      await win.evaluate(() => document.activeElement?.blur())
      await win.keyboard.press("2")
    }
    const stock = (await get(`/api/bars?listing_id=CN:000001&period=${period}&basis=qfq&limit=5000`)).rows
    await painted(stock.length)
    const closes = await win.evaluate(() => window.wbDebug.painted().compare[0].closes)
    for (const b of stock) expect(closes[b.t]).toBe(b.close)
  }
  await win.keyboard.press("1")
  await win.waitForFunction((id) => window.wbDebug.view()?.period === "day" && window.wbDebug.ready() === id, ID)
  await compare(() => win.getByRole("menuitem", { name: "取消对比" }).click())
})

test("on a weekly chart a rectangle stays a rectangle", async () => {
  await openChart()
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("2")
  await win.waitForFunction((id) => window.wbDebug.view()?.period === "week" && window.wbDebug.ready() === id, ID)
  await win.getByRole("button", { name: "矩形 / 样例框" }).click()
  await clickChart(0.35, 0.2)
  await clickChart(0.55, 0.4)
  await expect.poll(async () => (await get(`/api/drawings?listing_id=${ID}`)).filter((d) => d.kind === "wbRect").length).toBe(1)
  await expect(win.getByRole("dialog", { name: "设为样例框" })).toBeHidden()
  const rect = (await get(`/api/drawings?listing_id=${ID}`)).find((d) => d.kind === "wbRect")
  await win.evaluate(async (id) => fetch(`/api/drawings/${id}`, { method: "DELETE" }), rect.id)
  await win.keyboard.press("1")
  await win.waitForFunction((id) => window.wbDebug.view()?.period === "day" && window.wbDebug.ready() === id, ID)
})

test("dragging the chart with the rule on pans it and opens no verdict", async () => {
  await chartWithRule()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const [x, y] = [box.x + box.width * 0.6, box.y + box.height * 0.3]
  await win.waitForTimeout(600)
  await win.mouse.move(x, y)
  await win.mouse.down()
  await win.mouse.move(x - 200, y, { steps: 10 })
  await win.mouse.up()
  await win.waitForTimeout(800)
  await expect(win.getByTestId("detail-card")).toBeHidden()
  expect((await painted()).rule.bands).toEqual([])
  await win.getByRole("button", { name: "规则叠加", exact: true }).click()
})

test("stepping the replay keeps the zoom", async () => {
  await openChart()
  const space = () => win.evaluate(() => { // pixels per bar, over the last ten bars
    const b = window.wbDebug.bars()
    return (window.wbDebug.pixelOf(b.at(-1).date).x - window.wbDebug.pixelOf(b.at(-11).date).x) / 10
  })
  const before = await space()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  await win.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.3)
  for (let i = 0; i < 6; i++) await win.mouse.wheel(0, -100) // each step zooms in 10%
  await expect.poll(space).toBeGreaterThan(before * 1.3)
  const zoomed = await space()
  const dates = await get(`/api/dates?listing_id=${ID}`)
  await replayTo(dates, dates.at(-3))
  expect(await space()).toBeCloseTo(zoomed, 0)
  await win.getByRole("button", { name: "回放", exact: true }).click()
})

test("in replay the panel counts only the sample boxes drawn by that day", async () => {
  await openChart()
  const dates = await get(`/api/dates?listing_id=${ID}`)
  const bars = (await get(`/api/bars?listing_id=${ID}&period=day&basis=qfq&limit=100`)).rows.filter((b) => b.date >= dates.at(-60) && b.date <= dates.at(-30))
  const label = { listing_id: ID, cutoff: dates.at(-20), is_box: true, category: "flat", start: dates.at(-60), end: dates.at(-30),
    top: Math.max(...bars.map((b) => b.high)), bottom: Math.min(...bars.map((b) => b.low)), scale: "mid", touches: [], exclusions: [], basis: "qfq" }
  const { id } = await win.evaluate(async (l) => (await fetch("/api/labels", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(l) })).json(), label)
  await win.reload()
  await openChart()
  await expect(win.getByTestId("found")).toContainText("你的样例")
  await replayTo(dates, dates.at(-25))
  await expect(win.getByTestId("found")).not.toContainText("你的样例")
  await win.getByRole("button", { name: "回放", exact: true }).click()
  await win.evaluate(async (x) => fetch(`/api/labels/${x}`, { method: "DELETE" }), id)
})
