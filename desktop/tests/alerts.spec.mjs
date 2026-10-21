// 盘中提醒 (spec §7.2, §8.5 预警中心): the day's alerts on the 盯盘 page with what each waits for, muting a stock for
// the day, the alert settings, and a line drawn on the K-line page that alerts — 提醒线, or a right-click on any line.
// Demo data is never polled, so the day's alert is put in place before the app starts, as a poll would have kept it.
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

const ALERT = `
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from wb.config import get_paths
from wb.realtime.alerts import Alerts
now = datetime.now(ZoneInfo("Asia/Tokyo"))
Alerts(get_paths("test", sys.argv[1])).add(day=now.date(), market="JP", listing_id="JP:7203", kind="rule", key="tpl_shang_heng_shang",
    at=now.replace(tzinfo=None, microsecond=0), price=768.0, title="トヨタ自動車 7203 · 日股", text="上横上 · 短箱突破 v1 · 盘中满足（量按时间折算）",
    detail={"lines": ["JP:7203 盘中：信号", "  breakout.volume：现在 2.3，要 ≥ 1"]})
from wb.workspace.store import Workspace
line = Workspace(get_paths("test", sys.argv[1])).save_drawing("JP:6758", "horizontalStraightLine", [{"date": "2026-09-30", "price": 3400.0}], alert=True)
Alerts(get_paths("test", sys.argv[1])).add(day=now.date(), market="JP", listing_id="JP:6758", kind="line", key=line,
    at=now.replace(tzinfo=None, microsecond=0), price=3401.0, title="ソニーグループ 6758 · 日股", text="涨破你画的线 3,400")
`

test.beforeAll(async () => {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-alerts-"))
  const uv = (...args) => execFileSync("uv", ["run", "--project", path.join(repo, "backend"), ...args], { stdio: "inherit" })
  uv("wb", "--data-dir", dataDir, "devdata")
  uv("python", "-c", ALERT, dataDir)
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const get = (url) => win.evaluate(async (u) => (await fetch(u)).json(), url)
const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("the day's alerts, muting a stock for the day, and the alert settings", async () => {
  await go("#/live")
  const center = win.getByTestId("alert-center")
  const alert = center.getByTestId("alert").filter({ hasText: "トヨタ自動車" })
  await expect(alert).toContainText("盘中满足")
  await expect(alert).toContainText("未确认：收盘后按正式规则确认或撤销")
  await alert.getByRole("button", { name: "判定" }).click()                   // what the rule saw on today's bar
  await expect(alert.getByTestId("alert-lines")).toContainText("breakout.volume：现在 2.3")
  await alert.getByRole("button", { name: "忽略今天" }).click()
  await expect(alert).toContainText("今天不再提醒")
  expect((await get("/api/live/alerts")).muted).toEqual(["JP:7203"])

  const line = center.getByTestId("alert").filter({ hasText: "ソニーグループ" })   // deleting a line asks first, then says so
  await line.getByRole("button", { name: "删除这条线" }).click()
  const ask = win.getByTestId("ask")                                                 // the app's own question, buttons named
  await expect(ask).toContainText("删除这条线？")
  await ask.getByRole("button", { name: "保留" }).click()
  expect((await get("/api/drawings?listing_id=JP:6758")).length).toBe(1)
  await line.getByRole("button", { name: "删除这条线" }).click()
  await win.getByTestId("ask").getByRole("button", { name: "删除这条线" }).click()
  await expect(line).toContainText("线已删除")
  await expect.poll(async () => (await get("/api/drawings?listing_id=JP:6758")).length).toBe(0)

  await win.getByRole("button", { name: "提醒设置" }).click()
  const minutes = win.getByLabel("开盘后不发量能类预警的分钟数")
  await minutes.fill("30")
  await minutes.press("Enter")
  await expect.poll(async () => (await get("/api/settings"))["live.quiet_minutes"]).toBe(30)
  await minutes.fill("")                                                     // an emptied field changes nothing, and says so
  await minutes.press("Enter")
  await expect(win.getByText("要填一个数")).toBeVisible()
  await expect.poll(async () => (await get("/api/settings"))["live.quiet_minutes"]).toBe(30)
  await expect(win.getByLabel("潜伏箱顶自动提醒")).toHaveCount(0)   // gone: 盯盘 watches your stocks only (10-12)
})

test("Esc or 光标 leaves a drawing tool: the next click draws nothing, and a placed line returns to 光标", async () => {
  await win.keyboard.press("Escape")
  await go("#/k/JP:7203")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:7203" && window.wbDebug.indicator("WB_MA")?.length > 0)
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const click = async (fx) => {
    const [x, y] = [box.x + box.width * fx, box.y + box.height * 0.3]
    await win.waitForTimeout(600) // KLineChart drops a second click within 500 ms
    await win.mouse.move(x - 20, y - 10)
    await win.mouse.move(x, y, { steps: 5 })
    await win.mouse.down()
    await win.mouse.up()
  }
  const drawings = () => get("/api/drawings?listing_id=JP:7203")
  const cursor = win.getByRole("button", { name: "光标 (Esc)" })
  const alertLine = win.getByRole("button", { name: /提醒线/ })
  await alertLine.click()
  await win.keyboard.press("Escape")
  await expect(cursor).toHaveAttribute("aria-pressed", "true")
  await click(0.4)                                                   // was: a saved price alert where you clicked
  await alertLine.click()
  await cursor.click()
  await click(0.5)
  await win.waitForTimeout(800)
  expect(await drawings()).toEqual([])
  await alertLine.click()
  await click(0.6)
  await expect.poll(async () => (await drawings()).length).toBe(1)   // drawn once picked …
  await expect(cursor).toHaveAttribute("aria-pressed", "true")       // … and the rail is back on 光标, as the chart is
  await click(0.7)
  await win.waitForTimeout(800)
  expect((await drawings()).length).toBe(1)
})

test("a line that alerts: drawn with 提醒线, turned off with a right-click", async () => {
  await win.keyboard.press("Escape")
  await go("#/k/JP:130A")
  await win.waitForFunction(() => window.wbDebug?.ready() === "JP:130A" && window.wbDebug.indicator("WB_MA")?.length > 0)
  await win.getByRole("button", { name: /提醒线/ }).click()
  const box = await win.locator("[data-testid=kline-chart]").boundingBox()
  const [x, y] = [box.x + box.width * 0.5, box.y + box.height * 0.3]
  await win.waitForTimeout(600) // KLineChart drops a second click within 500 ms
  await win.mouse.move(x - 20, y - 10)
  await win.mouse.move(x, y, { steps: 5 })
  await win.mouse.down()
  await win.mouse.up()
  const drawings = () => get("/api/drawings?listing_id=JP:130A")
  await expect.poll(async () => (await drawings()).map((d) => [d.kind, d.alert])).toEqual([["horizontalStraightLine", true]])

  await win.waitForTimeout(600)
  await win.mouse.move(x + 60, y, { steps: 5 })
  await win.mouse.click(x + 60, y, { button: "right" })
  await win.getByRole("menuitemcheckbox", { name: "关闭价格提醒" }).click()
  await expect.poll(async () => (await drawings()).map((d) => d.alert)).toEqual([false])
  expect(await win.evaluate(() => window.wbDebug.overlays().map((o) => o.alert))).toEqual([false])
})
