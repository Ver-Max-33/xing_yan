// 检查更新 (spec §3.3): the newest release published on GitHub against the version running (backend wb/update.py) —
// read with your GitHub token, kept 6 hours. 下载并安装 (1.51): in the installed app, 更新到 x.y.z downloads that
// release's installer, checks it against the release's SHA256SUMS.txt, closes the app and installs it; Windows opens
// the app again when it is done (desktop main.js, update.js).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { toast } from "sonner"
import { desktop } from "@/app/platform"
import { api } from "./api"

export interface UpdateInfo {
  current: string; token: boolean   // whether a token is set (never the token)
  installable: boolean              // the installed app (not one run from the repository): it installs a new version itself
  latest: string | null; newer: boolean; url: string | null; name: string | null; published_at: string | null
  notes: string | null; error: string | null; checked_at: string
}

export interface UpdateDownload {
  state: "idle" | "downloading" | "ready" | "error"
  version: string | null; done: number; total: number; error: string | null   // done, total: bytes
}

export const checkUpdate = (force = false) => api<UpdateInfo>(`/update${force ? "?force=true" : ""}`)
const SIX_HOURS = 6 * 3600_000
export const useUpdate = () =>
  useQuery({ queryKey: ["update"], queryFn: () => checkUpdate(), staleTime: SIX_HOURS, refetchInterval: SIX_HOURS, refetchOnWindowFocus: false })

/** The download of a new version's installer, asked every second while it runs. */
export const useUpdateDownload = () =>
  useQuery({ queryKey: ["update-download"], queryFn: () => api<UpdateDownload>("/update/download"),
    refetchInterval: (q) => (q.state.data?.state === "downloading" ? 1000 : false) })

/** 更新到 x.y.z: the download starts (its progress shows wherever it is watched); the main process waits for it, then
 * closes the app and installs. What it says when nothing is installed (the download failed …) is shown. */
export function useInstallUpdate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      qc.setQueryData(["update-download"], await api<UpdateDownload>("/update/download", { method: "POST" }))
      return desktop.installUpdate()
    },
    onSuccess: (said) => toast(said ?? "正在关闭形研、安装新版本，装好后会自动重新打开"),
    onError: (e) => toast.error(`更新没成功：${e instanceof Error ? e.message : String(e)}`),
  })
}

/** What a check came to, in words: a newer release and its day, the latest running already, or why there is no answer. */
export function updateLine(u: UpdateInfo | undefined): { text: string; tone: "new" | "ok" | "warn" | "muted" } {
  if (!u) return { text: "检查中…", tone: "muted" }
  if (!u.token) return { text: "还没配 GitHub 令牌：配好才能检查", tone: "muted" }
  if (u.error) return { text: u.error, tone: "warn" }
  if (u.newer) return { text: `有新版本 ${u.latest}${u.published_at ? `（${u.published_at.slice(0, 10)} 发布）` : ""}`, tone: "new" }
  return { text: `已是最新（GitHub 上最新的是 ${u.latest}）`, tone: "ok" }
}

const percent = (d: UpdateDownload) => (d.total ? Math.floor((d.done / d.total) * 100) : 0)

/** How far a download is, in words: 下载中 37%（105 / 285 MB）, installing, or why it stopped; nothing when idle. */
export function downloadLine(d: UpdateDownload | undefined): { text: string; tone: "muted" | "new" | "warn" } | null {
  if (!d || d.state === "idle") return null
  if (d.state === "error") return { text: d.error ?? "下载出错", tone: "warn" }
  if (d.state === "ready") return { text: `${d.version} 下载好了：正在关闭形研、安装，装好后自动重新打开`, tone: "new" }
  const mb = (n: number) => Math.round(n / 1048576)
  return { text: d.total ? `下载中 ${percent(d)}%（${mb(d.done)} / ${mb(d.total)} MB）` : "下载中…", tone: "muted" }
}

/** The status bar's word on a new version: how far its download is while it runs, else that there is one. */
export function updateChip(u: UpdateInfo | undefined, d: UpdateDownload | undefined): string | null {
  if (d?.state === "downloading") return `下载新版本 ${percent(d)}%`
  if (d?.state === "ready") return "正在安装新版本…"
  return u?.newer ? `新版本 ${u.latest}` : null
}

const parts = (v: string) => v.split(".").map(Number)
/** a newer than b, both 1.2.3 */
export function newerThan(a: string, b: string): boolean {
  const [x, y] = [parts(a), parts(b)]
  return (x[0] - y[0] || x[1] - y[1] || x[2] - y[2]) > 0
}

/** Once, when the app first runs a newer version than last time: 形研已更新到 x.y.z (the version seen is kept in this
 * app's own storage). */
export function useUpdatedNotice(current: string | undefined) {
  useEffect(() => {
    if (!current) return
    try {
      const seen = localStorage.getItem("wb.version")
      if (seen && newerThan(current, seen)) toast.success(`形研已更新到 ${current}`)
      localStorage.setItem("wb.version", current)
    } catch { /* no storage: no notice */ }
  }, [current])
}
