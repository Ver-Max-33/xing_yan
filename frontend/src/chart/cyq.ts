// 筹码分布 (spec §8.6) as 东方财富's chart computes it (quotechart2022 cyq.ts, checked number for number on their own bars):
// the 210 bars up to the day (their daily chart loads 210, lmt=210, and spreads all of them), 150 price steps from the
// lowest low to the highest high (at least 0.01 apart). Each day the chips held decay by its turnover, and its turnover
// is laid on [low, high] as a triangle peaking at (O+H+L+C)/4. 获利比例: the share of chips at or below the close.
// An estimate, as in every software: it assumes the day's trades spread like that.

export interface ChipBar { open: number; high: number; low: number; close: number; turnover_rate: number | null; scale?: number | null }
export interface ChipRange { low: number; high: number; concentration: number }
export interface Chips {
  low: number; step: number; prices: number[]; weights: number[]   // the distribution: weights[j] of the chips at prices[j]
  close: number; profit: number; avgCost: number; p90: ChipRange; p70: ChipRange
  bars: number; missing: number                                    // bars read; days among them without a turnover
}

export const CHIP_BARS = 210
export const CHIP_STEPS = 150

const r12 = (x: number) => Number(x.toPrecision(12))   // as they add the steps up

export function chips(bars: ChipBar[], index: number, lookback = CHIP_BARS, steps = CHIP_STEPS): Chips | null {
  // the day's own prices (the shown ÷ its scale), as 东方财富's chart had them that day and as the rules' CHIP_* read them:
  // the 0.01 step floor and a one-price day's weight depend on the prices, so a later dividend or the basis would move them
  const f = bars[index]?.scale || 1
  const k = bars.slice(Math.max(0, index - lookback + 1), index + 1)
    .map((b) => ({ ...b, open: b.open / f, high: b.high / f, low: b.low / f, close: b.close / f }))
  if (!k.length || k.every((b) => b.turnover_rate === null)) return null
  let hi = 0, lo = 0
  for (const b of k) {
    hi = hi ? Math.max(hi, b.high) : b.high
    lo = lo ? Math.min(lo, b.low) : b.low
  }
  const step = Math.max(0.01, (hi - lo) / (steps - 1))
  const prices = Array.from({ length: steps }, (_, i) => Number((lo + step * i).toFixed(2)))
  const w = new Array<number>(steps).fill(0)
  let missing = 0
  for (const b of k) {
    if (b.turnover_rate === null) missing++
    const t = Math.min(1, (b.turnover_rate ?? 0) / 100)
    const avg = (b.open + b.close + b.high + b.low) / 4
    const top = Math.floor((b.high - lo) / step), bottom = Math.ceil((b.low - lo) / step)
    const g = b.high === b.low ? steps - 1 : 2 / (b.high - b.low)
    for (let j = 0; j < steps; j++) w[j] *= 1 - t
    if (b.high === b.low) {   // a one-price day (一字板)
      w[Math.floor((avg - lo) / step)] += (g * t) / 2
      continue
    }
    for (let j = bottom; j <= top; j++) {
      const p = lo + step * j
      if (p <= avg) w[j] += Math.abs(avg - b.low) < 1e-8 ? g * t : ((p - b.low) / (avg - b.low)) * g * t
      else w[j] += Math.abs(b.high - avg) < 1e-8 ? g * t : ((b.high - p) / (b.high - avg)) * g * t
    }
  }
  const total = w.reduce((s, x) => s + r12(x), 0)
  /** the price below which `chip` of them lie */
  const cost = (chip: number) => {
    let sum = 0
    for (let i = 0; i < steps; i++) {
      const x = r12(w[i])
      if (sum + x > chip) return lo + i * step
      sum += x
    }
    return 0
  }
  const range = (p: number): ChipRange => {
    const [a, b] = [cost(total * (1 - p) / 2), cost(total * (1 + p) / 2)]
    return { low: a * f, high: b * f, concentration: a + b === 0 ? 0 : (b - a) / (a + b) }
  }
  const close = k[k.length - 1].close
  let below = 0
  for (let i = 0; i < steps; i++) if (close >= lo + i * step) below += r12(w[i])
  return {   // back in the shown prices
    low: lo * f, step: step * f, prices: f === 1 ? prices : prices.map((p) => p * f), weights: w, close: close * f,
    profit: total === 0 ? 0 : below / total, avgCost: cost(total * 0.5) * f, p90: range(0.9), p70: range(0.7), bars: k.length, missing,
  }
}
