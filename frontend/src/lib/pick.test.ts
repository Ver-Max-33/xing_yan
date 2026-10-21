import { describe, expect, it } from "vitest"
import { pickedOf, toggleAll } from "./pick"

describe("全选 over the rows shown", () => {
  it("picks every row shown, or, all picked, lets them go — the ones picked elsewhere stay", () => {
    const shown = ["a", "b", "c"]
    expect([...toggleAll(shown, new Set(["x"]))].sort()).toEqual(["a", "b", "c", "x"])
    expect([...toggleAll(shown, new Set(["a", "b", "c", "x"]))]).toEqual(["x"])
    expect([...toggleAll(shown, new Set(["a"]))].sort()).toEqual(["a", "b", "c"])   // some picked: all of them
    expect([...toggleAll([], new Set(["x"]))]).toEqual(["x"])
  })
  it("says how much of what is shown is picked, for the header box", () => {
    expect(pickedOf(["a", "b"], new Set())).toBe("none")
    expect(pickedOf(["a", "b"], new Set(["a", "x"]))).toBe("some")
    expect(pickedOf(["a", "b"], new Set(["a", "b"]))).toBe("all")
    expect(pickedOf([], new Set(["a"]))).toBe("none")
  })
})
