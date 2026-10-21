// Saves still waiting (a note typed a moment ago): the page runs them when it is hidden or reloaded, and the desktop
// shell calls window.wbFlush() before a window closes and waits for it (desktop/main.js) — nothing typed is lost.
type Flush = () => Promise<unknown> | void

const pending = new Set<Flush>()

/** Register a save to run before the page goes; returns the unregister. */
export function onFlush(fn: Flush): () => void {
  pending.add(fn)
  return () => { pending.delete(fn) }
}

export function flushAll(): Promise<unknown> {
  return Promise.allSettled([...pending].map((f) => f()))
}

declare global { interface Window { wbFlush?: () => Promise<unknown> } }

if (typeof window !== "undefined") {
  window.wbFlush = flushAll
  window.addEventListener("pagehide", () => { void flushAll() })
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") void flushAll() })
}
