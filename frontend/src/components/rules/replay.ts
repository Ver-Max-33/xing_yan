// The replay timeline's arithmetic over a listing's trading days.

/** Index of the last trading day on or before `day`. */
export function dayIndex(dates: string[], day: string | null): number {
  if (day === null) return dates.length - 1
  let lo = 0, hi = dates.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (dates[mid] <= day) lo = mid + 1
    else hi = mid
  }
  return Math.max(0, lo - 1)
}
