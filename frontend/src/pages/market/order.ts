// The 市场 page's industries in order and in colour (redesign §2.4, §4), apart from the components that draw them.
import type { Industry } from "@/lib/market"

export type Window = "chg" | "rel5" | "rel20"
export type SortKey = "chg" | "rel5" | "rel20" | "limit_up"

/** The heat grid's tiles: by the chosen number, the strongest first; unknown last. */
export function byWindow(rows: Industry[], w: Window): Industry[] {
  return [...rows].sort((a, b) => (a[w] == null ? 1 : 0) - (b[w] == null ? 1 : 0) || (b[w] ?? 0) - (a[w] ?? 0) || a.name.localeCompare(b.name))
}

/** The full shade: the largest move shown. */
export const scaleOf = (rows: Industry[], w: Window) => Math.max(0, ...rows.map((r) => Math.abs(r[w] ?? 0)))

/** The ranking: the ranked by the chosen key (unknown last), then the small ones (three members or fewer, no rank). */
export function sortRows(rows: Industry[], key: SortKey): { ranked: Industry[]; small: Industry[] } {
  const v = (r: Industry) => (r[key] as number | null) ?? -Infinity
  const ranked = rows.filter((r) => !r.small).sort((a, b) => v(b) - v(a) || a.name.localeCompare(b.name))
  return { ranked, small: rows.filter((r) => r.small) }
}

/** A tag's colours: the strong ones red, the weak ones green (the swap setting flips both) — outlined on the page's own
 *  background: on the tinted fill the light theme's red was 4.2:1 (the layout check's contrast). */
export const tagTone = (t: Industry["tag"]) => (t === "一直强" || t === "转强" ? "text-up ring-1 ring-inset ring-up/45" : "text-down ring-1 ring-inset ring-down/45")
