// 全选 over a list: the rows shown (after its filters) are what the header box picks; picked ones not shown stay picked.

/** Every row shown picked, or, all of them picked already, let go; the others picked stay. */
export function toggleAll(shown: string[], picked: Set<string>): Set<string> {
  const all = shown.length > 0 && shown.every((id) => picked.has(id))
  const next = new Set(picked)
  for (const id of shown) {
    if (all) next.delete(id)
    else next.add(id)
  }
  return next
}

/** How much of what is shown is picked: the header box checked, half (indeterminate), or empty. */
export function pickedOf(shown: string[], picked: Set<string>): "all" | "some" | "none" {
  const n = shown.filter((id) => picked.has(id)).length
  return n === 0 ? "none" : n === shown.length ? "all" : "some"
}
