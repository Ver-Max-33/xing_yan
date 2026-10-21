// Per-viewer conveniences (period, panes, panel tab) kept in localStorage. Reads and writes are
// guarded: storage can be unavailable, and the UI must still work with defaults.
import { useCallback, useState } from "react"

const isObject = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x)

export function readPersisted<T>(key: string, initial: T): T {
  try {
    const raw = localStorage.getItem(`wb.${key}`)
    if (raw === null) return initial
    const parsed: unknown = JSON.parse(raw)
    // objects merge over the defaults so a newly added field gets its default value
    return isObject(initial) && isObject(parsed) ? ({ ...initial, ...parsed } as T) : (parsed as T)
  } catch {
    return initial
  }
}

/** What a page's hook keeps, written from another page (选股 opening the K 线 page on its list). */
export function writePersisted<T>(key: string, value: T): void {
  try {
    localStorage.setItem(`wb.${key}`, JSON.stringify(value))
  } catch {
    /* storage blocked: the page keeps its own value */
  }
}

export function usePersisted<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => readPersisted(key, initial))
  const set = useCallback((v: T) => {
    setValue(v)
    writePersisted(key, v)
  }, [key])
  return [value, set]
}
