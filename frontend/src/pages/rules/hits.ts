// Hits as the gallery, the change's effect and the version comparison count them.
import type { Market } from "@/lib/api"
import { codeOf, MARKET_NAME } from "@/lib/format"
import type { Compare, Hit } from "@/lib/rules"

export const hitKey = (h: { listing_id: string; signal_date: string }) => `${h.listing_id}|${h.signal_date}`

/** One comparison per market, added up. */
export function sum(parts: Compare[]): Compare {
  const pair = (k: "hits" | "right" | "wrong") => [0, 1].map((i) => parts.reduce((n, p) => n + p[k][i], 0)) as [number, number]
  const all = (k: "added" | "lost" | "right_lost" | "wrong_added") => parts.flatMap((p) => p[k])
  return { listings: parts.reduce((n, p) => n + p.listings, 0), kept: parts.reduce((n, p) => n + p.kept, 0), hits: pair("hits"),
    right: pair("right"), wrong: pair("wrong"), added: all("added"), lost: all("lost"), right_lost: all("right_lost"), wrong_added: all("wrong_added") }
}

/** The comparison on each of the rule's markets, added up. A market that cannot be compared (no data there yet) is left
 *  out and said — it took the others down with it, a toast and a blank panel; all of them failing is the first's error. */
export async function compareMarkets(markets: string[], run: (m: Market) => Promise<Compare>): Promise<{ data: Compare; skipped: string[] }> {
  const got = await Promise.allSettled(markets.map((m) => run(m as Market)))
  const ok = got.flatMap((g) => (g.status === "fulfilled" ? [g.value] : []))
  if (!ok.length) throw (got[0] as PromiseRejectedResult).reason
  const done = markets.filter((_, i) => got[i].status === "fulfilled").map((m) => MARKET_NAME[m as Market]).join("和")
  const why = (e: unknown) => (e instanceof Error ? e.message : String(e))
  const skipped = got.flatMap((g, i) => (g.status === "rejected"
    ? [`${MARKET_NAME[markets[i] as Market]}没有算进来（${why(g.reason)}），只比较了${/^[A-Za-z]/.test(done) ? " " : ""}${done}`] : []))
  return { data: sum(ok), skipped }
}

export const who = (h: Hit) => `${h.name ?? codeOf(h.listing_id)} ${codeOf(h.listing_id)} ${h.signal_date.slice(5)}`
