import { describe, expect, it } from "vitest"
import { downloadLine, newerThan, type UpdateDownload, type UpdateInfo, updateChip, updateLine } from "./update"

const base: UpdateInfo = { current: "0.1.0", token: true, installable: true, latest: null, newer: false, url: null, name: null,
  published_at: null, notes: null, error: null, checked_at: "2026-10-08T09:30:00" }
const idle: UpdateDownload = { state: "idle", version: null, done: 0, total: 0, error: null }

describe("检查更新 in words", () => {
  it("says a newer release with its day, the latest already running, and why there is no answer", () => {
    expect(updateLine({ ...base, latest: "0.2.0", newer: true, published_at: "2026-10-09T08:00:00Z" }))
      .toEqual({ text: "有新版本 0.2.0（2026-10-09 发布）", tone: "new" })
    expect(updateLine({ ...base, latest: "0.1.0" })).toEqual({ text: "已是最新（GitHub 上最新的是 0.1.0）", tone: "ok" })
    expect(updateLine({ ...base, error: "GitHub 令牌无效或过期：在设置里换一个" })).toEqual({ text: "GitHub 令牌无效或过期：在设置里换一个", tone: "warn" })
    expect(updateLine({ ...base, token: false })).toEqual({ text: "还没配 GitHub 令牌：配好才能检查", tone: "muted" })
    expect(updateLine(undefined)).toEqual({ text: "检查中…", tone: "muted" })
  })
})

describe("下载并安装 in words", () => {
  const MB = 1048576
  it("says how far the download is, that it installs once it is here, and why it stopped", () => {
    expect(downloadLine(idle)).toBeNull()
    expect(downloadLine({ ...idle, state: "downloading", version: "0.2.1", done: 105 * MB, total: 285 * MB }))
      .toEqual({ text: "下载中 36%（105 / 285 MB）", tone: "muted" })
    expect(downloadLine({ ...idle, state: "downloading" })).toEqual({ text: "下载中…", tone: "muted" })
    expect(downloadLine({ ...idle, state: "ready", version: "0.2.1" })?.text).toContain("0.2.1 下载好了")
    expect(downloadLine({ ...idle, state: "error", error: "下载断了（ReadTimeout）：再点一次" }))
      .toEqual({ text: "下载断了（ReadTimeout）：再点一次", tone: "warn" })
  })

  it("the status bar shows the download while it runs, else that a new version is out", () => {
    const newer = { ...base, latest: "0.2.1", newer: true }
    expect(updateChip(newer, idle)).toBe("新版本 0.2.1")
    expect(updateChip(newer, { ...idle, state: "downloading", done: 1, total: 4 })).toBe("下载新版本 25%")
    expect(updateChip(newer, { ...idle, state: "ready" })).toBe("正在安装新版本…")
    expect(updateChip(newer, { ...idle, state: "error", error: "x" })).toBe("新版本 0.2.1")
    expect(updateChip(base, undefined)).toBeNull()
  })

  it("compares versions by number, not as text", () => {
    expect(newerThan("0.10.0", "0.9.9")).toBe(true)
    expect(newerThan("0.2.1", "0.2.1")).toBe(false)
    expect(newerThan("0.2.0", "0.2.1")).toBe(false)
  })
})
