// One market with data, the other with none — the user's own shape on 2026-10-11 (A 股 正式档 with data, 日股 测试档
// with nothing in it), which every other test, all on demo data in both markets, never had. Each page says what the
// empty market lacks where it is looked at, never as an error, and the market with data works as usual.
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, "..", "..")
let app
let win

test.beforeAll(async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-onemarket-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  const api = (method, p, body) => win.evaluate(async ([method, p, body]) => (await fetch(p, {
    method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined })).status, [method, p, body])
  expect(await api("PUT", "/api/settings", { key: "profile.JP", value: "prod" })).toBe(200)   // 日股's 正式档 holds nothing
  expect(await api("PUT", "/api/rules/tpl_shang_pin/state", { enabled: true })).toBe(200)
  expect(await api("POST", "/api/runs", { rule_id: "tpl_shang_pin", market: "CN" })).toBe(200)
  await win.evaluate(() => { localStorage.setItem("wb.kline.last", JSON.stringify("JP:7203")) })   // last looked at when 日股 had data
  await win.reload()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 60_000 })
})

test.afterAll(async () => { await app?.close() })

const go = async (hash) => {
  await win.evaluate((h) => { location.hash = h }, hash)
  await win.waitForTimeout(1500)
}
const errorToasts = () => win.locator("[data-sonner-toast][data-type=error]").allTextContents()

test("a stock of the market with no data: why there is no chart, not the last stock's chart under its name", async () => {
  await go("#/k/CN:600519")
  await expect(win.getByText("贵州茅台", { exact: true }).first()).toBeVisible({ timeout: 30_000 })   // its chart drawn
  await go("#/k/JP:7203")
  const missing = win.getByTestId("kline-missing")
  await expect(missing).toContainText("日股现在的数据档（正式档）里还没有数据")
  await expect(missing).toBeVisible()   // it covers the chart that was there
})

test("今日 and 选股 on the empty market's tab say it has no data, not that nothing changed or matched", async () => {
  await go("#/")
  await win.getByRole("tab", { name: /^日股/ }).first().click()
  const steps = win.getByTestId("first-steps")
  await expect(steps).toContainText("取数据")
  await expect(steps.locator("svg.lucide-circle-check")).toHaveCount(1)   // 已启用规则 only: 日股 has no data, nothing scanned
  await go("#/screen?rule=tpl_shang_pin")
  await win.getByRole("tab", { name: /^日股/ }).first().click()
  await expect(win.getByText("日股现在的数据档（正式档）里还没有数据")).toBeVisible()
  await expect(win.getByText("没有符合的股票")).toHaveCount(0)
  await win.getByRole("tab", { name: /^A 股/ }).first().click()
  await expect(win.getByText("日股现在的数据档（正式档）里还没有数据")).toHaveCount(0)
})

test("a rule and an indicator open on a stock whose market has data, and the gallery samples only such markets", async () => {
  const asked = []
  win.on("request", (r) => { if (/\/api\/(preview|gallery)$/.test(new URL(r.url()).pathname)) asked.push(r.postData() ?? "") })
  await go("#/rules/tpl_shang_pin")
  await win.waitForTimeout(1500)
  await win.getByRole("tab", { name: "样例图集" }).click()
  await win.waitForTimeout(2500)
  await go("#/indicators/rsi")
  await win.waitForTimeout(1500)
  expect(await errorToasts()).toEqual([])
  expect(asked.filter((b) => b.includes("JP:7203") || b.includes('"market":"JP"'))).toEqual([])   // nothing asked of 日股
})
