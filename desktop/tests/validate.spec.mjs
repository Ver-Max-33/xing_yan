// 验证 (spec §9, §13 P4 / P6 acceptance): an event study of a rule runs in the background and shows its numbers with the
// effective sample and the worst cases; a check-up lists what dropping each condition does; a formula is tested on
// its own against random entries; a book of each market is run with its trading rules and its cash ledger can be
// exported; the robustness check shows the parameters' heatmap and the costs raised; and the test profile is said to
// be only good for checking the flow.
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
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-validate-"))
  execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
})

test.afterAll(async () => { await app?.close() })

const send = (method, url, body) => win.evaluate(async ([m, u, b]) => {
  const r = await fetch(u, { method: m, headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
  return { status: r.status, body: await r.json() }
}, [method, url, body])
const go = (hash) => win.evaluate((h) => { location.hash = h }, hash)

const RULE = [
  "id: validate_e2e", "name: 端到端 · 站上均线", "markets: [CN, JP]",
  "stages:", '  - {id: today, kind: event, when: "C > MA(C, 20)", at: [{id: pos, expr: "RAW_C > 0"}]}',
  "signal_at: today.at", "",
].join("\n")

test("an event study, a check-up and one condition on its own", async () => {
  expect((await send("POST", "/api/rules", { text: RULE })).status).toBe(200)
  await go("#/validate?rule=validate_e2e&tab=event")
  await expect(win.getByText(/测试数据档/)).toBeVisible({ timeout: 30_000 })
  await expect(win.getByRole("button", { name: "重新运行" })).toHaveCount(0)            // nothing to run again yet: one button
  await win.getByRole("button", { name: "运行事件研究" }).click()
  const study = win.getByTestId("event-study")
  await expect(study).toContainText("有效样本", { timeout: 60_000 })
  await expect(win.getByRole("button", { name: "重新运行" })).toBeVisible()
  await expect(study).toContainText(/样本外胜了几个\s*\d+\/\d+/)   // the demo's few out-of-sample signals: a count, no 「更好」
  await expect(study).toContainText("样本少，不比较")
  await expect(study).not.toContainText("更好")
  await expect(study.getByTestId("next-open")).toContainText("低开的样本占")              // the 次日容易低开 claim, on the data
  await expect(study.getByTestId("worst-cases").getByRole("link").first()).toBeVisible()
  // 按交易计划 on top (a rule without one: the next open, 20 days): per signal day, against stocks bought at random
  const plan = study.getByTestId("plan-report")
  await expect(plan).toContainText("信号第二天开盘买")
  await expect(plan.getByTestId("plan-line")).toContainText("每个信号日平均")
  await expect(plan).toContainText("比同一天随机买的")
  await expect(plan.getByTestId("plan-years")).toBeVisible()
  await expect(plan.getByTestId("plan-halves")).toContainText("样本外（运行前固定）")
  // 买法对比: no 潜伏 line to cross — the signal close and the next open, the same sales (design 2026-10-11 §10)
  await expect(plan.getByTestId("plan-entries")).toContainText("尾盘确认买")
  await expect(plan.getByTestId("plan-entries")).toContainText("比较了 2 种买法")
  await win.getByRole("tab", { name: "条件体检" }).click()
  await win.getByRole("button", { name: "开始体检" }).click()
  const ablation = win.getByTestId("ablation")
  await expect(ablation).toContainText("RAW_C > 0", { timeout: 60_000 })
  await expect(ablation).toContainText(/多余|样本太少/)
  await expect(win.getByTestId("checkup")).toContainText("按交易计划每个信号日平均")         // judged by the plan
  // 候选条件归因: each candidate feature by its in-sample quintiles, a filter only where both halves agree
  await expect(win.getByTestId("attribution")).toContainText("离 20 日线")
  await expect(win.getByTestId("attribution")).toContainText(/单调|不单调|样本太少/)
  await expect(win.getByTestId("suggestions")).toBeVisible()
  await win.getByLabel("条件公式").fill("CROSS(DIF, DEA)")
  await win.getByRole("button", { name: "检验" }).click()
  await expect(win.getByRole("alert")).toContainText("不认识的名字 DIF")       // said where it was written
  // the stock-picking claims, one pick fills the formula, with what to look at
  await win.getByLabel("选股说法").selectOption("vwap5")
  await expect(win.getByLabel("条件公式")).toHaveValue("(C - VWAP) / REF(C, 1) >= 0.05")
  await expect(win.getByTestId("single-look")).toContainText("次日低开")
  await win.getByLabel("条件公式").fill("C > MA(C, 20)")
  await expect(win.getByTestId("single-look")).toHaveCount(0)
  await win.getByRole("button", { name: "检验" }).click()
  await expect(win.getByTestId("single-result")).toContainText("随机入场", { timeout: 60_000 })
  await expect(win.getByTestId("single-gap")).toContainText("次日低开")
})

const TUNED = [
  "id: book_e2e", "name: 端到端 · 组合回测", "markets: [CN, JP]",
  "params:", "  - {id: n, value: 20, range: [10, 30], step: 10, tune: true}",
  "stages:", '  - {id: today, kind: event, when: "C > MA(C, $n)"}',
  "signal_at: today.at", "",
].join("\n")

test("a book of each market with its ledger, and the robustness check", async () => {
  expect((await send("POST", "/api/rules", { text: TUNED })).status).toBe(200)
  await win.reload()                                    // the open page read the rule list before this rule existed
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 30_000 })
  await go("#/validate?rule=book_e2e&tab=backtest")
  await expect(win.getByLabel("规则", { exact: true })).toHaveValue("book_e2e")   // not 「按规则自己的判断离场」
  const form = win.getByTestId("book-form")
  await expect(form.getByTestId("book-costs-CN")).toContainText("T+1")
  await expect(form.getByLabel("CN 印花税（卖出）")).toHaveValue("0.05")                          // each market's own costs
  await form.getByLabel("最多持仓").fill("4")
  await form.getByLabel("移动止损").fill("8")
  await form.getByLabel("不创新高天数").fill("5")                                              // 有新高再看高 (research reading)
  await win.getByRole("button", { name: "运行组合回测" }).click()
  const book = win.getByTestId("book")
  await expect(book).toContainText("最大回撤", { timeout: 60_000 })
  await expect(book.getByTestId("book-split")).toContainText("样本外")
  await expect(book.getByRole("img", { name: "净值和回撤" })).toBeVisible()
  const run = await win.evaluate(() => fetch("/api/validate?rule_id=book_e2e&kind=backtest").then((r) => r.json()))
  expect(run[0].status).toBe("done")
  const done = await win.evaluate((id) => fetch(`/api/validate/${id}`).then((r) => r.json()), run[0].id)
  expect([done.settings.CN.max_positions, done.settings.CN.trail, done.settings.CN.t_plus_1, done.settings.JP.t_plus_1]).toEqual([4, 0.08, true, false])
  expect([done.settings.CN.stall, done.settings.JP.weak_positions]).toEqual([5, null])
  // what the result was computed with; once the form above says something else, the page says so
  await expect(win.getByTestId("run-settings")).toContainText(   // the rule's own judgement first (on by default), the fixed exits for a rule without one
    "最多 4 只 · 按规则自己的失效线、目标位和天数（规则没写时：持有 20 天、跌破箱底）、移动止损 8%、5 天不创新高")
  await form.getByLabel("最多持仓").fill("5")
  await expect(win.getByTestId("run-settings")).toContainText("上面的交易设定改过了")
  await form.getByLabel("最多持仓").fill("4")
  await expect(win.getByTestId("run-settings")).not.toContainText("上面的交易设定改过了")
  await book.getByRole("tab", { name: "日股" }).click()                                         // a JP book: what its data cannot book
  await expect(book).toContainText("分红没有记")
  // Electron saves a download through its own dialog: the export is caught in the page (its name and its text)
  await win.evaluate(() => {
    window.__saved = []
    const blobs = new Map(), make = URL.createObjectURL
    URL.createObjectURL = (blob) => { const url = make(blob); blobs.set(url, blob); return url }
    HTMLAnchorElement.prototype.click = function () { if (this.download) window.__saved.push({ name: this.download, blob: blobs.get(this.href) }) }
  })
  await win.getByRole("button", { name: "导出" }).click()
  await win.getByRole("menuitem", { name: "资金账本（CSV）" }).click()
  await expect.poll(() => win.evaluate(() => window.__saved.length)).toBe(1)
  const saved = await win.evaluate(async () => ({ name: window.__saved[0].name, text: await window.__saved[0].blob.text() }))
  expect(saved.name).toMatch(/^组合回测-资金账本-book_e2e-v1-/)
  expect(saved.text).toContain("市场,日期,代码,名称,类别,股数,价格,金额,说明")
  expect(saved.text).toMatch(/\nCN,[^\n]*,买入,/)
  await win.getByRole("tab", { name: "稳健性" }).click()
  await win.getByRole("button", { name: "开始检验" }).click()
  const robust = win.getByTestId("robust")
  await expect(robust.getByTestId("heatmap")).toBeVisible({ timeout: 90_000 })
  await expect(robust.getByTestId("heatmap").locator("[aria-current]")).toHaveCount(1)           // the rule's own value, framed
  await expect(robust.getByTestId("cost-stress")).toContainText("× 3")
})

test("a suggested filter is adopted into the rule's editor, unsaved (§9.5, §6.4)", async () => {
  // the first test's check-up, with one suggestion in its result (a planted order is not in the demo data)
  const runs = await win.evaluate(() => fetch("/api/validate?rule_id=validate_e2e&kind=checkup").then((r) => r.json()))
  const id = runs[0].id
  const split = { signals: 100, effective_in: 60, effective_out: 40, win_in: 0.5, win_out: 0.5, median_in: 0, median_out: 0 }
  await win.route(new RegExp(`/api/validate/${id}$`), async (route) => {
    const res = await route.fetch()
    const run = await res.json()
    run.result.attribution.suggestions = [{ feature: "dist20", label: "离 20 日线", op: ">", threshold: 0.01, condition: "MA_DIST(20) > 0.01",
      before: split, after: { ...split, signals: 80, win_in: 0.56, win_out: 0.55 } }]
    await route.fulfill({ response: res, json: run })
  })
  await go("#/validate?rule=validate_e2e&tab=checkup")
  await win.reload()   // the page may already show this run from the first test, kept: read again, through the route
  const s = win.getByTestId("suggestions")
  await expect(s).toContainText("MA_DIST(20) > 0.01")
  await expect(s).toContainText("50.0% → 55.0%")                                             // 样本外, before and after
  await s.getByRole("button", { name: "采纳到规则里" }).click()
  await expect(s.getByTestId("rule-diff")).toContainText("f_dist20")                          // what it changes, seen first
  await s.getByRole("button", { name: "放进编辑器" }).click()
  await expect(win.getByLabel("规则 YAML")).toHaveValue(/f_dist20[\s\S]*MA_DIST\(20\) > 0\.01/)   // in the editor, on the 规则 tab
  const saved = await win.evaluate(() => fetch("/api/rules/validate_e2e").then((r) => r.json()))
  expect(saved.text).not.toContain("f_dist20")                                                // saving makes the version
  await win.unrouteAll()
})

test("a run can be paused, continued or cancelled, and the last finished result stays in view meanwhile", async () => {
  // two finished event studies; the latest is then made to look running, paused — as the backend reports them
  for (let k = 0; k < 2; k++) {
    const { status, body } = await send("POST", "/api/validate", { kind: "event", rule_id: "validate_e2e", markets: ["CN"] })
    expect(status, JSON.stringify(body)).toBe(200)
    await expect.poll(() => win.evaluate((id) => fetch(`/api/validate/${id}`).then((r) => r.json()).then((r) => r.status), body.id),
      { timeout: 60_000 }).toBe("done")
  }
  const [latest] = await win.evaluate(() => fetch("/api/validate?rule_id=validate_e2e&kind=event").then((r) => r.json()))
  let say = null   // what the latest run is made to be
  await win.route("**/api/validate**", async (route) => {
    const res = await route.fetch()
    let j = await res.json()
    const url = route.request().url()
    if (say && url.includes("/api/validate?") && Array.isArray(j)) j = j.map((r) => (r.id === latest.id ? { ...r, ...say } : r))
    else if (say && url.endsWith(`/api/validate/${latest.id}`)) j = { ...j, result: null, ...say }
    await route.fulfill({ response: res, json: j })
  })
  say = { status: "running", progress: "A 股 第 3/7 批股票", asked: null }
  await go("#/validate?rule=validate_e2e&tab=event")
  await win.reload()
  const now = win.getByTestId("run-now")
  await expect(now).toContainText("A 股 第 3/7 批股票", { timeout: 30_000 })
  await expect(win.getByText("下面是上一次跑完的结果")).toBeVisible()
  await expect(win.getByTestId("event-study")).toContainText("有效样本")               // the result stays while it runs
  const paused = win.waitForRequest((r) => r.url().endsWith(`/api/validate/${latest.id}/pause`) && r.method() === "POST")
  await now.getByRole("button", { name: "暂停" }).click()
  await paused
  await now.getByRole("button", { name: "取消" }).click()
  const ask = win.getByRole("dialog")
  await expect(ask).toContainText("取消这次事件研究？")
  await ask.getByRole("button", { name: "继续运行" }).click()                          // nothing sent
  await expect(ask).toBeHidden()
  const cancelled = win.waitForRequest((r) => r.url().endsWith(`/api/validate/${latest.id}/cancel`) && r.method() === "POST")
  await now.getByRole("button", { name: "取消" }).click()
  await win.getByRole("dialog").getByRole("button", { name: "取消运行" }).click()
  await cancelled
  say = { status: "paused", progress: "已暂停，做到：A 股 第 3/7 批股票。做完的部分保留，「继续」接着算", asked: null }
  await win.reload()
  await expect(win.getByTestId("run-now")).toContainText("已暂停", { timeout: 30_000 })
  await expect(win.getByRole("button", { name: "继续运行" })).toBeVisible()           // the page's own button continues it
  const resumed = win.waitForRequest((r) => r.url().endsWith(`/api/validate/${latest.id}/resume`) && r.method() === "POST")
  await win.getByTestId("run-now").getByRole("button", { name: "继续" }).click()
  await resumed
  say = null
  await win.unrouteAll({ behavior: "wait" })
})

test("交易设定's numbers stay as you type them; one out of range is said and holds the run", async () => {
  expect((await send("POST", "/api/rules", { text: TUNED })).status).toBe(200)   // the same text: no new version
  await win.reload()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 30_000 })
  await go("#/validate?rule=book_e2e&tab=backtest")
  if (await win.getByRole("button", { name: "修改" }).isVisible()) await win.getByRole("button", { name: "修改" }).click()
  const form = win.getByTestId("book-form")
  const retype = async (field, text) => { await field.click(); await field.press("ControlOrMeta+a"); await field.press("Delete"); await field.pressSequentially(text) }
  const most = form.getByLabel("最多持仓")
  await most.click(); await most.press("ControlOrMeta+a"); await most.press("Delete")
  await expect(most).toHaveValue("")                                        // cleared stays cleared: no 1 under the caret
  await most.pressSequentially("8")
  await expect(most).toHaveValue("8")                                       // not 18
  const fee = form.getByLabel("CN 佣金")
  await retype(fee, "0.025")
  await expect(fee).toHaveValue("0.025")                                    // a percent as typed, not 25
  await retype(most, "60")
  await expect(form).toContainText("要在 1 到 50 之间")
  const go_ = win.getByRole("button", { name: /^(运行组合回测|重新回测)$/ })   // the first run, or again
  await expect(go_).toBeDisabled()
  await retype(most, "8")
  await expect(form).not.toContainText("要在 1 到 50 之间")
  const list = () => win.evaluate(() => fetch("/api/validate?rule_id=book_e2e&kind=backtest").then((r) => r.json()))
  const before = new Set((await list()).map((r) => r.id))
  await expect(go_).toBeEnabled({ timeout: 120_000 })   // one run at a time: the previous test's may still be going on a busy machine
  await go_.click()
  await expect.poll(async () => (await list()).find((r) => !before.has(r.id))?.status, { timeout: 60_000 }).toBe("done")
  const mine = (await list()).find((r) => !before.has(r.id))
  const done = await win.evaluate((id) => fetch(`/api/validate/${id}`).then((r) => r.json()), mine.id)
  expect([done.settings.CN.max_positions, done.settings.CN.costs.commission]).toEqual([8, 0.00025])   // what was typed is what ran
})
