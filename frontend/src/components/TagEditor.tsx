// 题材 tags on a stock (spec §7.3): typed here, kept per stock; rules read them (TAGGED("锂电"), universe.tags) and
// the 选股 table shows them. Tags already used elsewhere are offered as you type.
import { useQueryClient } from "@tanstack/react-query"
import { X } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { marketEndpoints, useAllTags, useTags } from "@/lib/market"

export function TagEditor({ listingId }: { listingId: string }) {
  const qc = useQueryClient()
  const { data: tags = [] } = useTags(listingId)
  const { data: all = [] } = useAllTags()
  const [text, setText] = useState("")
  const save = async (next: string[]) => {
    try {
      qc.setQueryData(["tags", listingId], await marketEndpoints.setTags(listingId, next))
      void qc.invalidateQueries({ queryKey: ["tags"], exact: true })
    } catch (e) {
      toast.error(String(e))
    }
  }
  const add = () => {
    const t = text.trim()
    if (t && !tags.includes(t)) void save([...tags, t])
    setText("")
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 pb-2" data-testid="tag-editor">
      <span className="text-[0.75rem] text-subtle-foreground">题材</span>
      {tags.map((t) => (
        <span key={t} className="flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[0.8125rem]">
          {t}
          <button type="button" aria-label={`去掉 ${t}`} className="text-subtle-foreground hover:text-foreground"
            onClick={() => void save(tags.filter((x) => x !== t))}><X className="size-3" /></button>
        </span>
      ))}
      <input aria-label="加题材标签" list="tag-choices" value={text} onChange={(e) => setText(e.target.value)} placeholder="＋ 题材"
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add() } }}
        className="h-6 w-20 rounded border border-transparent bg-transparent px-1 text-[0.8125rem] outline-none placeholder:text-subtle-foreground focus:border-ring" />
      <datalist id="tag-choices">{all.filter((x) => !tags.includes(x.tag)).map((x) => <option key={x.tag} value={x.tag} />)}</datalist>
    </div>
  )
}
