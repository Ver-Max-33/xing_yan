// 指标库 (design 2026-10-10): an indicator written as 通达信 writes it — checked as you type, a name it does not know
// offered as a parameter, saved — and drawn on the K 线 from the 指标 menu, its values the backend's for the chart's own
// bars; a 同花顺 Level-2 formula is told at once what this system has no data for. Real Electron shell, demo data.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-ind-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)
const get = (url) => win.evaluate(async (u) => (await fetch(u)).json(), url)
const check = () => win.getByTestId("indicator-check")

test("an indicator written as 通达信 writes it is checked, saved and drawn on the K 线 with the backend's values", async () => {
  await win.evaluate((id) => localStorage.setItem("wb.kline.last", JSON.stringify(id)), ID)
  await go("#/indicators")
  await win.getByRole("button", { name: "新建指标" }).click()
  await win.getByLabel("名称", { exact: true }).fill("五日线差")
  await win.getByLabel("公式", { exact: true }).fill("MA5:MA(CLOSE,N),COLORRED;\nDIF:MA5-MA(CLOSE,20),COLORSTICK;")
  await expect(check()).toContainText("不认识的名字 N")
  await check().getByRole("button", { name: "把 N 加成参数" }).click()
  await win.getByLabel("参数 N 的值").fill("5")
  await expect(check()).toContainText("能画：另开一个副图")
  await expect(win.getByTestId("indicator-sample")).toContainText("MA5")
  await win.getByRole("button", { name: "保存", exact: true }).click()
  await expect.poll(() => win.evaluate(() => location.hash)).toMatch(/^#\/indicators\/u[0-9a-f]+$/)
  const id = (await win.evaluate(() => location.hash)).split("/").pop()

  await win.getByRole("button", { name: "在 K 线里看" }).click()
  await win.waitForFunction((n) => (window.wbDebug?.indicator(n)?.length ?? 0) > 0, `WB_LIB_${id}`)
  const drawn = await win.evaluate((n) => window.wbDebug.indicator(n), `WB_LIB_${id}`)
  const bars = await win.evaluate(() => window.wbDebug.bars())
  const api = await get(`/api/indicator-series?listing_id=${ID}&id=${id}`)
  const col = (name) => api.items.find((it) => it.name === name).cols.value
  const at = new Map(api.t.map((t, i) => [t, i]))
  for (const [k, bar] of bars.entries()) {   // every bar on the chart has the backend's value for it, to the cent
    const i = at.get(bar.timestamp)
    expect(i).not.toBeUndefined()
    expect(drawn[k][col("MA5")] ?? null).toBe(api.values[col("MA5")][i])
    expect(drawn[k][col("DIF")] ?? null).toBe(api.values[col("DIF")][i])
    if (bar.ma5 !== null && bar.ma5 !== undefined) expect(Math.abs(drawn[k][col("MA5")] - bar.ma5)).toBeLessThan(1e-9)
  }
})

test("a 同花顺 Level-2 formula is told at once what this system has no data for", async () => {
  await go("#/indicators/new?paste=1")
  await win.getByLabel("名称", { exact: true }).fill("同花顺")
  await win.getByLabel("公式", { exact: true }).fill("ZDMRT:=BIGBUYCOUNT1;\nZDMCT:=BIGSELLCOUNT1;\n资金:=ZJLR-ZJLC;\n动能:SUM(ZDMRT-ZDMCT,3)/ZYLTGB;\nFX:SMA(MAX(CLOSE-PRE,0),3,1)/SMA(ABS(CLOSE-PRE),3,1)*100;")
  const unsupported = win.getByTestId("indicator-unsupported")
  await expect(unsupported).toContainText("BIGBUYCOUNT1、BIGSELLCOUNT1、ZJLR、ZJLC")
  await expect(unsupported).toContainText("ZYLTGB")
  await unsupported.getByRole("button", { name: "只保留能算的部分" }).click()
  await expect(win.getByLabel("公式", { exact: true })).toHaveValue("FX:SMA(MAX(CLOSE-PRE,0),3,1)/SMA(ABS(CLOSE-PRE),3,1)*100;")
  await expect(check()).toContainText("能画")
  await expect(win.getByRole("button", { name: "保存", exact: true })).toBeEnabled()
})

test("a formula 通达信 saved on Windows (GBK, CRLF lines) is read into the formula, named after its file", async () => {
  await go("#/indicators/new")
  await win.getByLabel("名称", { exact: true }).fill("")                // a name you typed would stay
  const buffer = Buffer.concat([Buffer.from([0xd6, 0xf7, 0xc1, 0xa6]), Buffer.from(":MA(C,5);\r\nDIF:C-MA(C,20);\r\n")])   // 主力 in GBK
  await win.getByLabel("选择 YAML 或公式文件").setInputFiles({ name: "主力动能.txt", mimeType: "text/plain", buffer })
  await expect(win.getByLabel("公式", { exact: true })).toHaveValue("主力:MA(C,5);\nDIF:C-MA(C,20);")
  await expect(win.getByLabel("名称", { exact: true })).toHaveValue("主力动能")
  await expect(check()).toContainText("能画")
})

test("the 指标 menu's 指标库 puts a built-in on the chart and takes it off", async () => {
  await win.evaluate(() => localStorage.setItem("wb.kline.lib", "[]"))
  await go(`#/k/${ID}`)
  await win.waitForFunction((id) => window.wbDebug?.ready() === id, ID)
  await win.getByRole("button", { name: "指标", exact: true }).click()
  await win.getByRole("menuitem", { name: /指标库/ }).click()
  const rsi = win.getByRole("menuitemcheckbox", { name: "RSI 相对强弱" })
  await expect(rsi).toHaveAttribute("aria-checked", "false")
  await rsi.click()
  await expect(rsi).toHaveAttribute("aria-checked", "true")
  await win.waitForFunction(() => (window.wbDebug.indicator("WB_LIB_rsi")?.length ?? 0) > 0)
  const api = await get(`/api/indicator-series?listing_id=${ID}&id=rsi`)
  expect(api.pane).toBe("sub")
  // the Windows runner found the submenu closed here (10-12, run 38267039657): its system cursor is not where the test's
  // is, and a move the window reports there can land on the menu behind — opened again if so (a hand's cursor stays put)
  if (!(await rsi.isVisible())) {
    console.log("指标库 closed after the first tick; focus on:", await win.evaluate(() => document.activeElement?.textContent?.slice(0, 30)))
    if (!(await win.getByRole("menuitem", { name: /指标库/ }).isVisible())) await win.getByRole("button", { name: "指标", exact: true }).click()
    await win.getByRole("menuitem", { name: /指标库/ }).click()
  }
  await rsi.click()
  await expect.poll(() => win.evaluate(() => window.wbDebug.indicator("WB_LIB_rsi"))).toBeNull()
  await win.keyboard.press("Escape")
})

test("text cut short reads in full where the pointer rests, a title as the app's own tip (your 10-10 screenshot)", async () => {
  await go("#/indicators")
  const row = win.getByTestId("indicator-ict_fvg")
  const full = await row.locator("div[title]").getAttribute("title")   // its note, cut short in the table
  const note = row.getByText(full, { exact: true })
  expect(await note.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true)
  const tip = win.getByTestId("hover-tip")
  await note.hover()
  await expect(tip).toHaveText(full)
  await expect(note).not.toHaveAttribute("title")                       // the browser's own, a second later, stays away
  await win.getByRole("heading", { name: "指标库" }).hover()
  await expect(tip).toBeHidden()
  await expect(note).toHaveAttribute("title", full)                     // and is back once the pointer has left
  await note.hover()
  await expect(tip).toHaveText(full)
  await win.keyboard.press("Shift")                                     // a key, a click or the wheel puts it away
  await expect(tip).toBeHidden()
})
