// 手画对照 (spec §6.3): how the algorithm agrees with each of your samples on a stock, for their tags on the
// chart. One agreement run over the whole label set (the labels page shares it), only when there are samples.
import { useQuery } from "@tanstack/react-query"
import { useMemo } from "react"
import { labelNote } from "@/chart/structures"
import { type Label, ruleEndpoints } from "@/lib/rules"

export function useLabelNotes(labels: Label[]): Record<string, string> {
  const agreeQ = useQuery({ queryKey: ["agreement", "all"], queryFn: () => ruleEndpoints.agreement(null), enabled: labels.length > 0, staleTime: 5 * 60_000 })
  return useMemo(() => {
    const mine = new Set(labels.map((l) => l.id))
    return Object.fromEntries((agreeQ.data?.labels ?? []).filter((r) => mine.has(r.id)).map((r) => [r.id, labelNote(r)]))
  }, [agreeQ.data, labels])
}
