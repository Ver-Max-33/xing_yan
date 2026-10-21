// P2 acceptance, the rule side (spec §6.3–§6.5, §5.10, §13): the rule library, writing a rule, single-stock
// debugging with sliders, the sample gallery with your verdicts, the missed-hit check, version comparison,
// and the label set with its blind redraws — in the real Electron shell on deterministic demo data.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-p2r-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const get = (url) => win.evaluate(async (u) => (await fetch(u)).json(), url)
const send = (method, url, body) => win.evaluate(async ([m, u, b]) => {
  const r = await fetch(u, { method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
  return { status: r.status, body: await r.json() }
}, [method, url, body])
const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)
// every market's hits are in: until then a later market's newer hits can move to the top of the list
const galleryLoaded = () => expect(win.getByTestId("gallery")).toHaveAttribute("data-loading", "false", { timeout: 60_000 })
// a sample drawn more than a week ago: a redraw comes at least a week after the drawing (spec §5.10)
const age = (labelId) => execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "python", "-c",
  "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE box_label SET created_at='2000-01-01T00:00:00' WHERE id=?\", (sys.argv[2],)); c.commit()",
  path.join(dataDir, "shared.sqlite"), labelId])

const RULE = (id, n = 20) => [
  `id: ${id}`, "name: 端到端 · 站上均线", "markets: [CN, JP]",
  "params:", `  - {id: n, value: ${n}, range: [5, 60], step: 1, tune: true}`,
  "stages:", '  - {id: today, kind: event, when: "C > MA(C, $n)", at: [{id: vol, expr: "V / MA(V, 5) >= 1.2"}]}',
  "signal_at: today.at", "",
].join("\n")

test("the library lists the rules; enabling one puts it in the sidebar, and it runs on a market", async () => {
  await go("#/rules")
  const row = win.getByTestId("rule-vol_burst")
  await expect(row).toContainText("爆大量")
  await row.getByRole("switch").click()
  await expect(win.getByRole("navigation").getByRole("link", { name: /爆大量/ })).toBeVisible()
  await row.getByRole("button", { name: /更多操作/ }).click()
  await win.getByRole("menuitem", { name: "在 A 股全市场运行" }).click()
  await expect(win.getByText(/（爆大量） · A 股 \d{4}-\d{2}-\d{2}：信号/)).toBeVisible({ timeout: 60_000 })
  await expect(row).toContainText(/A 股 \d{2}-\d{2} · 信号 \d+ · 潜伏 \d+/)
  await row.getByRole("switch").click()
})

test("a new rule is written, checked as you type, saved, and its id is not taken by a second new rule", async () => {
  await go("#/rules")
  await win.getByRole("button", { name: "新建规则" }).click()
  const editor = win.getByLabel("规则 YAML")
  await expect(editor).toBeVisible()
  await editor.fill("id: e2e_new\nname: 坏的\nmarkets: [CN]\nstages: []\nsignal_at: nope.at\n")
  await expect(win.getByTestId("rule-errors")).toBeVisible()
  await editor.fill(RULE("e2e_new").replace("signal_at:", "universe: {watchlist: 不存在的分组}\nsignal_at:"))
  await expect(win.getByTestId("rule-warning")).toContainText("没有叫「不存在的分组」的自选分组")   // not a silent empty pool
  await editor.fill(RULE("e2e_new"))
  await expect(win.getByText("规则可以运行")).toBeVisible()
  await expect(win.getByTestId("rule-warning")).toHaveCount(0)
  await win.getByRole("button", { name: "保存", exact: true }).click()
  await win.waitForFunction(() => location.hash === "#/rules/e2e_new")
  await expect(win.locator("header").getByText("v1", { exact: true })).toBeVisible()
  // another "new rule" with the same id is refused, never made v2 of this one
  await go("#/rules/new")
  await win.getByLabel("规则 YAML").fill(RULE("e2e_new", 30))
  await expect(win.getByText("规则可以运行")).toBeVisible()
  await win.getByRole("button", { name: "保存", exact: true }).click()
  await expect(win.getByText(/已经有规则 e2e_new/)).toBeVisible()
  expect((await get("/api/rules/e2e_new")).versions.length).toBe(1)
})

test("single-stock debugging: a slider changes the signals at once, and the change is measured on the sample", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_debug") })
  await go("#/k/CN:600519") // the stock the workspace starts on
  await win.waitForFunction(() => window.wbDebug?.ready() === "CN:600519")
  await go("#/rules/e2e_debug")
  await win.reload()
  await expect(win.getByTestId("judgement-log")).toBeVisible()
  await win.waitForFunction(() => (window.wbDebug.painted().rule[0]?.signals.length ?? 0) > 0)
  const before = await win.evaluate(() => window.wbDebug.painted().rule[0].signals.length)
  const slider = win.getByLabel("参数 n")
  await slider.focus()
  for (let i = 0; i < 25; i++) await win.keyboard.press("ArrowRight") // n 20 → 45
  await win.waitForFunction((b) => window.wbDebug.painted().rule[0].signals.length !== b, before)
  await expect(win.getByText("20 → 45")).toBeVisible()
  await win.getByRole("button", { name: "算一下这次改动的影响" }).click()
  await expect(win.getByTestId("impact")).toContainText(/规则命中\s*\d+ → \d+/, { timeout: 60_000 })
  // a signal day in the log explains itself
  await win.getByRole("button", { name: "撤销参数改动" }).click()
  const day = await win.evaluate(() => window.wbDebug.painted().rule[0].signals.map((s) => s.signal_date).sort().at(-1))
  await win.getByTestId("judgement-log").getByRole("button", { name: day }).click()
  await expect(win.getByTestId("detail-card")).toContainText(day)
})

test("the gallery takes your verdicts by key, undoes the last one, and the library counts them", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_gallery") })
  await go("#/rules/e2e_gallery")
  await win.getByRole("tab", { name: "样例图集" }).click()
  await expect(win.locator("[data-hit]").first()).toBeVisible({ timeout: 60_000 })
  await expect(win.locator("[data-testid=mini-chart] [tabindex='1']")).toHaveCount(0)   // thumbnails are no Tab stops
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("j")
  await win.keyboard.press("y")  // right, and on to the next
  await win.keyboard.press("n")  // wrong
  await win.keyboard.press("n")  // wrong
  await win.keyboard.press("u")  // undo the last
  await expect.poll(async () => (await get("/api/hit_labels?rule_id=e2e_gallery")).map((x) => x.verdict).sort()).toEqual(["right", "wrong"])
  await expect(win.getByRole("tab", { name: /对\s*1/ })).toBeVisible()
  // Enter on a focused 「对」 presses it: the hit is marked, nothing else opens (it opened another hit)
  await win.locator("[data-hit]").getByRole("button", { name: / 对$/ }).first().focus()
  await win.keyboard.press("Enter")
  await expect.poll(async () => (await get("/api/hit_labels?rule_id=e2e_gallery")).length).toBe(3)
  await expect(win.getByRole("tab", { name: "样例图集" })).toHaveAttribute("aria-selected", "true")
  await go("#/rules")
  await expect(win.getByTestId("rule-e2e_gallery")).toContainText("对 2 · 错 1")
})

test("the missed-hit check says where a stock stopped and what to relax", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_missed").replace("V / MA(V, 5) >= 1.2", "V / MA(V, 5) >= $k").replace("params:", "params:\n  - {id: k, value: 5, range: [1, 9], step: 0.5}") })
  await go("#/rules/e2e_missed")
  await win.getByRole("tab", { name: "漏检排查" }).click()
  await win.getByRole("button", { name: "选择股票" }).click()
  await win.getByLabel("搜索股票").fill("600519")
  await win.getByRole("menuitem", { name: /贵州茅台/ }).click()
  await win.getByLabel("从").fill("2026-06-01")
  await win.getByLabel("到").fill("2026-09-30")
  await win.getByRole("button", { name: "排查", exact: true }).click()
  const result = win.getByTestId("missed-result")
  await expect(result).toContainText("最接近的一天", { timeout: 60_000 })
  await expect(result).toContainText("差在")
  await expect(result).toContainText(/把参数 k 放宽到/)
})

test("two saved versions are compared on the same sample", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_versions", 20) })
  await send("POST", "/api/rules", { text: RULE("e2e_versions", 40) })
  await go("#/rules/e2e_versions")
  await win.getByRole("tab", { name: "版本对比" }).click()
  await win.getByLabel("旧版本").selectOption("1")
  await win.getByLabel("新版本").selectOption("2")
  await win.getByRole("button", { name: "比较", exact: true }).click()
  await expect(win.getByTestId("compare-result")).toContainText(/命中\s*\d+ → \d+/, { timeout: 60_000 })
  await expect(win.getByTestId("compare-result")).toContainText("新版本才有的")
})

test("the label set: progress, the acceptance line, and a disagreement taken through review", async () => {
  const dates = await get("/api/dates?listing_id=CN:600519")
  const bars = (await get("/api/bars?listing_id=CN:600519&period=day&basis=qfq&limit=200")).rows.filter((b) => b.date >= dates.at(-90) && b.date <= dates.at(-60))
  const made = await send("POST", "/api/labels", { listing_id: "CN:600519", cutoff: dates.at(-40), is_box: true, category: "flat", basis: "qfq",
    start: dates.at(-90), end: dates.at(-60), top: Math.max(...bars.map((b) => b.high)) * 1.2, bottom: Math.min(...bars.map((b) => b.low)) * 0.8,
    scale: "mid", touches: [], exclusions: [] }) // far wider than any box: a disagreement
  expect(made.status).toBe(200)
  await go("#/labels")
  await expect(win.getByText(/是箱体\s*1\s*\/\s*40/)).toBeVisible()
  await expect(win.getByTestId("acceptance")).toBeVisible({ timeout: 60_000 })
  const row = win.getByTestId(`label-${made.body.id}`)
  await expect(row).toContainText(/不一致|没有箱体/)
  await row.getByRole("button", { name: "我画错" }).click()
  await expect(row).toContainText("我画错")
  expect((await get("/api/labels")).find((l) => l.id === made.body.id).verdict).toBe("me_wrong")
  // open it where it was drawn: the chart stops at its cutoff
  await row.getByRole("button", { name: "打开" }).click()
  await win.waitForFunction((cut) => window.wbDebug?.replay() === cut && window.wbDebug.bars().at(-1)?.date === cut, dates.at(-40))
})

test("a blind redraw hides your old line and the algorithm's boxes, and is stored as your baseline", async () => {
  const dates = await get("/api/dates?listing_id=CN:600519")
  const bars = (await get("/api/bars?listing_id=CN:600519&period=day&basis=qfq&limit=200")).rows.filter((b) => b.date >= dates.at(-90) && b.date <= dates.at(-60))
  const made = await send("POST", "/api/labels", { listing_id: "CN:600519", cutoff: dates.at(-40), is_box: true, category: "flat", basis: "qfq",
    start: dates.at(-90), end: dates.at(-60), top: Math.max(...bars.map((b) => b.high)), bottom: Math.min(...bars.map((b) => b.low)),
    scale: "mid", touches: [], exclusions: [] })
  age(made.body.id)
  const mine = (await get("/api/labels?listing_id=CN:600519&basis=qfq")).find((l) => l.id === made.body.id)
  await go(`#/k/CN:600519?redraw=${mine.id}`)
  await win.reload() // the sample was made through the API: the stock's cached samples do not have it yet
  await expect(win.getByTestId("blind-banner")).toContainText("盲画：重画样例（") // the sample it redraws was found
  await win.waitForFunction((cut) => window.wbDebug.replay() === cut && window.wbDebug.bars().at(-1)?.date === cut, mine.cutoff)
  await expect.poll(() => win.evaluate(() => {
    const s = window.wbDebug.painted().struct[0]
    return s.structures.boxes.length + s.labels.length
  })).toBe(0)
  await win.getByRole("button", { name: "矩形 / 样例框" }).click()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  for (const [fx, fy] of [[0.4, 0.25], [0.6, 0.45]]) {
    const [x, y] = [box.x + box.width * fx, box.y + box.height * fy]
    await win.waitForTimeout(600)
    await win.mouse.move(x - 20, y - 10)
    await win.mouse.move(x, y, { steps: 5 })
    await win.mouse.down()
    await win.mouse.up()
  }
  await expect(win.getByRole("dialog", { name: "存为重画基线" })).toBeVisible()
  await win.getByRole("button", { name: "存为样例" }).click()
  await expect.poll(async () => (await get("/api/labels")).filter((l) => l.redraw_of === mine.id).length).toBe(1)
  await expect(win.getByTestId("blind-banner")).toBeHidden()
})

test("resting the pointer on a signal shows when it was known, the rule version and its numbers", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_hover") })
  await go("#/k/CN:600519")
  await win.reload()
  await win.waitForFunction(() => window.wbDebug?.ready() === "CN:600519")
  const on = win.getByRole("button", { name: "规则叠加", exact: true })
  if (await on.getAttribute("aria-pressed") !== "true") await on.click()
  await win.getByRole("button", { name: "选择规则" }).click()
  await win.getByRole("menuitem", { name: /端到端 · 站上均线/ }).first().click()
  await expect(win.getByRole("menu")).toBeHidden()
  await win.waitForFunction(() => (window.wbDebug.painted().rule[0]?.signals.length ?? 0) > 0)
  const day = await win.evaluate(() => window.wbDebug.painted().rule[0].signals.map((s) => s.signal_date).sort().at(-1))
  const at = await win.evaluate((d) => window.wbDebug.pixelOf(d), day)
  await win.mouse.move(at.x - 30, at.y)
  await win.mouse.move(at.x, at.y, { steps: 4 })
  const card = win.getByTestId("signal-hover")
  await expect(card).toContainText(`信号 ${day} · 收盘确认`)
  await expect(card).toContainText("v1")
  await expect(card).toContainText(/信号 · vol：[\d.]+ ≥ 1\.2/)   // by name: the lone event is 信号
  await on.click()
})

test("opening one rule after another shows the second rule's own text, never the first one's as its draft", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_first") })
  await send("POST", "/api/rules", { text: RULE("e2e_second", 30) })
  await go("#/rules/e2e_first")
  await win.getByRole("tab", { name: "规则" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: e2e_first/)
  await go("#/rules/e2e_second")
  await win.getByRole("tab", { name: "规则" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: e2e_second/)
  await expect(win.getByRole("button", { name: "撤销改动" })).toBeHidden() // nothing changed: no draft
})

test("a built-in is copied into a new rule of your own, and a YAML file is imported as one", async () => {
  await go("#/rules")
  await win.getByTestId("rule-vol_burst").getByRole("button", { name: /更多操作/ }).click()
  await win.getByRole("menuitem", { name: "复制为新规则" }).click()
  const editor = win.getByLabel("规则 YAML")
  await expect(editor).toHaveValue(/id: vol_burst_copy/)
  await expect(editor).toHaveValue(/name: 成交量突然放大（爆大量）（副本）/)
  await expect(win.getByText("规则可以运行")).toBeVisible()
  await win.getByRole("button", { name: "保存", exact: true }).click()
  await win.waitForFunction(() => location.hash === "#/rules/vol_burst_copy")
  await go("#/rules")
  await win.getByLabel("选择 YAML 文件").setInputFiles({ name: "mine.yaml", mimeType: "text/yaml", buffer: Buffer.from(RULE("e2e_imported")) })
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: e2e_imported/)
  await expect(win.getByText("规则可以运行")).toBeVisible()
  await win.getByRole("button", { name: "保存", exact: true }).click()
  await win.waitForFunction(() => location.hash === "#/rules/e2e_imported")
  expect((await get("/api/rules")).map((r) => r.id)).toEqual(expect.arrayContaining(["vol_burst_copy", "e2e_imported"]))
})

test("↵ in the gallery opens that day in single-stock debugging, and moving a slider keeps that day's judgement", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_enter") })
  await go("#/rules/e2e_enter")
  await win.getByRole("tab", { name: "样例图集" }).click()
  const first = win.locator("[data-hit]").first()
  await expect(first).toBeVisible({ timeout: 60_000 })
  await galleryLoaded()
  const day = (await first.getAttribute("data-hit")).split("|")[1]
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("Enter")
  await expect(win.getByTestId("detail-card")).toContainText(day, { timeout: 30_000 })
  await win.getByLabel("参数 n").focus()
  await win.keyboard.press("ArrowRight")
  await expect(win.getByText("20 → 21")).toBeVisible()
  await win.waitForTimeout(1_000) // the slider settles and the rule runs again
  await expect(win.getByTestId("detail-card")).toContainText(day, { timeout: 30_000 })
})

test("a day opened from the gallery is brought into view, however far back it is", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_reveal") })
  await go("#/rules/e2e_reveal")
  await win.getByRole("tab", { name: "样例图集" }).click()
  await expect(win.locator("[data-hit]").first()).toBeVisible({ timeout: 60_000 })
  await galleryLoaded()
  const keys = await win.locator("[data-hit]").evaluateAll((els) => els.map((e) => e.getAttribute("data-hit")))
  const oldest = keys.sort((a, b) => a.split("|")[1].localeCompare(b.split("|")[1]))[0]
  const day = oldest.split("|")[1]
  await win.locator(`[data-hit="${oldest}"]`).click()
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("Enter")
  await expect(win.getByTestId("detail-card")).toContainText(day, { timeout: 30_000 })
  await win.waitForFunction((d) => { const [a, b] = window.wbDebug.visible(); return a !== null && a <= d && d <= b }, day, { timeout: 15_000 })
  const [a, b] = await win.evaluate(() => window.wbDebug.visible())
  console.log(`opened ${day}; on screen ${a} – ${b}`)
})

test("another rule opens without the first rule's opened day or its new hits", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_switch_a") })
  await send("POST", "/api/rules", { text: RULE("e2e_switch_b", 30) })
  await go("#/rules/e2e_switch_a")
  await win.getByRole("tab", { name: "样例图集" }).click()
  const first = win.locator("[data-hit]").first()
  await expect(first).toBeVisible({ timeout: 60_000 })
  await galleryLoaded()
  const day = (await first.getAttribute("data-hit")).split("|")[1]
  await first.click()                                                         // selected, then opened with the key
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("Enter")
  await expect(win.getByRole("tab", { name: "单股调试" })).toHaveAttribute("aria-selected", "true")
  await expect(win.getByTestId("detail-card")).toBeVisible({ timeout: 30_000 })
  await win.getByLabel("参数 n").focus()
  for (let i = 0; i < 10; i++) await win.keyboard.press("ArrowRight") // n 20 → 30
  await win.getByRole("button", { name: "算一下这次改动的影响" }).click()
  await win.getByRole("button", { name: /看这 \d+ 个新命中/ }).click({ timeout: 60_000 })
  await expect(win.getByRole("tab", { name: /这次新增/ })).toBeVisible()
  await go("#/rules/e2e_switch_b")
  await expect(win.getByTestId("judgement-log")).toContainText(/点一个信号日|这只股票没有信号/, { timeout: 30_000 }) // rule B has run
  await expect(win.getByText(`判定日志 · ${day}`)).toBeHidden()
  await win.getByRole("tab", { name: "样例图集" }).click()
  await expect(win.locator("[data-hit]").first()).toBeVisible({ timeout: 60_000 })
  await expect(win.getByRole("tab", { name: /这次新增/ })).toBeHidden()
})

test("in replay, a signal's hover card shows its box as it was known that day", async () => {
  const BOX_RULE = ["id: e2e_boxhover", "name: 端到端 · 小箱突破", "markets: [CN, JP]", "stages:",
    "  - {id: box, kind: structure, use: BOX, with: {scale: short}}", "  - {id: up, kind: event, on: box.break_up}", "signal_at: up.at", ""].join("\n")
  await send("POST", "/api/rules", { text: BOX_RULE })
  const p = (await send("POST", "/api/preview", { text: BOX_RULE, listing_id: "CN:600519", basis: "qfq" })).body
  const boxes = new Map(p.structures.boxes.map((b) => [b.id, b]))
  const s = p.signals.find((x) => boxes.get(x.box)?.ended_at > x.signal_date) // the box's end is known only later
  expect(s).toBeTruthy()
  await win.reload()                                   // the rule was made through the API: the page's list gets it
  await go(`#/k/CN:600519?day=${s.signal_date}`)        // then the link (reloading after it raced the page taking ?day= off: 10-13, Windows)
  await win.waitForFunction((d) => window.wbDebug?.ready() === "CN:600519" && window.wbDebug.replay() === d, s.signal_date)
  const on = win.getByRole("button", { name: "规则叠加", exact: true })
  if (await on.getAttribute("aria-pressed") !== "true") await on.click()
  await win.getByRole("button", { name: "选择规则" }).click()
  await win.getByRole("menuitem", { name: /端到端 · 小箱突破/ }).first().click()
  await expect(win.getByRole("menu")).toBeHidden()
  await win.waitForFunction((d) => (window.wbDebug.painted().rule[0]?.signals ?? []).some((x) => x.signal_date === d), s.signal_date)
  const at = await win.evaluate((d) => window.wbDebug.pixelOf(d), s.signal_date)
  await win.mouse.move(at.x - 30, at.y)
  await win.mouse.move(at.x, at.y, { steps: 4 })
  const card = win.getByTestId("signal-hover")
  await expect(card).toContainText(`信号 ${s.signal_date}`)
  await expect(card).toContainText("box：小箱")
  expect(await card.innerText()).not.toMatch(/box：小箱 \d+ 根/) // how long the box lasts is not known yet
  await on.click()
  await go("#/k/CN:600519")
})

test("a market that cannot be sampled is named beside the other market's hits", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_onemarket") })
  await win.route("**/api/gallery", (route) => (JSON.parse(route.request().postData() ?? "{}").market === "JP"
    ? route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "JP 还没有数据快照" }) })
    : route.continue()))
  try {
    await go("#/rules/e2e_onemarket")
    await win.getByRole("tab", { name: "样例图集" }).click()
    await expect(win.locator("[data-hit]").first()).toBeVisible({ timeout: 60_000 })
    await expect(win.getByTestId("gallery-failed")).toContainText("JP 还没有数据快照")
  } finally {
    await win.unroute("**/api/gallery")
  }
})

test("copying a built-in you tuned keeps the values you set", async () => {
  await go("#/rules/vol_burst")
  await win.getByLabel("参数 multiple").focus()
  await win.keyboard.press("ArrowRight")
  await win.keyboard.press("ArrowRight") // 2 → 2.5
  await expect(win.getByText("2.00 → 2.50 倍")).toBeVisible()   // in its unit
  await win.getByRole("button", { name: "复制为新规则" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: multiple, value: 2\.5\b/)
})

test("with no edit left to compare, the comparison goes back to the two latest versions", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_fallback", 20) })
  await send("POST", "/api/rules", { text: RULE("e2e_fallback", 40) })
  await go("#/rules/e2e_fallback")
  await win.getByLabel("参数 n").focus()
  await win.keyboard.press("ArrowRight")
  await win.getByRole("tab", { name: "版本对比" }).click()
  await expect(win.getByLabel("新版本")).toHaveValue("current")
  await win.getByRole("button", { name: "撤销改动" }).click()
  await expect(win.getByLabel("旧版本")).toHaveValue("1")
  await expect(win.getByLabel("新版本")).toHaveValue("2")
})

test("a built-in template has one version: the comparison says so, and a dragged parameter is compared with it", async () => {
  await go("#/rules/vol_burst")
  await win.getByRole("tab", { name: "版本对比" }).click()
  await expect(win.getByTestId("compare-only")).toContainText(/内置模板只有 v\d+/)
  await expect(win.getByRole("button", { name: "比较", exact: true })).toHaveCount(0)
  await win.getByRole("tab", { name: "单股调试" }).click()
  await expect(win.getByText("信号：VOL_MULT ≥ $multiple OR SMALL_YANG_RUN(4, $small_yang)").first()).toBeVisible()   // what the slider moves, by name
  await win.getByLabel("参数 multiple").focus()
  await win.keyboard.press("ArrowRight")
  await win.getByRole("tab", { name: "版本对比" }).click()
  await expect(win.getByLabel("旧版本")).toHaveValue(/^\d+$/)
  await expect(win.getByLabel("新版本")).toHaveValue("current")
  await expect(win.getByRole("button", { name: "比较", exact: true })).toBeEnabled()
})

test("the rule reads back in plain Chinese, and a parameter chip changes the rule's text", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_understand") })
  await go("#/rules/e2e_understand")
  await win.getByRole("tab", { name: "规则" }).click()
  const u = win.getByTestId("understanding")
  await expect(u).toContainText("端到端 · 站上均线 · A 股和日股 · 1 个阶段")
  await expect(u).toContainText("① 信号（today，事件）：「C > MA(C,")   // a stage by its name, its id beside
  await expect(u).toContainText("那一根：vol — V / MA(V, 5) ≥ 1.2")
  await u.getByTitle(/参数 n：/).first().click()
  const input = win.getByLabel("参数 n 的值")
  await input.fill("30")
  await input.press("Enter")
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: n, value: 30,/)
  await expect(u.getByTitle(/参数 n：/).first()).toHaveText("30") // read back from the changed text
})

test("calibration scores the tuned parameter on your marks and a candidate moves the sliders", async () => {
  await send("POST", "/api/rules", { text: RULE("e2e_calibrate") })
  const hits = (await send("POST", "/api/gallery", { rule_id: "e2e_calibrate", market: "CN", sample: { n: 300 }, years: 2 })).body.hits
  for (const [h, v] of hits.slice(0, 6).map((h, i) => [h, i % 3 === 2 ? "wrong" : "right"])) {
    await send("PUT", "/api/hit_labels", { rule_id: "e2e_calibrate", listing_id: h.listing_id, signal_date: h.signal_date, verdict: v })
  }
  await go("#/rules/e2e_calibrate")
  await win.getByRole("tab", { name: "校准" }).click()
  await expect(win.getByTestId("calibrate")).toContainText("n（5–60，步长 1）")
  await win.getByRole("button", { name: "开始校准" }).click()
  await expect(win.getByTestId("calibrate-now").or(win.getByTestId("calibrate-result"))).toBeVisible()   // its progress strip, or already done
  const result = win.getByTestId("calibrate-result")
  await expect(result).toContainText("现在", { timeout: 120_000 })
  await expect(win.getByTestId("calibrate")).toContainText("多标一些")          // 6 marks: likely to fit only them
  const use = result.getByRole("button", { name: "用这组" }).first()
  const n = await use.locator("xpath=ancestor::tr").locator("td").nth(1).innerText()
  await use.click()
  await expect(win.getByTestId("judgement-log")).toBeVisible()
  if (n !== "20") await expect(win.getByText(`20 → ${n}`)).toBeVisible()
  await win.getByRole("tab", { name: "校准" }).click()
  await expect(win.getByTestId("calibrate-result")).toContainText("现在")     // the result is still there, not lost
  await go("#/today")
  await expect(win.getByTestId("calibrate")).toHaveCount(0)
  await go("#/rules/e2e_calibrate")
  await win.getByRole("tab", { name: "校准" }).click()
  await expect(win.getByTestId("calibrate-result")).toContainText("现在")     // kept with the rule: leaving the page loses nothing
  await expect(win.getByRole("button", { name: "重新校准" })).toBeEnabled()
})

test("the library lists the box templates and the general ones, each reading back what it does", async () => {
  await go("#/rules")
  const row = win.getByTestId("rule-tpl_xia_po_hui_xiang")
  await expect(row).toContainText("下破回箱")
  await expect(row).toContainText("跌破箱底后缩量、很快收回箱内（破箱回箱")
  await expect(win.getByTestId("rule-tpl_new_high")).toContainText("创 N 日新高")
  await win.getByTestId("rule-tpl_giraffe").click()
  await win.getByRole("tab", { name: "规则" }).click()
  await expect(win.getByTestId("understanding")).toContainText("连续涨停后横盘几天再突破（长颈鹿） · A 股和日股 · 3 个阶段")
  await expect(win.getByTestId("understanding")).toContainText("位置：低位或中继")
})

test("a rule whose formula cannot run is not said to run, and cannot be saved", async () => {
  await go("#/rules/new")
  await win.getByLabel("规则 YAML").fill([
    "id: e2e_broken", "name: 拼错了", "markets: [CN]", "params:", "  - {id: n, value: 20}", "stages:",
    '  - {id: today, kind: event, when: "C > MAA(C, $n)"}', "signal_at: today.at", "",
  ].join("\n"))
  await expect(win.getByTestId("rule-errors")).toContainText("不认识的函数 MAA")
  await expect(win.getByText("规则可以运行")).toHaveCount(0)
  await expect(win.getByRole("button", { name: /^保存/ })).toBeDisabled()
})
