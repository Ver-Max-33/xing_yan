// P6 acceptance (spec §10.3, §10.4, §13 "AI 结论都能追溯到来源"): on the K 线 page the evidence a research is given is
// there to read first; the answer cites it fact by fact; the next research is told what the last one said and shows
// what changed; it goes into your notes; every research is on the record with its forward evaluation. The model is a
// local stand-in speaking the OpenAI-compatible protocol, and the demo data never asks for real announcements, so the
// test needs no key and no network.
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
let app, win, dataDir, server
const asked = []

const cite = (text) => ({ text, refs: ["px.close"], inference: false })
const answer = (told) => ({
  summary: "价格在箱体里整理，等待方向。", support: [cite("收盘在箱体上半部")], against: [{ ...cite("量能还没放出来"), inference: true }],
  to_verify: ["近期有没有公告"], invalidation: [cite("收盘跌回箱体下沿以下")], next_review: ["放量越过箱顶时"], lean: "支持",
  continuity: told ? { verdict: "同向", text: "仍在箱体里", refs: ["px.close"] } : { verdict: "首次", text: "", refs: [] },
})

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    let body = ""
    req.on("data", (c) => { body += c })
    req.on("end", () => {
      const user = JSON.parse(body).messages[1].content
      asked.push(user)
      res.writeHead(200, { "Content-Type": "application/json" })
      res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: JSON.stringify(answer(user.includes("上次研究"))) } }],
        usage: { prompt_tokens: 3000, completion_tokens: 500 } }))
    })
  })
  await new Promise((r) => server.listen(0, "127.0.0.1", r))
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-research-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir, {
    WB_SECRET_DEEPSEEK_API_KEY: "stand-in", WB_AI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
  }) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close(); server?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("a stock's research: the evidence first, an answer citing it, the next one told the last, the note and the record", async () => {
  await go("#/k/CN:600519")
  await expect(win.getByRole("button", { name: "加入观察" })).toBeEnabled({ timeout: 30_000 })
  await win.getByRole("button", { name: "AI 研究" }).click()
  await win.getByRole("tab", { name: /资料/ }).click()
  await expect(win.getByTestId("research-evidence")).toContainText("收盘", { timeout: 30_000 })
  await expect(win.getByRole("button", { name: /取最新公告/ })).toHaveCount(0)   // demo data asks no real source
  expect(asked).toHaveLength(0)                                               // reading the evidence calls no model
  await win.getByRole("tab", { name: /AI 研究/ }).click()
  await win.getByRole("button", { name: "开始研究" }).click()
  const a = win.getByTestId("research-answer")
  await expect(a).toContainText("价格在箱体里整理，等待方向。", { timeout: 30_000 })
  await expect(a).toContainText("第一次研究这只股票")
  await expect(a).toContainText("处引用都核对过")
  await expect(a).toContainText("推断")                                       // an inference says it is one
  await expect(a.getByRole("button", { name: "F1" }).first()).toBeVisible()   // each conclusion's fact
  expect(asked[0]).toContain("[px.close]")
  await win.getByRole("button", { name: "重新研究" }).click()
  await expect(a).toContainText("和上次同向", { timeout: 30_000 })
  expect(asked.at(-1)).toContain("上次研究")
  await expect(a).toContainText("证据没有变化")                               // the same day: nothing new
  await win.getByRole("button", { name: "加入观察" }).click()
  await expect(win.getByRole("button", { name: "已在观察" })).toBeVisible()
  await win.getByRole("button", { name: "摘到笔记" }).click()
  await expect(win.getByText("摘到观察笔记里了")).toBeVisible()
  await go("#/research")
  const records = win.getByTestId("research-records")
  await expect(records).toContainText("贵州茅台")
  await expect(records.locator("tbody tr")).toHaveCount(2)
  await expect(records).toContainText("演示数据")
  await expect(win.getByTestId("research-evaluation")).toContainText("支持")
  await records.locator("tbody tr").first().getByText("价格在箱体里整理").click()       // the whole row opens its research
  await expect(win).toHaveURL(/#\/k\/CN(:|%3A)600519\?panel=research/)
  await expect(win.getByTestId("research-answer")).toContainText("价格在箱体里整理", { timeout: 30_000 })
})

test("a document you import is in the evidence, opens at its page, its excerpt is yours, and the research gets it", async () => {
  await go("#/k/CN:600519?panel=research")
  await win.getByRole("tab", { name: /资料/ }).click()
  const imported = win.getByTestId("imported")
  await imported.getByLabel("导入资料文件").setInputFiles({ name: "调研纪要.txt", mimeType: "text/plain",
    buffer: Buffer.from("调研纪要：产能利用率约八成，订单排到明年一季度，管理层预计下半年价格稳定。") })
  const viewer = win.getByTestId("doc-viewer")
  await expect(viewer).toContainText("调研纪要")
  await expect(win.getByTestId("doc-page")).toContainText("产能利用率约八成")
  await win.getByLabel("给研究的摘录").fill("产能利用率约八成，订单排到明年一季度")
  await viewer.getByRole("button", { name: /用作摘录/ }).click()
  await expect(win.getByText("摘录改成第 1 页的这段")).toBeVisible()
  await win.keyboard.press("Escape")
  await expect(imported).toContainText(/D\d+ ?你导入的资料《调研纪要》第 1 页：产能利用率约八成，订单排到明年一季度/)   // as the AI gets it
  await win.getByRole("tab", { name: /AI 研究/ }).click()
  const before = asked.length
  await win.getByRole("button", { name: "重新研究" }).click()
  await expect.poll(() => asked.length, { timeout: 30_000 }).toBeGreaterThan(before)    // this research's own request
  expect(asked.at(-1)).toContain("你导入的资料《调研纪要》第 1 页：产能利用率约八成，订单排到明年一季度")
  await win.getByRole("tab", { name: /资料/ }).click()
  await imported.getByRole("button", { name: "删除 调研纪要" }).click()
  await win.getByTestId("ask").getByRole("button", { name: "删除资料" }).click()   // the app's own question
  await expect(imported).not.toContainText("调研纪要")
})
