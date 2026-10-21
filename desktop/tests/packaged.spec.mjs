// The installed app (spec §13 P4 acceptance): the packaged shell starts the PyInstaller backend from its resources —
// no Python, no Node, no uv — and its registries hold as many entries as the source's. Runs only against a build:
//   WB_PACKAGED_APP="dist/mac-arm64/形研.app/Contents/MacOS/形研" npx playwright test tests/packaged.spec.mjs
// (Windows: "dist\\win-unpacked\\形研.exe").
import { execFileSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { _electron as electron, expect, test } from "@playwright/test"
import { isolated } from "./env.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, "..", "..")
const exe = process.env.WB_PACKAGED_APP
test.skip(!exe, "set WB_PACKAGED_APP to a built app's executable")

test("the installed app runs its own backend with every registry entry", async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "wb-packaged-"))
  const backend = path.join(path.dirname(path.resolve(exe)), "..", "Resources", "backend", "wb-backend")
  execFileSync(fs.existsSync(backend) ? backend : path.join(path.dirname(path.resolve(exe)), "resources", "backend", "wb-backend.exe"),
    ["--data-dir", dataDir, "devdata"], { stdio: "inherit" })
  const want = JSON.parse(execFileSync("uv", ["run", "--project", path.join(repo, "backend"), "python", "-c",
    "import json; from wb import registry; print(json.dumps(registry.load_all()))"], { encoding: "utf-8" }))
  // nothing on PATH that the app could fall back to: the backend comes from the app's own resources
  const app = await electron.launch({ executablePath: path.resolve(exe), env: isolated(dataDir, { PATH: "/usr/bin:/bin" }) })
  try {
    const win = await app.firstWindow()
    await expect(win.getByText("形研", { exact: true })).toBeVisible({ timeout: 90_000 })
    const health = await win.evaluate(async () => (await fetch("/api/health")).json())
    expect(health.registries).toEqual(want)
    await win.evaluate(() => { location.hash = "#/k/CN:600519" })
    await expect(win.locator("[data-testid=kline-chart]")).toBeVisible({ timeout: 30_000 })
  } finally {
    await app.close()
  }
})
