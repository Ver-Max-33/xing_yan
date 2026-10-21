// Reading a typed number (components/NumberField): the value it stands for, or why it is not one — never a clamped
// or default value in its place.
export interface NumberRule {
  int?: boolean
  min?: number            // inclusive bounds, in the units shown (a percent field: percent)
  max?: number
  above?: number          // exclusive bounds
  below?: number
  optional?: boolean      // empty means "none" (关 / 不限 / the default) instead of an error
  scale?: number          // shown = stored × scale (100 for a share shown in %)
}

const fmt = (v: number) => String(v)

/** What a typed text means under a rule: the stored value, or why it is not one. */
export function parseNumber(text: string, r: NumberRule): { value: number | null; error: string | null } {
  const t = text.trim()
  if (t === "") return r.optional ? { value: null, error: null } : { value: null, error: "要填一个数" }
  const n = Number(t)
  if (!Number.isFinite(n)) return { value: null, error: "不是一个数" }
  if (r.int && !Number.isInteger(n)) return { value: null, error: "要填整数" }
  if ((r.min != null && n < r.min) || (r.max != null && n > r.max)) {
    return { value: null, error: r.min != null && r.max != null ? `要在 ${fmt(r.min)} 到 ${fmt(r.max)} 之间`
      : r.min != null ? `不能小于 ${fmt(r.min)}` : `不能大于 ${fmt(r.max!)}` }
  }
  if (r.above != null && n <= r.above) return { value: null, error: `要大于 ${fmt(r.above)}` }
  if (r.below != null && n >= r.below) return { value: null, error: `要小于 ${fmt(r.below)}` }
  return { value: r.scale ? n / r.scale : n, error: null }
}

/** How a stored value reads in the field. */
export function showNumber(v: number | null | undefined, r: NumberRule): string {
  if (v == null) return ""
  return String(r.scale ? Math.round(v * r.scale * 1e6) / 1e6 : v)
}
