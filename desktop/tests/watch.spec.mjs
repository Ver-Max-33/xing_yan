// 观察 (spec §11, §2.1, design 10): a stock watched from the K-line page keeps where it came from; its reason and
// notes are kept (every version), a fail line shows how far the close is from it, setting it aside moves it to
// 暂不考虑 (still followed), and the record lists what happened.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-watch-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("a stock watched from the K-line page is followed on 观察 with its reason, notes, fail line and record", async () => {
  await go("#/k/CN:600519")
  await expect(win.getByRole("button", { name: "加入观察" })).toBeEnabled({ timeout: 30_000 })   // the stock has loaded: W works
  await win.evaluate(() => document.activeElement?.blur())
  await win.keyboard.press("w")
  await expect(win.getByRole("button", { name: "已在观察" })).toBeVisible()
  await go("#/watch")
  const list = win.getByTestId("watch-list")
  await expect(list).toContainText("贵州茅台")
  await expect(win.getByRole("tab", { name: /观察中\s*1/ })).toBeVisible()
  await list.getByText("贵州茅台").click()
  const detail = win.getByTestId("watch-detail")
  await expect(detail).toContainText("来自 K 线页")
  await detail.getByLabel("为什么关注").fill("箱体很干净，放量突破")
  await detail.getByLabel("我的笔记").fill("等决算")
  await detail.getByLabel("我的笔记").blur()
  await detail.getByLabel("条件说明").fill("收盘跌回箱顶")
  await detail.getByLabel("失效价位").fill("1")
  await detail.getByRole("button", { name: "加上" }).click()
  await expect(detail.getByTestId("watch-conditions")).toContainText("收盘跌回箱顶")
  await expect(detail.getByTestId("watch-conditions")).toContainText("距离")
  // 养箱's trigger: a line drawn on its chart with 价格提醒 on shows here too
  expect(await win.evaluate(() => fetch("/api/drawings/e2e-trigger", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ listing_id: "CN:600519", kind: "horizontalStraightLine", points: [{ date: "2026-09-30", price: 1800 }], alert: true }),
  }).then((r) => r.status))).toBe(200)
  await win.reload()
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  await expect(win.getByTestId("watch-lines")).toContainText("水平线 1,800.00")
  await expect(win.getByTestId("watch-detail").getByLabel("为什么关注")).toHaveValue("箱体很干净，放量突破")   // saved on leaving the box
  await win.getByTestId("watch-detail").getByLabel("状态").selectOption("passed")
  await expect(win.getByRole("tab", { name: /暂不考虑\s*1/ })).toBeVisible()
  await expect(win.getByTestId("watch-record")).toContainText("改为 暂不考虑")
  await expect(win.getByTestId("watch-record")).toContainText("加失效条件：收盘跌回箱顶")
})

test("typing on while the last save is still on its way is kept", async () => {
  await go("#/watch?tab=passed")
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  const box = win.getByTestId("watch-detail").getByLabel("我的笔记")
  await win.route("**/api/watch/*/text", async (route) => { await new Promise((r) => setTimeout(r, 800)); await route.continue() })
  await box.fill("第一句")
  await box.blur()                                    // saved, slowly
  await box.click()
  await box.press("End")
  await box.pressSequentially("，第二句")
  await win.waitForTimeout(1200)                      // the save came back while typing
  await expect(box).toHaveValue("第一句，第二句")
  await win.unrouteAll({ behavior: "wait" })          // the autosave's slowed save finishes first
  await box.blur()
  await win.reload()
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  await expect(win.getByTestId("watch-detail").getByLabel("我的笔记")).toHaveValue("第一句，第二句")
})

test("a note still being typed survives a reload and quitting the app, and says when it was saved", async () => {
  await win.evaluate(async () => {   // watched (it is already, with another status, when the whole file runs)
    const h = { "Content-Type": "application/json" }
    await fetch("/api/watch", { method: "POST", headers: h, body: JSON.stringify({ listing_id: "CN:600519", source: "kline" }) })
    await fetch(`/api/watch/${encodeURIComponent("CN:600519")}/status`, { method: "PUT", headers: h, body: JSON.stringify({ status: "watching" }) })
  })
  await go("#/watch")
  await win.reload()                                                           // the list as the backend has it now
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  const box = win.getByTestId("watch-detail").getByLabel("我的笔记")
  await box.fill("")
  await box.blur()
  await box.focus()
  await box.pressSequentially("回踩 20 日线缩量，等放量。")                      // the caret stays in the box
  await expect(win.getByTestId("watch-detail").getByText(/已保存 \d\d:\d\d/).first()).toBeVisible({ timeout: 5_000 })
  await box.pressSequentially("再等一天")                                      // no pause: still unsaved when the page goes
  await win.reload()
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  await expect(win.getByTestId("watch-detail").getByLabel("我的笔记")).toHaveValue("回踩 20 日线缩量，等放量。再等一天")
  // a save sent as the page went that never arrived — on Windows the reloaded page read the note first (2026-10-10):
  // what was typed was kept on this computer at once; it is put back and saved. (First what the reload above left is
  // saved and let go: on a slow machine that save, still going, would write over the draft set here.)
  await expect.poll(() => win.evaluate(() => JSON.parse(localStorage.getItem("wb.watch.unsaved.CN:600519.note") ?? "null") === null))
    .toBe(true)
  await win.evaluate(() => localStorage.setItem("wb.watch.unsaved.CN:600519.note",
    JSON.stringify({ text: "回踩 20 日线缩量，等放量。再等一天，等量", at: Date.now() })))
  await win.reload()
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  await expect(win.getByTestId("watch-detail").getByLabel("我的笔记")).toHaveValue("回踩 20 日线缩量，等放量。再等一天，等量")
  await expect.poll(() => win.evaluate(async () => (await (await fetch("/api/watch/CN%3A600519")).json()).note))
    .toBe("回踩 20 日线缩量，等放量。再等一天，等量")

  const again = win.getByTestId("watch-detail").getByLabel("我的笔记")
  await again.focus()
  await again.press("End")
  await again.pressSequentially("，周五看")                                    // quitting right away: no blur, no pause
  await app.close()
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  await go("#/watch")
  await win.getByTestId("watch-list").getByText("贵州茅台").click()
  await expect(win.getByTestId("watch-detail").getByLabel("我的笔记")).toHaveValue("回踩 20 日线缩量，等放量。再等一天，等量，周五看")
})
