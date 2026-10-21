// 危险提醒 (design 2026-10-15 §3): the K-line block and its chart marks, the tags on 观察. The warnings are the API's
// (backend tests); here the pages show what it answers.
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
const W = { kind: "limit_up", label: "连板后易回落", date: "2026-09-29", days: 1, new: false, predicted: false,
  reason: "09-29 收盘涨停，已连续 2 天。", risk: "过去十年，连续 2 天涨停的股票之后约三个月平均比同样大小的股票少涨 5–7%……", brief: "" }

const UNLOCK = { ...W, kind: "unlock", label: "解禁前易走弱", date: "2026-10-28", days: 20, reason: "10-28 有占总股本 35% 的首发原始股解除限售（还有 20 个交易日）。" }
const RETAIL = { ...W, kind: "retail", label: "散户接盘易走弱", date: "2026-09-25", days: 3, reason: "到 09-25 的 20 天里，小单……" }

test.beforeAll(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-danger-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  await win.route("**/api/danger", async (r) => {
    const ids = JSON.parse(r.request().postData() ?? "{}").ids ?? []
    await r.fulfill({ json: { covered: true, why: null, day: "2026-09-30", unknown: {}, uncovered: {}, unlocks_from: "2026-10-15",
      warnings: ids.includes("CN:600519") ? { "CN:600519": [UNLOCK, W, RETAIL] } : {} } })
  })
  await win.route("**/api/danger/events?**", (r) => r.fulfill({ json: { covered: true, why: null,
    events: [{ date: "2026-09-29", kind: "limit_up", label: "连板后易回落" }] } }))
})

test.afterAll(async () => { await app?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("a stock's warning shows in the K-line panel, on its chart and as a tag on 观察", async () => {
  await go("#/k/CN:600519")
  const block = win.getByTestId("danger-block")
  await expect(block).toContainText("连板后易回落")
  await expect(block).toContainText("原因：09-29 收盘涨停，已连续 2 天。")         // why, then what may follow, each said
  await expect(block).toContainText("可能的风险：过去十年")
  await expect(block).toContainText("2026-10-15 以前的解禁没有存")              // a day before unlocks were kept is said
  await win.waitForFunction(() => (window.wbDebug?.painted().rule[0]?.danger ?? []).length === 1 && window.wbDebug.pixelOf("2026-09-29"))
  const at = await win.evaluate(() => window.wbDebug.pixelOf("2026-09-29"))     // resting on the day: what came that day
  await win.mouse.move(at.x - 30, at.y)
  await win.mouse.move(at.x, at.y, { steps: 4 })
  await expect(win.getByTestId("danger-hover")).toContainText("危险提醒 · 2026-09-29 连板后易回落")
  await expect(win.getByRole("button", { name: "加入观察" })).toBeEnabled({ timeout: 30_000 })
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("w")
  await go("#/watch")
  const tags = win.getByTestId("watch-list").getByTestId("danger-tags")
  await expect(tags).toContainText("连板后易回落")
  await expect(tags).toHaveAttribute("title", /原因：09-29 收盘涨停，已连续 2 天。\n可能的风险：/)
  // three long tags (10-17): the name stays whole and nothing runs off the list — they wrap on a line of their own
  const m = await win.evaluate(() => {
    const list = document.querySelector('[data-testid="watch-list"]'), name = [...list.querySelectorAll("span")].find((x) => x.textContent === "贵州茅台")
    return { over: list.scrollWidth - list.clientWidth, cut: name.scrollWidth > name.clientWidth + 1, width: name.getBoundingClientRect().width }
  })
  expect(m.over).toBeLessThanOrEqual(1)
  expect(m.width).toBeGreaterThan(0)
  expect(m.cut).toBe(false)
})
