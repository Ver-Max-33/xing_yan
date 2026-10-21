import { describe, expect, it } from "vitest"
import type { Bar } from "@/lib/api"
import { barFor, dateTs, isoDate, liveStep, lowerBound, minuteTicks, tsOf, reanchor, scaleAt, settled, toDisplayPoint, toKLine, toRawPoint } from "./data"
import { bucketOf, upTo } from "./data"

const bar = (date: string, close: number, scale: number): Bar =>
  ({ date, t: dateTs(date), open: close, high: close, low: close, close, volume: 100, amount: null, scale } as unknown as Bar)

// 1:2 bonus on 2026-09-29: qfq scale halves the history before it
const rows = [bar("2026-09-25", 10, 0.5), bar("2026-09-28", 10.2, 0.5), bar("2026-09-29", 5.1, 1), bar("2026-09-30", 5.2, 1)]
const scales: [string, number][] = [["2026-01-05", 0.5], ["2026-09-29", 1]] // daily change points from /api/bars

describe("chart data", () => {
  it("maps rows to KLineData with timestamps", () => {
    const k = toKLine(rows)
    expect(k[0].timestamp).toBe(Date.UTC(2026, 8, 25))
    expect(k[0].volume).toBe(100)
    expect(k[0].turnover).toBeUndefined()
  })
  it("dates round-trip", () => {
    expect(isoDate(dateTs("2026-09-30"))).toBe("2026-09-30")
  })
  it("lowerBound / barFor", () => {
    const k = toKLine(rows)
    expect(lowerBound(k, dateTs("2026-09-27"))).toBe(1)
    expect(barFor(k, dateTs("2026-09-27"))?.date).toBe("2026-09-28")
    expect(barFor(k, dateTs("2026-12-01"))?.date).toBe("2026-09-30")
  })
  it("anchors are stored as raw prices so an ex-date never moves them", () => {
    const k = toKLine(rows)
    // drawn at qfq 5.0 on 09-25 (scale 0.5) -> raw 10.0
    expect(toRawPoint({ timestamp: dateTs("2026-09-25"), value: 5.0 }, scales)).toEqual({ date: "2026-09-25", price: 10 })
    expect(toDisplayPoint(k, { date: "2026-09-25", price: 10 }, scales)).toEqual({ timestamp: dateTs("2026-09-25"), value: 5 })
    expect(toRawPoint({ timestamp: undefined, value: 1 }, scales)).toBeNull()
  })
  it("weekly bars: a daily anchor lands on its week's bar at its own day's price", () => {
    // the week bar is dated Fri 10-02 and carries that day's scale (1); the anchor is Mon 09-28 (0.5)
    const weeks = toKLine([bar("2026-09-25", 10, 0.5), bar("2026-10-02", 5.3, 1)])
    expect(toDisplayPoint(weeks, { date: "2026-09-28", price: 10.2 }, scales)).toEqual({ timestamp: dateTs("2026-10-02"), value: 5.1 })
  })
  it("an anchor older than the loaded bars keeps its own date", () => {
    const k = toKLine(rows)
    expect(toDisplayPoint(k, { date: "2020-01-06", price: 8 }, scales)).toEqual({ timestamp: dateTs("2020-01-06"), value: 4 })
  })
  it("scaleAt follows the change points", () => {
    expect(scaleAt(scales, "2026-09-28")).toBe(0.5)
    expect(scaleAt(scales, "2026-09-29")).toBe(1)
    expect(scaleAt(scales, "2019-01-01")).toBe(0.5) // before the first point: the first scale
    expect(scaleAt([], "2026-09-29")).toBe(1)
  })
  it("only moved points are re-anchored; untouched ones keep their stored anchor", () => {
    const stored = [{ date: "2026-09-28", price: 10.2 }, { date: "2026-09-30", price: 5.2 }]
    const shown = stored.map((p) => toDisplayPoint(toKLine([bar("2026-10-02", 5.3, 1)]), p, scales)) // both snap to Fri
    const moved = [shown[0], { timestamp: dateTs("2026-10-02"), value: 6 }]
    expect(reanchor(stored, shown, moved, scales)).toEqual([stored[0], { date: "2026-10-02", price: 6 }])
  })
})

describe("the forming bar each minute (spec §8.5)", () => {
  const ts = (d: string, temp = false) => ({ timestamp: dateTs(d), temp })
  it("puts in the bars from the chart's last one on: it again (a minute on, or the official one once published), then any after it", () => {
    expect(liveStep(dateTs("2026-10-02"), [ts("2026-10-01"), ts("2026-10-02", true)])).toEqual([ts("2026-10-02", true)])
    expect(liveStep(dateTs("2026-10-02"), [ts("2026-10-01"), ts("2026-10-02")])).toEqual([ts("2026-10-02")])   // published
    // the next day's first poll: yesterday's forming bar is replaced by the official one before today's goes in
    expect(liveStep(dateTs("2026-10-01"), [ts("2026-10-01"), ts("2026-10-02", true)])).toEqual([ts("2026-10-01"), ts("2026-10-02", true)])
  })
  it("leaves the forming bar out of what is final (a sample box never stands on it)", () => {
    expect(settled([ts("2026-10-01"), ts("2026-10-02", true)])).toEqual([ts("2026-10-01")])
  })
  it("reloads when the chart's last bar is not among them: a week's bar that took today's date, a day that has gone", () => {
    expect(liveStep(dateTs("2026-10-01"), [ts("2026-09-25"), ts("2026-10-02")])).toBe("reload")
    expect(liveStep(dateTs("2026-10-02"), [ts("2026-09-30"), ts("2026-10-01")])).toBe("reload")
    expect(liveStep(dateTs("2026-10-02"), [])).toBeNull()
  })
})

describe("minuteTicks", () => {
  it("drops a tick past the last bar or on the left edge, and says the day at each day's first tick shown", () => {
    const at = (iso: string) => Date.parse(`${iso}:00Z`)   // the exchange's clock read as UTC, as the chart's bars
    const ticks = [["2026-10-09T10:30", "10:30"], ["2026-10-09T13:05", "13:05"], ["2026-10-09T14:35", "14:35"], ["2026-10-12T09:35", "09:35"],
      ["2026-10-12T16:00", "16:00"]].map(([d, text], i) => ({ coord: 8 + 100 * i, value: at(d), text }))   // the first 8 px in: clipped
    expect(minuteTicks(ticks, at("2026-10-12T15:00")).map((x) => x.text)).toEqual(["10-09 13:05", "14:35", "10-12 09:35"])
  })
})

describe("tsOf", () => {
  it("reads a day as its midnight and a minute bar's end as that minute, both on the chart's clock (UTC)", () => {
    expect(tsOf("2026-10-12")).toBe(dateTs("2026-10-12"))
    expect(tsOf("2026-10-12T09:35")).toBe(Date.UTC(2026, 9, 12, 9, 35))
  })
})

describe("review 10-13: the 大盘 pane by week / month, no tick past the last bar", () => {
  const ts = (d: string) => Date.UTC(Number(d.slice(0, 4)), Number(d.slice(5, 7)) - 1, Number(d.slice(8, 10)))
  it("a stock's week dated Thursday meets the index's week dated Friday", () => {
    // CN:000016's last week before its suspension is dated 2026-09-03 (Thu); 深证成指's 2026-09-04 (Fri)
    expect(bucketOf(ts("2026-09-03"), "week")).toBe(bucketOf(ts("2026-09-04"), "week"))
    expect(bucketOf(ts("2026-09-04"), "week")).not.toBe(bucketOf(ts("2026-09-07"), "week"))
    expect(bucketOf(ts("2026-09-03"), "month")).toBe(bucketOf(ts("2026-09-30"), "month"))
    expect(bucketOf(ts("2026-09-30"), "month")).not.toBe(bucketOf(ts("2026-10-08"), "month"))
    expect(bucketOf(ts("2026-09-03"), "day")).toBe(ts("2026-09-03"))
  })
  it("the room after the last bar has no date", () => {
    const ticks = [{ value: ts("2026-10-09"), text: "10-09" }, { value: ts("2026-11-06"), text: "11-06" }]
    expect(upTo(ticks, ts("2026-10-12")).map((t) => t.text)).toEqual(["10-09"])
  })
})
