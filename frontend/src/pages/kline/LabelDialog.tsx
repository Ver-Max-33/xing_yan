// Turn a rectangle into a sample box (spec §8.2 样例框, §5.10 标注集): is it a box or not, its class,
// scale, background and position, the touches on its edges and the extremes it leaves out (and why).
// Prices are those on the chart; the backend stores them as exchange prices on their own days.
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { Basis } from "@/lib/api"
import { BOX_CATEGORIES, type LabelDraft, NOT_BOX_CATEGORIES, ruleEndpoints } from "@/lib/rules"
import { candidates, type OHLC } from "./labelDraft"

interface Props {
  draft: LabelDraft | null
  bars: OHLC[]
  basis: Basis
  digits: number
  onClose: () => void
  onSaved: (id: string) => void
}

const SCALES: [NonNullable<LabelDraft["scale"]>, string][] = [["long", "大箱"], ["mid", "中箱"], ["short", "小箱"]]

export function LabelDialog({ draft, bars, basis, digits, onClose, onSaved }: Props) {
  // a redraw arrives with the sample's kind, category and scale; a new sample starts as a flat box
  const [isBox, setIsBox] = useState(draft?.is_box ?? true)
  const [category, setCategory] = useState(draft?.category ?? "flat")
  const [scale, setScale] = useState<LabelDraft["scale"]>(draft?.scale ?? null)
  const [context, setContext] = useState<LabelDraft["context"]>(draft?.context ?? null)
  const [position, setPosition] = useState<LabelDraft["position"]>(draft?.position ?? null)
  const [note, setNote] = useState("")
  const [off, setOff] = useState<Set<string>>(new Set()) // unticked candidates
  const [saving, setSaving] = useState(false)
  const cand = useMemo(() => (draft ? candidates(bars, draft) : { touches: [], exclusions: [] }), [draft, bars])
  if (!draft) return null
  const key = (k: string, d: string, x: string) => `${k}|${d}|${x}`
  const toggle = (k: string) => setOff((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n })
  const kinds = isBox ? BOX_CATEGORIES : NOT_BOX_CATEGORIES
  const fmt = (v: number) => v.toFixed(digits)
  const save = async () => {
    setSaving(true)
    try {
      const body: LabelDraft = {
        ...draft, is_box: isBox, category: category in kinds ? category : Object.keys(kinds)[0], scale, context, position, note,
        touches: cand.touches.filter((t) => !off.has(key("t", t.date, t.side))),
        exclusions: cand.exclusions.filter((e) => !off.has(key("e", e.date, e.kind))),
      }
      const r = await ruleEndpoints.addLabel(body, basis)
      toast("已存为样例")
      onSaved(r.id)
    } catch (e) {
      toast.error(`没存上：${String(e)}`)
    } finally {
      setSaving(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{draft.redraw_of ? "存为重画基线" : "设为样例框"}</DialogTitle>
          <DialogDescription className="num">
            {draft.start} → {draft.end} · 上沿 {fmt(draft.top)} · 下沿 {fmt(draft.bottom)} · 截止日 {draft.cutoff}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-[0.875rem]">
          <ToggleGroup type="single" value={isBox ? "box" : "not"} onValueChange={(v) => v && (setIsBox(v === "box"), setCategory(v === "box" ? "flat" : "trend_pause"))}
            variant="outline" size="sm">
            <ToggleGroupItem value="box" className="px-3">是箱体</ToggleGroupItem>
            <ToggleGroupItem value="not" className="px-3">不是箱体</ToggleGroupItem>
          </ToggleGroup>
          <label className="block">
            <span className="text-[0.8125rem] text-muted-foreground">类别</span>
            <select aria-label="类别" value={category} onChange={(e) => setCategory(e.target.value)}
              className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2">
              {Object.entries(kinds).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          {isBox && (
            <div className="grid grid-cols-3 gap-2">
              {([["级别", SCALES, scale, setScale], ["背景", [["上横", "上横"], ["下横", "下横"]], context, setContext],
                ["位置", [["低位", "低位"], ["中继", "中继"], ["高位", "高位"]], position, setPosition]] as const).map(([title, opts, v, set]) => (
                <label key={title} className="block">
                  <span className="text-[0.8125rem] text-muted-foreground">{title}</span>
                  <select aria-label={title} value={v ?? ""} onChange={(e) => (set as (x: string | null) => void)(e.target.value || null)}
                    className="mt-1 h-8 w-full rounded-md border border-input bg-background px-2">
                    <option value="">—</option>
                    {(opts as readonly (readonly [string, string])[]).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                  </select>
                </label>
              ))}
            </div>
          )}
          {isBox && (
            <fieldset>
              <legend className="text-[0.8125rem] text-muted-foreground">触点（上下沿附近的高低点，不对的去掉勾）</legend>
              <div className="mt-1 grid max-h-28 grid-cols-2 gap-x-3 overflow-y-auto">
                {cand.touches.length === 0 && <span className="text-[0.8125rem] text-subtle-foreground">框边附近没有高低点</span>}
                {cand.touches.map((t) => (
                  <label key={key("t", t.date, t.side)} className="num flex items-center gap-1.5 text-[0.8125rem]">
                    <input type="checkbox" checked={!off.has(key("t", t.date, t.side))} onChange={() => toggle(key("t", t.date, t.side))} />
                    {t.side === "top" ? "上沿" : "下沿"} {t.date} {fmt(t.price)}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          <fieldset>
            <legend className="text-[0.8125rem] text-muted-foreground">排除的极值和理由</legend>
            <div className="mt-1 space-y-0.5">
              {cand.exclusions.length === 0 && <span className="text-[0.8125rem] text-subtle-foreground">没有需要排除的极值</span>}
              {cand.exclusions.map((e) => (
                <label key={key("e", e.date, e.kind)} className="flex items-center gap-1.5 text-[0.8125rem]">
                  <input type="checkbox" checked={!off.has(key("e", e.date, e.kind))} onChange={() => toggle(key("e", e.date, e.kind))} />
                  <span className="num">{e.date} {fmt(e.price)}</span> · {e.kind} · <span className="text-muted-foreground">{e.reason}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <Textarea aria-label="备注" value={note} onChange={(e) => setNote(e.target.value)} placeholder="备注（可选）" className="min-h-14" />
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>只是画个框</Button>
          <Button onClick={save} disabled={saving}>存为样例</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
