// P3 (spec §10.1): AI is optional — without a key the settings say what is missing, the budget is kept, and the
// rest of the app does not depend on it. The app runs with an empty .env and no keychain, so this machine's own
// keys never reach a test.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-ai-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("without a key the AI settings say what is missing, and the budget can be set", async () => {
  await go("#/settings?s=ai")
  const ai = win.getByTestId("ai-settings")
  await expect(ai).toContainText("还没有填 DeepSeek 的 API key")
  await expect(ai.getByRole("button", { name: "测试连接" }).first()).toBeDisabled()
  await win.getByLabel("每天的 AI 预算").fill("3")
  await win.getByRole("button", { name: "保存 AI 设置" }).click()
  await expect(win.getByText("AI 设置已保存")).toBeVisible()
  await expect(ai).toContainText("今天已用 ¥0.00")
  await go("#/rules")
  await go("#/settings?s=ai")
  await expect(win.getByLabel("每天的 AI 预算")).toHaveValue("3")
})

test("each task's model is one of DeepSeek's current ones, and how hard it thinks is yours to set", async () => {
  await go("#/settings?s=ai")
  await expect(win.getByLabel("规则助手的模型")).toHaveValue("deepseek-v4-pro")        // deepseek-chat stopped on 2026-07-24
  await expect(win.getByLabel("个股研究的模型")).toHaveValue("deepseek-flash")
  await expect(win.getByLabel("个股研究的思考强度")).toHaveValue("high")
  await win.getByLabel("个股研究的思考强度").selectOption("none")
  await win.getByRole("button", { name: "保存 AI 设置" }).click()
  await expect(win.getByText("AI 设置已保存").first()).toBeVisible()
  // the save itself, not a toast an earlier test left up for a few seconds
  await expect.poll(() => win.evaluate(() => fetch("/api/ai/settings").then((r) => r.json()).then((j) => j.tasks.research.effort))).toBe("none")
  await expect(win.getByLabel("deepseek-v4-pro 命中缓存的输入价格")).toHaveValue("0.3")    // the prompt's cache hits, priced apart
})
