// P3 acceptance (spec §6.1–§6.4, §13): a description becomes a checked rule read back by the program, the model's
// assumptions are cards you pick (no AI call), a phrase it cannot compute must be decided before the preview, one
// sentence edits the rule (the difference shown first), and the draft opens as a new rule in the workspace. The
// model is a local stand-in speaking the OpenAI-compatible protocol, so the test needs no key and no network.
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

const RULE = (vol) => [
  "id: shrink_breakout", "name: 横盘缩量后放量突破", "markets: [CN, JP]",
  "params:", `  - {id: vol, value: ${vol}, range: [1.0, 3.0], step: 0.25, tune: true}`,
  "stages:", "  - {id: box, kind: structure, use: BOX, with: {scale: mid}}",
  "  - id: up", "    kind: event", "    on: box.break_up", "    at:", '      - {id: volume, expr: "VOL_RATIO(5) >= $vol"}',
  "signal_at: up.at", "watch_stage: box", "",
].join("\n")
const DRAFT = {
  rule: RULE(1.5), note: "",
  assumptions: [{ id: "box_scale", text: "你说的横盘一两个月，按中箱识别", path: "stages.box.with.scale", chosen: "mid",
    options: [{ value: "short", label: "小箱" }, { value: "mid", label: "中箱" }, { value: "long", label: "大箱" }] }],
  unmapped: [{ phrase: "主力洗盘", reason: "看不到资金是谁的，无法直接计算", proxies: [
    { label: "用「缩量 + 长下影」近似", stage: "box", where: "during", expr: "COUNT(L < MIN(O, C) * 0.97) >= 2" }] }],
}
const EDIT = { rule: RULE(2.5), note: "突破量要求提高到 2.5 倍", assumptions: [], unmapped: [] }
const BROKEN = { rule: "id: broken\nstages: [", note: "", assumptions: [], unmapped: [] }   // never loads: fails all 3 tries

test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    let body = ""
    req.on("data", (c) => { body += c })
    req.on("end", () => {
      const msgs = JSON.parse(body).messages
      asked.push(msgs.at(-1).content)
      const first = msgs[1].content                                           // the request, also on a retry
      const answer = first.includes("坏掉") ? BROKEN : first.includes("用户要求的修改") ? EDIT : DRAFT
      setTimeout(() => {                                                      // "慢慢想": a model thinking for a while
        res.writeHead(200, { "Content-Type": "application/json" })
        res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: JSON.stringify(answer) } }],
          usage: { prompt_tokens: 12000, completion_tokens: 800 } }))
      }, first.includes("慢慢想") ? 4000 : 0)
    })
  })
  await new Promise((r) => server.listen(0, "127.0.0.1", r))
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-assist-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir, {
    WB_SECRET_DEEPSEEK_API_KEY: "stand-in", WB_AI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
  }) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close(); server?.close() })

const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

test("a description becomes a rule read back by the program, the cards change it without AI, and it opens for preview", async () => {
  await go("#/rules")
  await win.getByLabel("用一句话描述你的选股流程").fill("先横盘一两个月，期间缩量、主力洗盘，然后放量突破箱顶")
  await win.getByRole("button", { name: "AI 起草" }).click()
  await expect(win.getByLabel("你的选股描述")).toHaveValue(/主力洗盘/)
  await win.getByRole("button", { name: "起草规则" }).click()
  const u = win.getByTestId("understanding")
  await expect(u).toContainText("横盘缩量后放量突破 · A 股和日股 · 2 个阶段", { timeout: 30_000 })
  expect(asked.at(-1)).toContain("先横盘一两个月")
  await expect(win.getByTestId("draft-meta")).toContainText("规则检查通过")
  const preview = win.getByRole("button", { name: "去预览" })
  await expect(preview).toBeDisabled()                                        // 主力洗盘 is not decided yet
  await win.getByTestId("card-box_scale").getByRole("button", { name: /大箱/ }).click()
  await expect(u).toContainText("大箱（BOX")                                  // applied by the program
  const calls = asked.length
  await win.getByTestId("unmapped-0").getByText("删除这个说法").click()
  await expect(preview).toBeEnabled()
  expect(asked.length).toBe(calls)                                            // picking never calls the model
  await win.getByLabel("用一句话修改规则").fill("突破量要求高一点")
  await win.getByRole("button", { name: "生成修改" }).click()
  const proposal = win.getByTestId("edit-proposal")
  await expect(proposal).toContainText("突破量要求提高到 2.5 倍")
  await expect(proposal).toContainText("+  - {id: vol, value: 2.5")
  await win.getByRole("button", { name: "用这个修改" }).click()
  await expect(u).toContainText("vol = 2.5")
  await win.getByRole("button", { name: "去预览" }).click()
  await win.waitForFunction(() => location.hash.startsWith("#/rules/new"))
  await win.getByRole("tab", { name: "规则" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/id: shrink_breakout/)
})

test("an edit that fails its checks shows what the model wrote, and a new draft drops an old proposal", async () => {
  await go("#/rules/draft")
  await win.reload()
  await win.getByLabel("你的选股描述").fill("先横盘一两个月，然后放量突破箱顶")
  await win.getByRole("button", { name: /起草规则|重新起草/ }).click()
  await expect(win.getByTestId("draft-meta")).toContainText("规则检查通过", { timeout: 30_000 })
  await win.getByLabel("用一句话修改规则").fill("坏掉")
  await win.getByRole("button", { name: "生成修改" }).click()
  const failed = win.getByTestId("edit-failure")
  await expect(failed).toContainText("模型试了 3 次")
  await failed.getByText("AI 的原始输出").click()
  await expect(failed).toContainText("id: broken")                            // shown as it came (§6.1)
  await win.getByLabel("用一句话修改规则").fill("突破量要求高一点")
  await win.getByRole("button", { name: "生成修改" }).click()
  await expect(win.getByTestId("edit-proposal")).toBeVisible()
  await win.getByRole("button", { name: "重新起草" }).click()
  await expect(win.getByRole("button", { name: "重新起草" })).toBeEnabled({ timeout: 30_000 })
  await expect(win.getByTestId("edit-proposal")).toHaveCount(0)               // it was a change to the old draft
})

test("a draft you stop waiting for leaves the page as it was, ready to draft again at once", async () => {
  await go("#/rules/draft")
  await win.reload()
  await win.getByLabel("你的选股描述").fill("慢慢想：先横盘一两个月，然后放量突破箱顶")
  await win.getByRole("button", { name: /起草规则|重新起草/ }).click()
  await expect(win.getByRole("button", { name: /起草中/ })).toBeVisible()
  await win.getByRole("button", { name: "停止" }).click()
  await expect(win.getByRole("button", { name: /起草规则|重新起草/ })).toBeEnabled()   // not after the model answers
  await expect(win.getByTestId("draft-failure")).toHaveCount(0)               // stopping is not a failure
  await win.getByLabel("你的选股描述").fill("先横盘一两个月，然后放量突破箱顶")
  await win.getByRole("button", { name: /起草规则|重新起草/ }).click()
  await expect(win.getByTestId("draft-meta")).toContainText("规则检查通过", { timeout: 30_000 })
})

test("a saved rule's meaning is changed in one sentence: the difference is shown, and taking it leaves it unsaved", async () => {
  const send = (method, url, body) => win.evaluate(async ([m, u, b]) => {
    const r = await fetch(u, { method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
    return { status: r.status, body: await r.json() }
  }, [method, url, body])
  await send("POST", "/api/rules", { text: RULE(1.5) })
  await go("#/rules/shrink_breakout")
  await win.reload()
  await win.getByRole("tab", { name: "规则" }).click()
  await win.getByLabel("用一句话修改这条规则").fill("突破量要求高一点")
  await win.getByRole("button", { name: "生成修改" }).click()
  await expect(win.getByTestId("rule-diff")).toContainText("+  - {id: vol, value: 2.5")
  expect(asked.at(-1)).toContain("用户要求的修改：突破量要求高一点")
  await win.getByRole("button", { name: "用这个修改" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/value: 2\.5/)
  await expect(win.getByText("有改动")).toBeVisible()                        // in the editor, not saved yet
  await win.getByLabel("用一句话修改这条规则").fill("坏掉")
  await win.getByRole("button", { name: "生成修改" }).click()
  const failed = win.getByTestId("edit-failure")
  await expect(failed).toContainText("模型试了 3 次")
  await failed.getByText("AI 的原始输出").click()
  await expect(failed).toContainText("id: broken")
})
