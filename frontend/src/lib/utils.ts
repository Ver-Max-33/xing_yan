export { cn } from "cn"

/** A text file as either system writes it: UTF-8 (a BOM or none), else GBK — what 通达信, 同花顺 and older Windows
 *  programs save Chinese in (read as UTF-8, it would come out garbled). */
export async function readText(f: Blob): Promise<string> {
  const bytes = new Uint8Array(await f.arrayBuffer())
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes)
  } catch {
    return new TextDecoder("gbk").decode(bytes)
  }
}
