// What the Electron preload exposes (spec §3.3: openExternal, setKeepAwake, installUpdate and setTitleBar only).
// In a plain browser (vite dev) these fall back to no-ops.
interface DesktopBridge {
  platform: string
  openExternal: (url: string) => void
  setKeepAwake: (on: boolean) => void
  installUpdate?: () => Promise<string | null>   // what it says when nothing is installed; null: the app closes to install
  setTitleBar?: (color: string, symbolColor: string, height: number) => Promise<void>   // Windows: the window's buttons
}

declare global {
  interface Window { wb?: DesktopBridge }
}

const bridge = typeof window !== "undefined" ? window.wb : undefined
const mac = bridge ? bridge.platform === "darwin" : typeof navigator !== "undefined" && /Mac/.test(navigator.userAgent)

export const desktop = {
  inElectron: !!bridge,
  isMac: bridge?.platform === "darwin",
  // Windows: the window has no title bar of its own; its buttons sit over the top row's right end (main.js titleBarOverlay)
  isWindows: bridge?.platform === "win32",
  mod: mac ? "⌘" : "Ctrl ",   // the shortcut's modifier as the keyboard says it: ⌘K on a Mac, Ctrl K on Windows
  openExternal: (url: string) => (bridge ? bridge.openExternal(url) : void window.open(url, "_blank", "noopener")),
  setKeepAwake: (on: boolean) => bridge?.setKeepAwake(on),
  setTitleBar: (color: string, symbolColor: string, height: number) => void bridge?.setTitleBar?.(color, symbolColor, height),
  installUpdate: (): Promise<string | null> =>
    bridge?.installUpdate ? bridge.installUpdate() : Promise.resolve("只有安装好的形研能自己更新"),
}
