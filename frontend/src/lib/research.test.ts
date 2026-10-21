import { describe, expect, it } from "vitest"
import { diffLine, factGroups, noteText, outcomeText, type Fact, type Research } from "./research"

const fact = (id: string, text: string): Fact => ({ id, kind: id.split(".")[0], source: "tushare", as_of: "2026-10-02", text, value: null })
const item = (text: string, inference = false) => ({ text, refs: ["px.close"], inference })
const rec = (over: Partial<Research> = {}): Research => ({
  id: "r1", listing_id: "CN:600519", day: "2026-10-02", profile: "test", demo: false, trigger: "manual", pack_hash: "h",
  pack: { listing_id: "CN:600519", name: "贵州茅台", market: "CN", day: "2026-10-02", facts: [], left_out: [], hash: "h" },
  output: { summary: "大箱里整理。", support: [item("靠近上沿")], against: [item("净利下滑", true)], to_verify: ["分红"],
            invalidation: [item("跌破下沿")], next_review: ["越过上沿时"], lean: "支持", continuity: { verdict: "首次", text: "", refs: [] } },
  status: "ok", flags: [], call_id: "c", model: "deepseek-chat", cost: 0.01, created_at: "2026-10-02T20:00:00+08:00",
  entry_date: null, settled_at: null, outcome: null, return20: null, excess20: null, ...over,
})

describe("个股研究", () => {
  it("groups the evidence the way you read it, leaving out what is not there", () => {
    const g = factGroups([fact("fin.2026H1", "半年报"), fact("px.close", "收盘"), fact("box.long", "大箱"), fact("note.reason", "理由"),
                          fact("ann.7", "公告：回购"), fact("doc.7", "《回购》摘录：…"), fact("ir.ab12", "你导入的资料《纪要》第 1 页：…")])
    expect(g.map((x) => [x.label, x.facts.map((f) => f.id)])).toEqual(
      [["行情", ["px.close"]], ["结构", ["box.long"]], ["财报", ["fin.2026H1"]], ["公告", ["ann.7", "doc.7"]],
       ["你导入的资料", ["ir.ab12"]], ["你的笔记", ["note.reason"]]])
  })
  it("says in a line what changed since the last research", () => {
    expect(diffLine({ added: [fact("fin.2026Q3", "2026 三季报（2026-10-28 公告，累计）：营收 1,200 亿元"),
                              fact("ann.9", "2026-10-02 公告：关于回购股份的公告"), fact("doc.9", "《关于回购股份的公告》摘录：公司拟回购")],
                      removed: [fact("ann.1", "回购")],
                      changed: [{ id: "px.close", before: "收盘 10", after: "收盘 11" }] }))
      .toBe("新增 3 条：2026 三季报、关于回购股份的公告、关于回购股份的公告；少了 1 条；变了 1 条")
    expect(diffLine({ added: [], removed: [], changed: [] })).toBe("证据没有变化")
  })
  it("takes the research to your notes with its date, its lean and inferences marked", () => {
    expect(noteText(rec())).toBe("AI 研究 2026-10-02（deepseek-chat，倾向支持）：大箱里整理。\n支持：靠近上沿\n反对：（推断）净利下滑\n失效条件：跌破下沿")
  })
  it("says what followed a research once its 20 days have passed", () => {
    expect(outcomeText(rec())).toBe("待结算")
    expect(outcomeText(rec({ outcome: "settled", excess20: 0.0123 }))).toBe("+1.23%")
    expect(outcomeText(rec({ outcome: "gone" }))).toBe("停止交易")
    expect(outcomeText(rec({ outcome: "halt" }))).toBe("首日没成交")
    expect(outcomeText(rec({ demo: true }))).toBe("演示数据不结算")
  })
})
