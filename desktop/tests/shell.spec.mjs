// End-to-end: the real Electron shell starting the real backend (spec §3.3, §14 端到端测试).
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))

test("shell starts the backend, hands over the token, and stops it on quit", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-e2e-"))
  const app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  const win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  await expect(win.getByText("CN 无数据")).toBeVisible()
  // a first launch: 今日 says what to do, in order, and the scan waits for data
  const steps = win.getByTestId("first-steps")
  await expect(steps).toContainText("开始使用")
  await expect(steps.getByRole("link", { name: "去数据页" })).toBeVisible()
  await expect(win.getByRole("button", { name: "扫描全部市场" }).first()).toBeDisabled()

  // a test runs beside your own app: its own Chromium profile (localStorage: drafts, layouts, panels), never yours
  expect(await app.evaluate(({ app: a }) => a.getPath("userData"))).toBe(path.join(dataDir, "electron"))

  // the renderer has no Node access; the preload exposes exactly five members
  const bridge = await win.evaluate(() => [typeof window.require, Object.keys(window.wb ?? {}).sort()])
  expect(bridge).toEqual(["undefined", ["installUpdate", "openExternal", "platform", "setKeepAwake", "setTitleBar"]])

  // a page gets no permission at all (camera, microphone, notifications, location …): the app needs none
  expect(await win.evaluate(() => Notification.requestPermission())).toBe("denied")
  expect(await win.evaluate(() => navigator.permissions.query({ name: "geolocation" }).then((p) => p.state))).toBe("denied")

  // same-origin API calls carry the httpOnly session cookie; the cookie is not readable from JS
  expect(await win.evaluate(async () => (await fetch("/api/status")).status)).toBe(200)
  expect(await win.evaluate(() => document.cookie.includes("wb_token"))).toBe(false)

  // the session token travels by environment and cookie only, never on a command line (ps shows those)
  const token = (await app.context().cookies()).find((c) => c.name === "wb_token")?.value
  expect(token?.length).toBeGreaterThan(20)
  const { execFileSync } = await import("node:child_process")
  const cmdlines = process.platform === "win32"   // wmic is gone from current Windows
    ? execFileSync("powershell", ["-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object CommandLine"], { encoding: "utf8" })
    : execFileSync("ps", ["-axww", "-o", "args="], { encoding: "utf8" })
  expect(cmdlines.includes(token)).toBe(false)

  const origin = new URL(win.url()).origin
  const before = await fetch(`${origin}/api/status`)
  expect(before.status).toBe(401) // a request without the token is refused

  await app.close()
  await new Promise((r) => setTimeout(r, 1500))
  await expect(fetch(`${origin}/api/health`)).rejects.toThrow() // backend stopped with the app
})

test("a new version: the status bar says so and 设置 → 关于 updates it — run from the repository, git pull says how", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-e2e-"))
  const app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  const win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  // what the installed app would be told (nothing here reaches GitHub): 9.9.9 is out, and once asked, downloading
  const MB = 1048576
  let asked = 0
  await win.route(/\/api\/update(\?.*)?$/, (route) => route.fulfill({ json: {
    current: "0.2.0", installable: true, latest: "9.9.9", newer: true, url: "https://github.com/Ver-Max-33/xingyan/releases/tag/v9.9.9",
    name: "形研 v9.9.9", published_at: "2026-10-11T00:00:00Z", notes: "这一版改了这些", error: null, checked_at: "2026-10-11T08:00:00" } }))
  await win.route(/\/api\/update\/download$/, (route) => {
    if (route.request().method() === "POST") asked += 1
    return route.fulfill({ json: asked ? { state: "downloading", version: "9.9.9", done: 105 * MB, total: 285 * MB, error: null }
      : { state: "idle", version: null, done: 0, total: 0, error: null } })
  })
  await win.reload()
  const chip = win.getByTestId("status-update")
  await expect(chip).toHaveText("新版本 9.9.9", { timeout: 30_000 })
  await chip.click()   // to 设置 → 关于, where the release notes are and the button that updates
  await expect(win.getByText("这一版改了这些")).toBeVisible()
  // the license, and where this version's source is (AGPL): the version running is its tag
  await expect(win.getByTestId("source")).toContainText("这一版是其中的标签 v0.2.0")
  await expect(win.getByTestId("source").getByRole("link")).toHaveAttribute("href", "https://github.com/Ver-Max-33/xing_yan")
  await expect(win.getByText(/PA_Agent 的代码和提示词（Copyright \(C\) 2026 PA Agent Contributors/)).toBeVisible()   // its own notice
  await win.getByTestId("update-install").click()
  expect(asked).toBe(1)
  await expect(win.getByTestId("update-download")).toHaveText("下载中 36%（105 / 285 MB）")
  await expect(chip).toHaveText("下载新版本 36%")
  await expect(win.getByTestId("update-install")).toBeDisabled()
  // this app runs from the repository: the main process installs nothing and says what to do instead
  await expect(win.getByText("从仓库运行的开发版不自己安装：git pull 后重新打开就是新版本")).toBeVisible()
  await app.close()
})

test("right-click in a text box gives 剪切 / 复制 / 粘贴 / 全选, on selected text 复制", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-e2e-"))
  const app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  const win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  // the menu is the system's (no screenshot shows it): what the main process builds is recorded, not shown
  await app.evaluate(({ Menu }) => {
    globalThis.built = []
    const build = Menu.buildFromTemplate.bind(Menu)
    Menu.buildFromTemplate = (t) => {
      globalThis.built.push(t.map((i) => i.label ?? i.type))
      const m = build(t)
      m.popup = () => {}
      return m
    }
  })
  await win.evaluate(() => { location.hash = "#/rules" })
  const box = win.getByPlaceholder(/例如：前面涨过一波/)
  await box.click({ button: "right" })
  const heading = win.getByText("规则库", { exact: true }).first()
  await heading.evaluate((el) => window.getSelection()?.selectAllChildren(el))   // (macOS selects the word on a right-click anyway)
  await heading.click({ button: "right" })
  await expect.poll(() => app.evaluate(() => globalThis.built)).toEqual([["剪切", "复制", "粘贴", "separator", "全选"], ["复制"]])
  await app.close()
})

test("the backend stops by itself when the app is killed (a crash, as far as it can tell)", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-e2e-"))
  const { execFileSync } = await import("node:child_process")
  execFileSync("uv", ["run", "--project", path.join(here, "..", "..", "backend"), "wb", "--data-dir", dataDir, "devdata"], { stdio: "ignore" })
  const app = await electron.launch({ args: [path.join(here, "..")], env: isolated(dataDir) })
  const win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
  const origin = new URL(win.url()).origin
  // the app's own main process: on Windows Playwright starts Electron through a shell, and app.process() is that shell —
  // killing it left the app (and so its backend) running, which read as the backend outliving a killed app
  const main = await app.evaluate(() => process.pid)
  process.kill(main, "SIGKILL")                                          // no quit handler runs
  const gone = await expect.poll(() => fetch(`${origin}/api/health`).then(() => "up", () => "gone"), { timeout: 30_000 }).toBe("gone")
    .then(() => true, () => false)
  if (!gone) {   // what the backend said and which processes were left — on Windows only the run's log shows them
    const log = path.join(dataDir, "logs", "backend.log")
    console.log("backend.log:\n" + (fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").slice(-40).join("\n") : "(none)"))
    console.log(process.platform === "win32"
      ? execFileSync("powershell", ["-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|uv|wb|electron' } | Format-Table ProcessId,ParentProcessId,Name,CommandLine -AutoSize -Wrap | Out-String -Width 400"], { encoding: "utf8" })
      : execFileSync("ps", ["-axo", "pid,ppid,command"], { encoding: "utf8" }).split("\n").filter((l) => /python|uv|wb |electron/i.test(l)).join("\n"))
  }
  expect(gone).toBe(true)
  // which way it learned (backend.log): on Windows the app's process ending, if not its pipe
  expect(fs.readFileSync(path.join(dataDir, "logs", "backend.log"), "utf8")).toMatch(/the app is gone \((its pipe closed|process \d+ ended)\)/)
})
