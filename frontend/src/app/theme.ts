// Applies the appearance settings to <html>: "dark" class (dark is the default), "green-up" (绿涨红跌), the font
// ("font-geist" class; the system's otherwise) and the root font size every rem scales from. Everything else reads the
// CSS tokens; the charts read them again on "wb-theme".
import { useEffect, useState } from "react"
import { useSettings } from "@/lib/api"
import { desktop } from "./platform"

/** The appearance as last applied, kept here so the next start paints with it before the settings arrive. */
const KEPT = "wb.appearance"
interface Look { dark: boolean; greenUp: boolean; geist: boolean; size: number }

function apply(l: Look) {
  const root = document.documentElement
  root.classList.toggle("dark", l.dark)
  root.classList.toggle("green-up", l.greenUp)
  root.classList.toggle("font-geist", l.geist)
  root.style.fontSize = `${l.size}px`
  // Windows: the window's buttons over the top row, in the page's colours and as tall as the row (h-11: 2.75rem) less
  // its bottom line, which runs on beneath them
  const css = getComputedStyle(root)
  desktop.setTitleBar(css.getPropertyValue("--background").trim(), css.getPropertyValue("--muted-foreground").trim(), Math.round(l.size * 2.75) - 1)
}

/** Before the first paint (main.tsx): the look the app had when it was last open — no jump when the settings load. */
export function applyKept() {
  try {
    const l = JSON.parse(localStorage.getItem(KEPT) ?? "null") as Look | null
    if (l && typeof l.size === "number") apply(l)
  } catch { /* first start, or storage off: the defaults */ }
}

export function resolveDark(theme: string | undefined, prefersDark: boolean): boolean {
  if (theme === "light") return false
  if (theme === "system") return prefersDark
  return true
}

export function useAppearance() {
  const { data } = useSettings()
  const [prefersDark, setPrefersDark] = useState(() => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true)
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)")
    if (!mq) return
    const on = (e: MediaQueryListEvent) => setPrefersDark(e.matches)
    mq.addEventListener("change", on)
    return () => mq.removeEventListener("change", on)
  }, [])
  const dark = resolveDark(data?.theme, prefersDark)
  const greenUp = data?.colors === "green_up"
  const geist = data?.["ui.font"] === "geist"
  const size = data?.["ui.size"] ?? 16
  const loaded = !!data
  useEffect(() => {
    if (!loaded) return   // until the settings arrive, the look kept from the last start stands (applyKept)
    apply({ dark, greenUp, geist, size })
    try { localStorage.setItem(KEPT, JSON.stringify({ dark, greenUp, geist, size })) } catch { /* storage off */ }
    window.dispatchEvent(new Event("wb-theme"))
  }, [loaded, dark, greenUp, geist, size])
  return { dark, greenUp }
}
