// The AI layer's client (spec §10.1): which model each task uses, the budget, a test call and the call log.
// The keys themselves go through /api/secrets like every other key.
import { useQuery } from "@tanstack/react-query"
import { ApiError, api } from "./api"
import type { Understanding } from "./rules"

export type Provider = "deepseek" | "openrouter"
export type AITask = "rule_assistant" | "research"
/** How hard the model thinks before answering: default = the provider's own (DeepSeek: on, high); none = off. */
export type Effort = "default" | "none" | "low" | "high" | "max"
export interface AIConfig {
  tasks: Record<AITask, { provider: Provider; model: string; effort: Effort }>
  day_cap: number; month_cap: number; usd_cny: number
  // 元 per million tokens: the prompt as a cache miss (DeepSeek: peak; off-peak is half) and a hit, the output
  prices: Record<string, { input: number; cached?: number | null; output: number }>
}
export interface AIStatus {
  tasks: Record<AITask, { name: string; provider: Provider; model: string; key: string | null; ready: boolean; reason: string | null }>
  spent: { day: number; month: number }; day_cap: number; month_cap: number
  providers: Record<Provider, { name: string; key: string | null }>
}
export interface AICall {
  id: string; task: string; provider: string; model: string; ok: number; error: string | null
  tokens_in: number | null; tokens_out: number | null; cost: number | null; latency_ms: number | null; created_at: string
}

/** 规则助手 (spec §6.1–§6.4): an assumption the model made (its place in the rule and the options), a phrase it could
 *  not compute (with computable stand-ins), and a draft or an edit as it came back, checked. */
export interface Assumption { id: string; text: string; path: string; chosen: string; options: { value: string; label: string }[] }
export interface StandIn { label: string; stage: string; where: "at" | "during" | "all" | "any"; expr: string; id?: string }
export interface Unmapped { phrase: string; reason: string; proxies: StandIn[] }
export interface Draft {
  rule_text: string; markets: ("CN" | "JP")[]; understanding: Understanding; assumptions: Assumption[]; unmapped: Unmapped[]; note: string
  attempts: number; call_id: string; cost: number | null; model: string; latency_ms: number; diff?: string
}

/** The library's one-line box hands its text to the drafting page under this sessionStorage key. */
export const DRAFT_INPUT = "rule.draft.input"

const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) })
const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) })

export const aiEndpoints = {
  status: () => api<AIStatus>("/ai/status"),
  settings: () => api<AIConfig>("/ai/settings"),
  save: (c: AIConfig) => put<AIConfig>("/ai/settings", c),
  test: (task: AITask) => post<{ ok: boolean; model: string; latency_ms: number; cost: number | null }>("/ai/test", { task }),
  calls: (limit = 20) => api<AICall[]>(`/ai/calls?limit=${limit}`),
  // `signal`: 停止 — the page stops waiting; the model's call already under way still finishes (and is counted)
  draft: (text: string, signal?: AbortSignal) => api<Draft>("/assistant/draft", { method: "POST", body: JSON.stringify({ text }), signal }),
  edit: (rule_text: string, instruction: string, rule_id?: string, signal?: AbortSignal) =>
    api<Draft>("/assistant/edit", { method: "POST", body: JSON.stringify({ rule_text, instruction, rule_id }), signal }),
  apply: (text: string, changes: { path: string; value: string }[], add: StandIn[]) =>
    post<{ text: string; diff: string; understanding: Understanding }>("/assistant/apply", { text, changes, add }),
}

export const useAIStatus = () => useQuery({ queryKey: ["ai", "status"], queryFn: aiEndpoints.status })
export const useAIConfig = () => useQuery({ queryKey: ["ai", "settings"], queryFn: aiEndpoints.settings })
export const useAICalls = () => useQuery({ queryKey: ["ai", "calls"], queryFn: () => aiEndpoints.calls(20) })

/** A failed AI call: what went wrong and, when the answer failed its checks, the model's own text (§6.1: shown, never dropped). */
export interface AIFailed { message: string; raw: string | null }
export const failureOf = (e: unknown): AIFailed => ({
  message: String(e), raw: e instanceof ApiError ? (e.data as { raw?: string | null } | null)?.raw ?? null : null,
})
