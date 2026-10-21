// 下载并安装 for real, on Windows (windows-check.yml, 自动更新): an installed old version (0.0.1) learns from GitHub that a
// newer release is out on the public repository; 更新到 x.y.z downloads that release's installer, checks it
// against the release's SHA256SUMS.txt, closes the app, installs it silently and opens it again. Runs only there:
//   WB_UPDATE_APP=<the installed 形研.exe> npx playwright test tests/update.spec.mjs
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const exe = process.env.WB_UPDATE_APP
test.skip(!exe || process.platform !== "win32", "the windows-check workflow's 自动更新 runs this")

/** PowerShell's answer as text (UTF-8: the paths have 形研 in them). */
const ps = (command) => execFileSync("powershell", ["-NoProfile", "-Command",
  `[Console]::OutputEncoding = [Text.Encoding]::UTF8; ${command}`], { encoding: "utf8" }).trim()

/** The version the uninstall entry says is installed, and the one the app's own executable carries. */
const installed = () => ({
  entry: ps(`(Get-ItemProperty "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*" | Where-Object { $_.DisplayName -like "形研*" }).DisplayVersion`),
  exe: ps(`(Get-Item -LiteralPath '${exe}').VersionInfo.ProductVersion`),
})

/** A backend running from the install folder that started after `since` and answers: the app the installer opened. */
async function openedAgain(since) {
  const out = ps(`Get-CimInstance Win32_Process -Filter "Name='wb-backend.exe'" | Select-Object ExecutablePath,CommandLine,` +
    `@{n='Started';e={[DateTimeOffset]::new($_.CreationDate).ToUnixTimeMilliseconds()}} | ConvertTo-Json -Compress`)
  for (const p of out ? [].concat(JSON.parse(out)) : []) {
    const port = /--port (\d+)/.exec(p.CommandLine ?? "")?.[1]
    const inside = (p.ExecutablePath ?? "").toLowerCase().startsWith(path.dirname(exe).toLowerCase() + "\\")
    if (!port || !inside || p.Started < since) continue
    try {
      if ((await fetch(`http://127.0.0.1:${port}/api/health`)).ok) return p
    } catch { /* still starting */ }
  }
  return null
}

test("an old version updates itself to the newest release and opens again", async () => {
  test.setTimeout(15 * 60_000)
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-update-"))
  const app = await electron.launch({ executablePath: exe,
    env: isolated(dataDir, { WB_GITHUB_API: "https://api.github.com" }) })   // the real releases
  const win = await app.firstWindow()
  await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 120_000 })
  expect((await win.evaluate(async () => (await fetch("/api/health")).json())).version).toBe("0.0.1")
  expect(installed()).toEqual({ entry: "0.0.1", exe: expect.stringMatching(/^0\.0\.1/) })
  await win.evaluate(() => { location.hash = "/settings?s=about" })
  const install = win.getByTestId("update-install")
  await expect(install).toBeVisible({ timeout: 60_000 })   // the check found a newer release, and this app can install it
  const latest = (await install.textContent()).replace("更新到", "").trim()
  expect(latest).toMatch(/^\d+\.\d+\.\d+$/)
  const closed = app.waitForEvent("close", { timeout: 10 * 60_000 })
  await install.click()
  await expect(win.getByTestId("update-download")).toContainText("下载", { timeout: 30_000 })
  await closed   // downloaded and checked: the app closed itself to install
  const closedAt = Date.now() - 2000
  console.log(`更新到 ${latest}：下载、核对完，应用已关闭`)

  // the installer, silent, puts the new version where the old one was and opens it
  const deadline = Date.now() + 5 * 60_000
  let now = null
  let backend = null
  while (Date.now() < deadline) {
    now = installed()
    backend = await openedAgain(closedAt)
    if (now.entry === latest && backend) break
    await new Promise((r) => setTimeout(r, 3000))
  }
  console.log(`装好的：${JSON.stringify(now)}；重新打开的后端：${JSON.stringify(backend)}`)
  try {
    expect(now.entry).toBe(latest)
    expect(now.exe.startsWith(latest)).toBe(true)
    expect(backend).not.toBeNull()
  } finally {
    try { execFileSync("taskkill", ["/IM", "形研.exe", "/T", "/F"], { stdio: "ignore" }) } catch { /* none left */ }
  }
})
