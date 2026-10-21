// P1 acceptance (spec §13): the chart draws exactly the backend's numbers; periods, search,
// watchlist and drawings work end to end in the real Electron shell on deterministic demo data.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-p1-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

// wait until this listing's bars and saved drawings are on screen, not just requested
async function openChart(hash) {
  await win.evaluate((h) => { location.hash = h }, hash)
  await waitReady(hash.split("/k/")[1])
}

async function waitReady(listingId) {
  await win.waitForFunction((id) => window.wbDebug?.ready() === id && window.wbDebug.indicator("WB_MA")?.length > 0, listingId)
}

test("chart values are exactly the backend's", async () => {
  await openChart("#/k/JP:7203")
  const result = await win.evaluate(async () => {
    const api = await (await fetch("/api/bars?listing_id=JP:7203&period=day&basis=qfq&limit=600")).json()
    const drawn = { ma: window.wbDebug.indicator("WB_MA"), macd: window.wbDebug.indicator("WB_MACD"), bars: window.wbDebug.bars() }
    const n = drawn.bars.length
    const mism = []
    for (let i = 0; i < n; i++) {
      const a = api.rows[api.rows.length - n + i]
      for (const k of ["ma5", "ma10", "ma20", "ma60"]) if (drawn.ma[i][k] !== a[k]) mism.push([i, k])
      for (const k of ["dif", "dea", "macd"]) if (drawn.macd[i][k] !== a[k]) mism.push([i, k])
      if (drawn.bars[i].close !== a.close) mism.push([i, "close"])
    }
    return { n, mism: mism.slice(0, 5) }
  })
  expect(result.n).toBeGreaterThan(100)
  expect(result.mism).toEqual([])
})

test("the 1:5 split does not show up as a crash in the default basis", async () => {
  await openChart("#/k/JP:7203")
  const maxJump = await win.evaluate(() => {
    const b = window.wbDebug.bars()
    let m = 0
    for (let i = 1; i < b.length; i++) m = Math.max(m, Math.abs(b[i].close / b[i - 1].close - 1))
    return m
  })
  expect(maxJump).toBeLessThan(0.15)
})

test("keyboard: 2 switches to weekly bars", async () => {
  await openChart("#/k/CN:600519")
  await win.locator("[data-testid=kline-chart]").click()
  await win.keyboard.press("2")
  // wait for the weekly data itself, not just the requested period
  await win.waitForFunction(() => window.wbDebug.view()?.period === "week" && window.wbDebug.bars().length > 0
    && window.wbDebug.bars().length < 100)
  const weekly = await win.evaluate(() => window.wbDebug.bars().length)
  expect(weekly).toBeLessThan(80)
  await win.keyboard.press("1")
  await win.waitForFunction(() => window.wbDebug.view()?.period === "day")
})

test("keyboard: ] hides the K 线 right panel and brings it back, the chart takes its room, it stays as you left it (10-17)", async () => {
  // 10-17: a shortcut to hide the K 线 side panel — on a 1200–1440 px window the panel's 300 px are a fifth of the width
  await openChart("#/k/CN:600519")
  const chart = win.locator("[data-testid=kline-chart]"), side = win.getByTestId("kline-side")
  await expect(side).toBeVisible()
  const width = async () => (await chart.boundingBox()).width
  const shown = await width()
  await chart.click()
  await win.keyboard.press("]")
  await expect(side).toBeHidden()
  await expect.poll(width).toBeGreaterThan(shown + 250)                  // its 300 px go to the chart
  // Windows: the toolbar now reaches the window's own buttons and leaves them their room (checked everywhere, 10-12)
  await win.evaluate(() => document.documentElement.classList.add("win-overlay"))
  try {
    const pad = await win.evaluate(() => parseFloat(getComputedStyle(document.querySelector('[data-testid="kline-toolbar"]')).paddingRight))
    expect(pad).toBeGreaterThanOrEqual(138)
  } finally {
    await win.evaluate(() => document.documentElement.classList.remove("win-overlay"))
  }
  await win.reload()
  await waitReady("CN:600519")
  await expect(side).toBeHidden()                                        // as you left it
  await win.getByRole("button", { name: "显示右栏" }).click()             // the toolbar's button, for the mouse
  await expect(side).toBeVisible()
  await expect.poll(width).toBeLessThan(shown + 5)
})

test("⌘K starts from the stocks you looked at last, and finds pages by name too", async () => {
  await openChart("#/k/JP:7203")
  await win.keyboard.press("Meta+k")
  const recent = win.getByRole("group", { name: "最近看过" })
  await expect(recent.getByRole("option").first()).toContainText("7203")
  await win.keyboard.type("k")
  await expect(win.getByRole("option", { name: /K 线/ })).toBeVisible()            // a page found whatever the case
  await win.keyboard.press("Backspace")
  await win.keyboard.type("设置")
  await expect(win.getByRole("option", { name: /设置/ })).toBeVisible()            // a page, not "没有匹配的证券"
  await win.keyboard.press("Enter")
  await expect(win).toHaveURL(/#\/settings$/)
})

test("a dialog opened by a key gives the focus back where it was when it closes", async () => {
  await win.evaluate(() => { location.hash = "#/" })
  const scan = win.getByRole("button", { name: /^扫描/ }).first()
  await expect(scan).toBeVisible()
  await scan.focus()
  await win.keyboard.press("Meta+k")
  await expect(win.getByRole("dialog")).toBeVisible()
  await win.keyboard.press("Escape")
  await expect(win.getByRole("dialog")).toBeHidden()
  await expect(scan).toBeFocused()   // it fell to <body>: the next Tab started from the top
})

test("the status bar always shows the Feishu state and each market's profile, at the smallest window and the largest text", async () => {
  const put = (k, v) => win.evaluate(([k, v]) => fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: k, value: v }) }), [k, v])
  await win.setViewportSize({ width: 1200, height: 760 })
  await put("ui.size", 18)
  try {
    await win.reload()
    await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
    for (const text of [/^飞书/, "CN 测试档", "JP 测试档"]) {
      const el = win.locator("footer").getByText(text)
      await expect(el).toBeVisible()
      const box = await el.boundingBox()
      expect(box.x + box.width).toBeLessThanOrEqual(1200)                     // whole, not cut at the edge
    }
  } finally {
    await put("ui.size", 16)
    await win.setViewportSize({ width: 1440, height: 900 })
    await win.reload()
  }
})

test("settings come one kind at a time from the list on the left, each with its own address", async () => {
  await win.evaluate(() => { location.hash = "#/settings" })
  const nav = win.getByRole("navigation", { name: "设置的分类" })
  await expect(nav.getByRole("link", { name: /数据/ }).first()).toHaveAttribute("aria-current", "page")   // 数据 first
  await expect(win.getByTestId("datause")).toBeVisible()
  await expect(win.getByTestId("ai-settings")).toHaveCount(0)                      // not everything on one page
  await nav.getByRole("link", { name: /^AI/ }).click()
  await expect(win.getByTestId("ai-settings")).toBeVisible()
  await expect(win.getByTestId("datause")).toHaveCount(0)
  await expect(win).toHaveURL(/#\/settings\?s=ai$/)
  await win.reload()
  await expect(win.getByTestId("ai-settings")).toBeVisible({ timeout: 30_000 })   // the address keeps the kind
})

test("the font and its size are yours to set, and the controls grow with the text", async () => {
  const look = () => win.evaluate(() => ({ size: getComputedStyle(document.documentElement).fontSize,
    geist: document.documentElement.classList.contains("font-geist") }))
  const thumb = async () => {   // shadcn's switch as drawn: a 1rem thumb in its 2 × 1.15rem track
    await win.evaluate(() => { location.hash = "#/rules" })
    const t = win.locator("[data-slot=switch-thumb]").first()
    await t.waitFor()
    return t.evaluate((e) => [e.getBoundingClientRect().width, e.parentElement.getBoundingClientRect().height])
  }
  expect(await look()).toEqual({ size: "16px", geist: false })                 // 标准, the system's font
  const [w16, h16] = await thumb()
  expect(w16).toBe(16)
  expect(h16).toBeCloseTo(18.4, 1)
  try {
    await win.evaluate(() => { location.hash = "#/settings?s=appearance" })
    await win.getByRole("radio", { name: /^大/ }).click()
    await win.getByRole("radio", { name: "Geist" }).click()
    await expect.poll(look).toEqual({ size: "17px", geist: true })
    const [w, h] = await thumb()
    expect(w).toBe(17)
    expect(h).toBeCloseTo(19.55, 1)
  } finally {   // as they were, for the tests after — pass or fail
    await win.evaluate(() => Promise.all([["ui.size", 16], ["ui.font", "system"]].map(([k, v]) =>
      fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: k, value: v }) }))))
    await win.reload()
    await expect.poll(look).toEqual({ size: "16px", geist: false })
  }
})

test("search, watchlist and drawings persist", async () => {
  await openChart("#/k/JP:7203")
  await win.keyboard.press("Meta+k")
  await win.keyboard.type("130a")
  await expect(win.getByRole("option", { name: /130A/ })).toBeVisible()
  await win.keyboard.press("Enter")
  await waitReady("JP:130A")

  // keys go to the page, not a leftover focused control. Don't click the chart for this: KLineChart
  // drops a second click within 500 ms as a failed double-click, which would swallow the drawing click.
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("w")
  await expect(win.getByRole("button", { name: "已在观察" })).toBeVisible()

  await win.getByRole("button", { name: "水平线" }).click()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const [x, y] = [box.x + box.width * 0.5, box.y + box.height * 0.3]
  await win.waitForTimeout(600) // a chart click within 500 ms of an earlier one (the previous test's) is dropped
  // KLineChart tracks the pointer: move into the pane first, then press and release
  await win.mouse.move(x - 20, y - 10)
  await win.mouse.move(x, y, { steps: 5 })
  await win.mouse.down()
  await win.mouse.up()
  await expect.poll(async () => win.evaluate(async () => (await (await fetch("/api/drawings?listing_id=JP:130A")).json()).length)).toBe(1)

  await win.reload()
  await openChart("#/k/JP:130A")
  await expect.poll(async () => win.evaluate(() => window.wbDebug.overlays().length)).toBe(1)

  // switching to another stock and back, or to weekly bars and back, must not delete the line
  const saved = async () => win.evaluate(async () => (await (await fetch("/api/drawings?listing_id=JP:130A")).json()).length)
  await openChart("#/k/JP:7203")
  await openChart("#/k/JP:130A")
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("2")
  await win.waitForFunction(() => window.wbDebug.view()?.period === "week" && window.wbDebug.ready() === "JP:130A")
  await win.keyboard.press("1")
  await win.waitForFunction(() => window.wbDebug.view()?.period === "day" && window.wbDebug.ready() === "JP:130A")
  expect(await saved()).toBe(1)
  await expect.poll(async () => win.evaluate(() => window.wbDebug.overlays().length)).toBe(1)

  // clearing them all asks first (the app's own question), and a moment to take it back
  await win.getByRole("button", { name: "清除这只股票的全部标注" }).click()
  const ask = win.getByTestId("ask")
  await expect(ask).toContainText("清除这只股票的全部标注？")
  await ask.getByRole("button", { name: "保留" }).click()
  expect(await saved()).toBe(1)
  await win.getByRole("button", { name: "清除这只股票的全部标注" }).click()
  await win.getByTestId("ask").getByRole("button", { name: "全部清除" }).click()
  await expect.poll(saved).toBe(0)
  await win.getByRole("button", { name: "撤销" }).click()                            // the toast's
  await expect.poll(saved).toBe(1)
  await expect.poll(async () => win.evaluate(() => window.wbDebug.overlays().length)).toBe(1)   // drawn again
})

test("a text note is typed in the app and saved with the drawing", async () => {
  await openChart("#/k/JP:6758")
  await win.getByRole("button", { name: "文字标注" }).click()
  await win.getByLabel("标注文字").fill("箱顶放量")
  await win.keyboard.press("Enter")
  await expect(win.getByRole("dialog")).toBeHidden() // its fading overlay would catch the click
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const [x, y] = [box.x + box.width * 0.4, box.y + box.height * 0.25]
  await win.mouse.move(x - 20, y - 10)
  await win.mouse.move(x, y, { steps: 5 })
  await win.mouse.down()
  await win.mouse.up()
  await expect.poll(async () => win.evaluate(async () =>
    (await (await fetch("/api/drawings?listing_id=JP:6758")).json()).map((d) => d.styles.text).join())).toBe("箱顶放量")
})
