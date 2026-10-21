// 规则工作台 (spec §5.2, §6.3–§6.5; design 06, 07): write the rule (YAML) and try it — on one stock (单股调试),
// on a sample with your Y/N verdicts (样例图集), on a stock it should have caught (漏检排查), and against
// another version (版本对比). What you try runs from the text and sliders as they are; nothing is saved
// until you save a version. An unsaved draft stays on this computer.
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronRight, Copy, Save, Undo2 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { ParamValues } from "@/components/rules/ParamSliders"
import { useDataIn } from "@/lib/api"
import { marketOf } from "@/lib/format"
import { ruleEndpoints, type RuleRef, useRule } from "@/lib/rules"
import { usePersisted } from "@/lib/usePersisted"
import { CalibrateTab } from "./rules/CalibrateTab"
import { CompareTab } from "./rules/CompareTab"
import { DebugTab } from "./rules/DebugTab"
import { EditorTab } from "./rules/EditorTab"
import { type Focus, GalleryTab } from "./rules/GalleryTab"
import { MissedTab } from "./rules/MissedTab"
import { applyParams, copyText, NEW_RULE, ruleIdOf } from "./rules/ruleText"

type Tab = "rule" | "debug" | "gallery" | "missed" | "compare" | "calibrate"
const TABS: [Tab, string][] = [["rule", "规则"], ["debug", "单股调试"], ["gallery", "样例图集"], ["missed", "漏检排查"], ["compare", "版本对比"],
  ["calibrate", "校准"]]

const readDraft = (key: string) => { try { return localStorage.getItem(key) } catch { return null } }
const writeDraft = (key: string, text: string | null) => {
  try { if (text === null) localStorage.removeItem(key); else localStorage.setItem(key, text) } catch { /* no storage: the draft lives until you leave */ }
}

export default function RuleWorkspacePage() {
  const { ruleId = "new" } = useParams()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isNew = ruleId === "new"
  const docQ = useRule(isNew ? undefined : ruleId)
  const doc = docQ.data
  const draftKey = `wb.rule.draft.${ruleId}`
  const [text, setText] = useState<string | null>(null)
  const [params, setParams] = useState<ParamValues>({})
  // the sliders move at once; what runs follows them once they rest for a moment
  const [settled, setSettled] = useState<ParamValues>(params)
  const [tab, setTab] = useState<Tab>(isNew ? "rule" : "debug")
  const [picked, setListingId] = usePersisted<string | null>("rules.listing", null)
  const [lastK] = usePersisted<string>("kline.last", "")
  // the stock you looked at last, until you pick one here — not one whose market has no data now (日股 switched to an
  // empty profile): its preview could only say so, as an error, on opening any rule
  const dataIn = useDataIn()
  const usable = (id: string | null) => (id && dataIn.has(marketOf(id)) ? id : null)
  const listingId = usable(picked) ?? usable(lastK || null)
  const [seed, setSeed] = usePersisted<number>("rules.seed", 0)
  const [focus, setFocus] = useState<Focus | null>(null)         // hits to look at in the gallery (from 这次改动的影响)
  const [pick, setPick] = useState<{ listing: string; day: string } | null>(null) // a day to open 单股调试 on
  const openDay = (id: string, day: string) => { setListingId(id); setPick({ listing: id, day }); setTab("debug") }

  // the text to start from: what you imported or copied, your unsaved draft, or the saved version
  const [loaded, setLoaded] = useState<string | null>(null)
  if (loaded !== ruleId && (isNew || doc)) {
    setLoaded(ruleId)
    let start = readDraft(draftKey) ?? doc?.text ?? NEW_RULE
    if (isNew && search.get("from")) {
      start = sessionStorage.getItem("rule.import") ?? start
      sessionStorage.removeItem("rule.import")
    }
    setText(start)
    setParams({})
    setSettled({})   // with them: another rule never runs once with the last rule's slider values (they lag 150 ms)
    setTab(isNew || search.get("tab") === "rule" ? "rule" : "debug")   // ?tab=rule: a change brought here to look at (条件体检's filter)
    setFocus(null) // the previous rule's new hits and opened day are not this rule's
    setPick(null)
  }
  const saved = doc?.text ?? null
  const dirty = text !== null && text !== saved
  useEffect(() => {
    // only a text that belongs to this rule is its draft (right after you open another rule, the old text is still here)
    if (text === null || loaded !== ruleId || doc?.builtin) return
    writeDraft(draftKey, text !== saved ? text : null)
  }, [text, saved, draftKey, doc?.builtin, loaded, ruleId])

  // the text is checked as you type; the rule runs only while it is valid
  const [checked, setChecked] = useState<string | null>(null)
  useEffect(() => {
    const id = window.setTimeout(() => setChecked(text), 400)
    return () => window.clearTimeout(id)
  }, [text])
  const checkQ = useQuery({ queryKey: ["check", checked], queryFn: () => ruleEndpoints.check(checked!), enabled: checked !== null })
  const valid = checkQ.data?.ok === true && checked === text
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(params), 150)
    return () => window.clearTimeout(id)
  }, [params])
  const savedRef: RuleRef | null = useMemo(() => (doc ? { rule_id: doc.id, version: doc.version } : null), [doc])
  const ruleRef: RuleRef | null = useMemo(() => {
    if (loaded !== ruleId) return null   // another rule opened: its text is not here yet, the last one's still is
    if (!dirty && savedRef) return Object.keys(settled).length ? { ...savedRef, params: settled } : savedRef
    return valid && text !== null ? { text, params: settled } : null
  }, [loaded, ruleId, dirty, savedRef, settled, valid, text])
  const info = (checked === text ? checkQ.data?.rule : undefined) ?? doc?.rule ?? null
  const markets = info?.markets ?? ["CN"]
  const name = info?.name ?? ruleId

  const save = async () => {
    if (text === null) return
    let body = text
    if (Object.keys(params).length) {
      const r = applyParams(text, params)
      if (r.missing.length) {
        toast.error(`这些参数在规则文字里没找到，请在「规则」里手动改：${r.missing.join("、")}`)
        return
      }
      body = r.text
    }
    const id = ruleIdOf(body)
    try {
      const r = await ruleEndpoints.save(body, isNew || id !== ruleId) // a new id is a new rule, never another rule's next version
      writeDraft(draftKey, null)
      await qc.invalidateQueries({ queryKey: ["rules"] })
      await qc.invalidateQueries({ queryKey: ["rule", r.id] })
      toast(r.new ? `已保存为 ${r.id} v${r.version}` : "和上一个版本一样，没有新版本")
      setParams({})
      if (r.id !== ruleId) navigate(`/rules/${r.id}`, { replace: true })
      else setText(body)
    } catch (e) {
      toast.error(`没保存：${String(e)}`)
    }
  }
  const copy = () => {
    if (text === null) return
    const r = applyParams(text, params) // the values you tuned go with the copy
    if (r.missing.length) {
      toast.error(`这些参数在规则文字里没找到：${r.missing.join("、")}`)
      return
    }
    sessionStorage.setItem("rule.import", copyText(r.text))
    navigate("/rules/new?from=copy")
  }

  if (!isNew && docQ.error) {
    return <div className="flex h-full items-center justify-center text-[0.875rem] text-subtle-foreground">读不到规则 {ruleId}：{String(docQ.error)}</div>
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="drag clear-window-buttons flex h-11 shrink-0 items-center gap-2 border-b border-border px-4">
        <Link to="/rules" className="no-drag text-[0.875rem] text-muted-foreground hover:text-foreground">规则库</Link>
        <ChevronRight className="size-3.5 text-subtle-foreground" />
        <h1 className="no-drag truncate text-[0.9375rem] font-semibold">{name}</h1>
        {doc?.builtin ? <Badge variant="outline" className="h-5 px-1.5 text-[0.75rem] font-normal">内置 · 只读</Badge>
          : <Badge variant="outline" className="num h-5 px-1.5 text-[0.75rem] font-normal">{isNew ? "新规则" : `v${doc?.version ?? ""}`}{(dirty && !isNew) || Object.keys(params).length ? " · 有改动" : ""}</Badge>}
        <span className="flex-1" />
        <div className="no-drag flex items-center gap-2">
          {(dirty && !isNew) || Object.keys(params).length ? (
            <Button variant="ghost" size="sm" onClick={() => { setText(saved ?? NEW_RULE); setParams({}) }}><Undo2 className="size-4" />撤销改动</Button>
          ) : null}
          {doc?.builtin ? <Button size="sm" onClick={copy}><Copy className="size-4" />复制为新规则</Button> : (
            <Button size="sm" disabled={!valid || (!dirty && !Object.keys(params).length && !isNew)} onClick={() => void save()}>
              <Save className="size-4" />{isNew ? "保存" : `保存为 v${(doc?.version ?? 0) + 1}`}
            </Button>
          )}
        </div>
      </header>
      <div className="flex h-10 shrink-0 items-center border-b border-border px-3">
        {loaded === ruleId && ( // a tab picked before the rule has loaded would be reset by the loading
          <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
            <TabsList>{TABS.map(([k, l]) => <TabsTrigger key={k} value={k}>{l}</TabsTrigger>)}</TabsList>
          </Tabs>
        )}
        {!valid && text !== null && checked === text && checkQ.data && <span className="ml-3 text-[0.8125rem] text-error">规则有错误，先在「规则」里改好</span>}
      </div>
      {text === null || loaded !== ruleId ? <div className="p-6 text-[0.875rem] text-subtle-foreground">…</div> : ( // never the previous rule's text
        <div className="flex min-h-0 flex-1">
          {tab === "rule" && <EditorTab text={text} onText={setText} readOnly={!!doc?.builtin} ruleId={isNew ? undefined : ruleId} check={checked === text ? checkQ.data ?? null : null}
            versions={doc?.versions ?? []} />}
          {tab === "debug" && <DebugTab key={ruleId} ruleRef={ruleRef} savedRef={savedRef} params={params} onParams={setParams} ruleParams={info?.params ?? null} ruleStages={info?.stages} ruleNames={info?.names} listingId={listingId}
            onListing={setListingId} markets={markets} sampleSeed={seed} onShowHits={(f) => { setFocus(f); setTab("gallery") }} pick={pick} />}
          {tab === "gallery" && <GalleryTab ruleRef={ruleRef} ruleId={ruleIdOf(text) ?? ruleId} version={dirty ? null : doc?.version ?? null}
            markets={markets} seed={seed} onSeed={setSeed} focus={focus} onFocus={setFocus} onOpen={openDay} onMissed={() => setTab("missed")} />}
          {tab === "missed" && <MissedTab ruleRef={ruleRef} listingId={listingId} onListing={setListingId} seed={seed} markets={markets} onOpen={openDay} />}
          {tab === "calibrate" && <CalibrateTab ruleRef={ruleRef} ruleId={ruleIdOf(text) ?? ruleId} info={info} seed={seed} onUse={(v) => {
            const base = Object.fromEntries((info?.params ?? []).map((p) => [p.id, p.value])) // the sliders hold only what differs
            setParams(Object.fromEntries(Object.entries(v).filter(([k, x]) => x !== base[k])))
            setTab("debug")
          }} />}
          {tab === "compare" && <CompareTab ruleId={ruleId} doc={doc ?? null} current={ruleRef} dirty={dirty || Object.keys(params).length > 0}
            markets={markets} seed={seed} onOpen={openDay} />}
        </div>
      )}
    </div>
  )
}
