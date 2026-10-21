import { describe, expect, it } from "vitest"
import { readText } from "./utils"

describe("a text file as Windows programs write it", () => {
  it("UTF-8 with or without a BOM, else GBK — what 通达信, 同花顺 and older Notepad save Chinese in", async () => {
    const utf8 = new TextEncoder().encode("主力:MA(C,5);")
    expect(await readText(new Blob([utf8]))).toBe("主力:MA(C,5);")
    expect(await readText(new Blob([new Uint8Array([0xef, 0xbb, 0xbf]), utf8]))).toBe("主力:MA(C,5);")
    expect(await readText(new Blob([new Uint8Array([0xd6, 0xf7, 0xc1, 0xa6, 0x3a, 0x43, 0x3b])]))).toBe("主力:C;")   // 主力:C; in GBK
  })
})
