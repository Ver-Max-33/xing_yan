// 价格行为分析 in the app (design 2026-10-20 §6, §11, dev branch): on a demo stock's K 线 page the 价格行为 tab runs PA_Agent's
// two stages and shows where the stock is, the plan and the demo mark; the plan's lines are on the chart; the 研究 page
// lists the analysis. The model is a local stand-in speaking DeepSeek's protocol, answering from the answers the backend's
// tests use (backend/tests/data/pa): stage 1 is the conversation's first turn (2 messages), stage 2 goes on from it (4).
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import http from "node:http"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, "..", "..")
const data = path.join(repo, "backend", "tests", "data", "pa")
let app, win, dataDir, server
const asked = []
const firstSteps = ["stage1_proceed.json", "stage1_wait.json"]   // the second analysis's gate says wait: 不买
// PA's own codes and English left in what the panel shows (the folded 决策路径 is not shown): none may be read (10-21)
const ENGLISH = /\b(wait|proceed|bullish|bearish|neutral|normal_channel|trending_tr|trading_range|AIL|AIS|Always In|buy on pullback|signal1)\b/

/** Stage 2's limit plan from the K-line table's first row, as the backend tests' answers() fills it: 2% under K1's close,
 *  the stop 4% under the entry, TP1 at one R and TP2 at two, in whole cents. */
function stage2(table) {
  const close = Number(table.match(/^1\s+\|\s*\S+\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*([\d.]+)/m)[1])
  const entry = Math.round(close * 98), stop = Math.round(entry * 0.96)
  const v = { entry, stop, tp1: 2 * entry - stop, tp2: 3 * entry - 2 * stop }
  let text = fs.readFileSync(path.join(data, "stage2_limit.json"), "utf8")
  for (const [k, c] of [...Object.entries(v).map(([k, x]) => [k, (x / 100).toFixed(2)]), ["conf", "60"], ["win", "60"]]) {
    text = text.replaceAll(`"{${k}}"`, c).replaceAll(`{${k}}`, c)
  }
  return text
}

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    let body = ""
    req.on("data", (c) => { body += c })
    req.on("end", () => {
      const { messages } = JSON.parse(body)
      asked.push(messages.length)
      const content = messages.length === 2 ? fs.readFileSync(path.join(data, firstSteps.shift() ?? "stage1_wait.json"), "utf8")
        : stage2(messages[1].content)
      res.writeHead(200, { "Content-Type": "application/json" })
      res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content } }], usage: { prompt_tokens: 30000, completion_tokens: 3000 } }))
    })
  })
  await new Promise((r) => server.listen(0, "127.0.0.1", r))
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-pa-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir, {
    WB_SECRET_DEEPSEEK_API_KEY: "stand-in", WB_AI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
  }) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close(); server?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("an analysis from the 价格行为 tab: the stage, the plan, the demo mark, the lines on the chart, the record", async () => {
  await go("#/k/CN:600519")
  await expect(win.getByRole("button", { name: "加入观察" })).toBeEnabled({ timeout: 30_000 })
  await win.getByRole("button", { name: "AI 研究" }).click()
  await win.getByRole("tab", { name: "价格行为" }).click()
  await expect(win.getByTestId("pa-panel")).toContainText("还没有分析过这只股票", { timeout: 30_000 })
  expect(asked).toHaveLength(0)                                               // opening the tab calls no model
  await expect(win.getByTestId("pa-panel").locator("..")).toContainText("高峰时约 ¥1–1.5 一次，夜里和周末减半")
  await win.getByRole("button", { name: "分析", exact: true }).click()
  const result = win.getByTestId("pa-result")
  await expect(result).toContainText("现在所处的阶段", { timeout: 60_000 })
  await expect(result).toContainText("常规通道")
  await expect(result).toContainText("买点和卖点")
  await expect(result).toContainText("跌到买入价再买（挂限价）")
  await expect(result).toContainText("演示数据，不对账")
  await expect(result).toContainText("文中 K1 指最新一根日线")
  expect(await result.innerText()).not.toMatch(ENGLISH)
  await expect(result).toContainText("AI 估的胜率")                           // the AI's guess, never read as a measured rate
  await expect(result).toContainText("AI 自己估的，不是统计出来的")
  const text = await result.innerText()                                       // PA's own warning: folded away
  expect(text.slice(text.indexOf("风险提示"), text.indexOf("决策路径"))).not.toContain("突破质量")
  await expect(win.getByTestId("pa-path")).toContainText("程序的提示：")
  await expect(win.getByTestId("pa-path")).toContainText("突破质量")
  expect(await win.getByTestId("pa-path").textContent()).not.toMatch(ENGLISH)   // the folded path in Chinese too (10-21)
  expect(asked).toEqual([2, 4])                                               // stage 2 went on from stage 1's conversation
  await expect.poll(() => win.evaluate(() => window.wbDebug.painted().rule[0].plan.length), { timeout: 10_000 }).toBe(4)
  await win.getByRole("button", { name: "再分析一次" }).click()                // nothing changed since: the same analysis,
  await expect(win.getByText("结果就是已有的那次", { exact: false })).toBeVisible({ timeout: 10_000 })   // (10-21)
  expect(asked).toEqual([2, 4])                                               // no model asked — after a buy too (final review 1)
  await expect(result).toContainText("跌到买入价再买（挂限价）")
  await go("#/settings?s=ai")                                                 // another effort: another analysis
  await win.getByLabel("价格行为的思考强度").selectOption("max")
  await win.getByTestId("pa-task").getByRole("button", { name: "保存" }).click()
  await expect(win.getByText("价格行为的模型已保存").first()).toBeVisible()
  await go("#/k/CN:600519?panel=research&tab=pa")
  await win.getByRole("button", { name: "再分析一次" }).click({ timeout: 30_000 })   // this time it waits: 不买
  await expect(result).toContainText("这次不买，图上只画最近的支撑和阻力", { timeout: 60_000 })
  await expect(result).toContainText("阶段一闸门结论为「等待」")                  // PA's own sentence, its code said in Chinese
  expect(await result.innerText()).not.toMatch(ENGLISH)
  await expect.poll(() => win.evaluate(() => window.wbDebug.painted().rule[0].plan.map((l) => l.kind).join()),
    { timeout: 10_000 }).toBe("support,resistance")
  expect(asked).toEqual([2, 4, 2])                                            // the gate said wait: no stage 2
  await go("#/research")
  const records = win.getByTestId("pa-records")
  await expect(records).toContainText("600519", { timeout: 30_000 })
  await expect(records).toContainText("挂限价")
  await expect(records).toContainText("演示数据")
})
