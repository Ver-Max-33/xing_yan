// Electron main process (spec §3.3), kept thin on purpose:
//   1. pick a free port and a random session token
//   2. start the Python backend (dev: `uv run wb serve`; packaged: the PyInstaller binary in resources/backend)
//   3. poll /api/health, 4. hand the token to the page as an httpOnly cookie and open the window
//   5. stop the backend (with its children) on quit, once each window has let its page save what it still held — and
//      however else the app ends (a crash, killed), the backend
//      sees its stdin pipe close, or this process end, and stops itself — 6. keep the system awake while asked to,
//   7. 下载并安装: once the backend has downloaded and checked a new version's installer, close and run it (update.js).
// The renderer is sandboxed with no Node access; the preload exposes five members only.
const { app, BrowserWindow, dialog, ipcMain, Menu, powerSaveBlocker, session, shell } = require("electron")
const { spawn, spawnSync } = require("node:child_process")
const crypto = require("node:crypto")
const fs = require("node:fs")
const http = require("node:http")
const net = require("node:net")
const path = require("node:path")
const { MAC_SWAP, WINDOWS_ARGS, bundleToReplace, installerFor } = require("./update")

const REPO = path.resolve(__dirname, "..")
// tests and screenshots run beside your own app with their own Chromium profile (localStorage: drafts, layouts, panels)
if (process.env.WB_USER_DATA) app.setPath("userData", process.env.WB_USER_DATA)
// one 形研 at a time: a second launch (Windows: the shortcut double-clicked again while the first is still opening) only
// brings the first window forward — two would run two backends on one data folder: two daily updates sharing its
// staging (one's publish takes the other's half), two scans at once, every Feishu notice twice
if (!app.requestSingleInstanceLock()) app.exit(0)
app.on("second-instance", () => {
  const win = BrowserWindow.getAllWindows()[0]
  if (!win) return
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
})
const isMac = process.platform === "darwin"
let backend = null
let keepAwakeId = null
let api = null          // the backend's port and session token: the main process asks it about a download
let installing = null   // a download-and-install under way (one at a time)
let toRun = null        // the checked installer, run once every window has closed and the backend has stopped

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on("error", reject)
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

/** What runs the backend: in the installed app the PyInstaller binary and the built frontend from its resources,
 * in development `uv run wb serve` from the repository. */
function backendCommand(port) {
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, "backend")
    return { cmd: path.join(dir, isWin() ? "wb-backend.exe" : "wb-backend"), cwd: dir,
      args: ["serve", "--port", String(port), "--frontend", path.join(process.resourcesPath, "frontend"), "--exit-with-stdin"] }
  }
  return { cmd: "uv", cwd: path.join(REPO, "backend"), args: ["run", "--project", path.join(REPO, "backend"), "wb", "serve",
    "--port", String(port), "--frontend", path.join(REPO, "frontend", "dist"), "--exit-with-stdin"] }
}

/** Development: the window shows frontend/dist — built again first when its sources are newer (fresh.js); a failed build
 * says so and opens the last one. */
function buildFrontend() {
  const fe = path.join(REPO, "frontend")
  if (!require("./fresh").stale(fe)) return
  const r = spawnSync(isWin() ? "npm.cmd" : "npm", ["run", "build"], { cwd: fe, stdio: "inherit", windowsHide: true, shell: isWin() })
  if (r.status !== 0) {
    dialog.showMessageBoxSync({ type: "warning", title: "形研", message: "界面没能重新构建，打开的是上一次构建的版本",
      detail: "在仓库的 frontend 目录运行 npm run build 可以看到原因。" })
  }
}

function startBackend(port, token) {
  // the token goes by environment only: a command line is visible to every local process (ps)
  const { cmd, args, cwd } = backendCommand(port)
  // POSIX: own process group so the whole tree (uv -> python, or the binary and its pool) can be stopped together.
  // stdin is a pipe the backend watches (--exit-with-stdin): the OS closes it when this process ends, however it ends.
  // and it watches this process too (WB_PARENT_PID): on Windows the pipe alone did not always tell a killed app
  // stderr through here: what the backend says last is shown when it cannot start (the installed app has no console)
  const child = spawn(cmd, args, { cwd, detached: !isWin(), windowsHide: true, stdio: ["pipe", "inherit", "pipe"],
    env: { ...process.env, WB_TOKEN: token, WB_PARENT_PID: String(process.pid), WB_UPDATE_DIR: updateDir() } })
  child.stdin.on("error", () => {})   // a backend already gone: nothing to tell it
  child.said = ""
  child.stderr.on("data", (b) => {
    child.said = (child.said + b.toString()).slice(-4000)
    try { process.stderr.write(b) } catch { /* no console (the installed app on Windows) */ }
  })
  child.on("exit", (code) => { if (code && !app.isQuitting) console.error(`backend exited with ${code}`) })
  // rejects if the backend cannot be run or dies before it is healthy (fail fast, not after 60 s)
  child.failed = new Promise((_, reject) => {
    child.once("error", (e) => reject(new Error(`无法运行后端（${e.message}）`)))
    child.once("exit", (code) => {
      const last = child.said.trim().split(/\r?\n/).filter(Boolean).pop()
      reject(Object.assign(new Error(`后端进程退出，代码 ${code}${last ? `：${last.slice(0, 400)}` : ""}`), { said: !!last }))
    })
  })
  child.failed.catch(() => {}) // after start-up, exits are only logged
  return child
}

function isWin() { return process.platform === "win32" }

/** Where the backend downloads a new version's installer; cleared when the app starts (what is there was installed
 * or given up). */
function updateDir() { return path.join(app.getPath("temp"), "xingyan-updates") }

function stopBackend() {
  if (!backend || backend.exitCode !== null) return
  if (isWin()) spawnSync("taskkill", ["/pid", String(backend.pid), "/T", "/F"])
  else {
    try { process.kill(-backend.pid, "SIGTERM") } catch { backend.kill("SIGTERM") }
  }
}

function waitHealthy(port, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/api/health", timeout: 2000 }, (res) => {
        res.resume()
        if (res.statusCode === 200) resolve()
        else retry()
      })
      req.on("error", retry)
      req.on("timeout", () => { req.destroy(); retry() })
    }
    const retry = () => (Date.now() > deadline ? reject(new Error("backend did not become healthy")) : setTimeout(attempt, 300))
    attempt()
  })
}

async function createWindow() {
  const external = process.env.WB_BACKEND_URL // an already running backend (development)
  const port = external ? Number(new URL(external).port) : await freePort()
  const token = external ? process.env.WB_TOKEN : crypto.randomBytes(32).toString("hex")
  if (!external && !app.isPackaged && !process.env.WB_FRONTEND_URL) buildFrontend()
  if (!external) backend = startBackend(port, token)
  api = { port, token }
  await (backend ? Promise.race([waitHealthy(port), backend.failed]) : waitHealthy(port))
  const origin = `http://127.0.0.1:${port}`
  // the app needs no permission (camera, microphone, notifications, location …): a page asking gets none
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)
  await session.defaultSession.cookies.set({ url: origin, name: "wb_token", value: token, httpOnly: true, sameSite: "strict" })

  const win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1200, minHeight: 760, show: false, title: "形研",
    backgroundColor: "#0b0b0c",
    // no title bar of the system's: the app's top row is the title bar (its toolbars drag the window). macOS: the traffic
    // lights in the sidebar's top strip. Windows: the window's buttons drawn over the top row's right end, in the page's
    // colours and the row's height (theme.ts sends them on every change of look; a row there leaves them room) — no
    // white strip above a dark app; Electron's English menu bar (File Edit View …) stays out of sight
    ...(isMac ? { titleBarStyle: "hiddenInset", trafficLightPosition: { x: 16, y: 14 } }
      : { titleBarStyle: "hidden", titleBarOverlay: { color: "#0b0b0c", symbolColor: "#a1a1aa", height: 43 }, autoHideMenuBar: true }),
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true, nodeIntegration: false },
  })
  const appUrl = process.env.WB_FRONTEND_URL || `${origin}/`
  // links leave the app through the system browser; the app never navigates away
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url)
    return { action: "deny" }
  })
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(new URL(appUrl).origin)) e.preventDefault()
  })
  win.once("ready-to-show", () => win.show())
  // right-click: in a text box 剪切 / 复制 / 粘贴 / 全选, on selected text 复制 — Electron shows no menu of its own (a token
  // or a webhook pasted with the mouse, on Windows above all). A page's own menu (the K 线 page's) stops the event first
  win.webContents.on("context-menu", (_e, p) => {
    const items = p.isEditable
      ? [{ label: "剪切", role: "cut", enabled: p.editFlags.canCut }, { label: "复制", role: "copy", enabled: p.editFlags.canCopy },
          { label: "粘贴", role: "paste", enabled: p.editFlags.canPaste }, { type: "separator" }, { label: "全选", role: "selectAll" }]
      : p.selectionText.trim() ? [{ label: "复制", role: "copy" }] : []
    if (items.length) Menu.buildFromTemplate(items).popup({ window: win })
  })
  closeAfterSaving(win)
  await win.loadURL(appUrl)
  return win
}

/** A window closing (the app quitting, or its close button) first lets the page save what it still holds — a note
 * typed a moment ago (frontend lib/flush.ts) — for at most 2 s; the backend stops only after the windows are gone. */
function closeAfterSaving(win) {
  let saved = false
  win.on("close", (e) => {
    if (saved) return
    e.preventDefault()
    const flush = win.webContents.executeJavaScript("window.wbFlush ? window.wbFlush() : null", true).catch(() => {})
    void Promise.race([flush, new Promise((r) => setTimeout(r, 2000))]).finally(() => {
      saved = true
      if (!win.isDestroyed()) win.close()
    })
  })
}

/** One request of the main process's own to the backend, with the session token; its JSON, or its detail as an error. */
function backendCall(method, pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port: api.port, path: pathname, method, timeout: 30_000,
      headers: { "x-wb-token": api.token } }, (res) => {
      let body = ""
      res.setEncoding("utf8")
      res.on("data", (c) => { body += c })
      res.on("end", () => {
        let data = null
        try { data = JSON.parse(body) } catch { /* not JSON: said by the status */ }
        if (res.statusCode === 200 && data) resolve(data)
        else reject(new Error(typeof data?.detail === "string" ? data.detail : `后端返回 ${res.statusCode}`))
      })
    })
    req.on("error", reject)
    req.on("timeout", () => req.destroy(new Error("后端没有回答")))
    req.end()
  })
}

/** 下载并安装 (spec §3.3): wait for the backend's download (checked against the release's SHA256SUMS.txt), then close
 * the app — each window saves what it holds, the backend stops — and run the installer (will-quit). Resolves with what
 * to tell you when nothing is installed, or null once the app is closing to install. */
async function installUpdate() {
  if (!app.isPackaged) return "从仓库运行的开发版不自己安装：git pull 后重新打开就是新版本"
  let d = await backendCall("POST", "/api/update/download")
  while (d.state === "downloading") {
    await new Promise((r) => setTimeout(r, 1000))
    d = await backendCall("GET", "/api/update/download")
  }
  if (d.state !== "ready") return d.error ?? "安装包没下载好"
  const file = installerFor(d.version, updateDir(), process.platform, app.getVersion())
  if (!file || !fs.existsSync(file)) return "下载的安装包不对，没有安装"
  let bundle = null
  if (isMac) {
    bundle = bundleToReplace(process.execPath, (p) => {
      try { fs.accessSync(p, fs.constants.W_OK); return true } catch { return false }
    })
    if (!bundle) {
      await shell.openPath(file)
      return "这里的形研换不了（不在「应用程序」文件夹里，或者没有写入权限）：已打开新版本的安装包，把形研拖进「应用程序」替换旧的"
    }
  }
  toRun = { file, bundle }
  setTimeout(() => app.quit(), 1500)   // a moment to read 正在安装 before the window closes
  return null
}

/** Run the installer once the backend has gone (Windows: its files are about to be replaced; macOS: it never runs beside
 * the new app). Windows: the installer's update mode, which opens the app again; macOS: the swap, which waits for this
 * process to end. Resolves once it has started. */
async function runInstaller({ file, bundle }) {
  if (backend && backend.exitCode === null && backend.signalCode === null) {
    await new Promise((resolve) => { backend.once("exit", resolve); setTimeout(resolve, 15_000) })
  }
  const [cmd, args] = isWin() ? [file, WINDOWS_ARGS]
    : ["/bin/sh", ["-c", MAC_SWAP, "sh", String(process.pid), file, bundle, "open"]]
  await new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { detached: true, stdio: "ignore" })
    child.once("spawn", resolve)
    child.once("error", reject)
    child.unref()
  })
}

ipcMain.handle("wb:install-update", () => (installing ??= installUpdate().finally(() => { installing = null })))
/** Windows: the window's buttons in the page's background and muted text colour, as tall as its top row (px). */
ipcMain.handle("wb:title-bar", (e, color, symbolColor, height) => {
  const win = BrowserWindow.fromWebContents(e.sender)
  const hex = /^#[0-9a-f]{6}$/i
  if (isMac || !win || !hex.test(color) || !hex.test(symbolColor)) return
  try {
    win.setTitleBarOverlay({ color, symbolColor, height: Math.min(Math.max(Math.round(Number(height)) || 43, 32), 64) })
  } catch { /* a window without the overlay: its buttons as they are */ }
})
ipcMain.handle("wb:open-external", (_e, url) => {
  if (typeof url === "string" && /^https?:\/\//.test(url)) return shell.openExternal(url)
  return undefined
})
ipcMain.handle("wb:keep-awake", (_e, on) => {
  if (on && keepAwakeId === null) keepAwakeId = powerSaveBlocker.start("prevent-app-suspension")
  if (!on && keepAwakeId !== null) {
    powerSaveBlocker.stop(keepAwakeId)
    keepAwakeId = null
  }
  return keepAwakeId !== null
})

// what an update left: the installer may still be ending a moment after it opened the app again (Windows holds it till then)
app.whenReady().then(() => fs.rm(updateDir(), { recursive: true, force: true, maxRetries: 10, retryDelay: 1000 }, () => {}))
app.whenReady().then(() => createWindow().catch((e) => {
  console.error(e)
  stopBackend()
  dialog.showErrorBox("形研无法启动", `后端没有正常启动：${e.message}` + (e.said ? "\n\n上面是后端说的原因；解决不了就把这条信息发给维护者。"
    : app.isPackaged ? "\n\n请重新安装；还不行就把这条信息发给维护者。" : "\n\n请先在仓库的 backend 目录运行 uv sync，再重新打开。"))
  app.exit(1)
}))
app.on("before-quit", () => { app.isQuitting = true })
app.on("window-all-closed", () => app.quit())
app.on("will-quit", (e) => {   // after every window has closed (and saved what it held)
  stopBackend()
  if (!toRun) return
  e.preventDefault()   // the installer starts first, then the app ends
  const run = toRun
  toRun = null
  runInstaller(run).catch((err) => {   // e.g. Windows' 智能应用控制 refusing the unsigned installer: the app comes back
    dialog.showErrorBox("形研没能开始安装新版本", `${err.message}\n\n形研会重新打开，还是原来的版本。可以稍后再点「更新到」，` +
      "或者在发布页下载安装包手动安装；如果是 Windows 安全中心拦下的，它的「保护历史记录」里写着拦下了哪个文件。")
    app.relaunch()
  }).finally(() => app.exit(0))
})
process.on("exit", stopBackend)
process.stderr.on("error", () => {})   // a console that went away: the backend's words are still kept
