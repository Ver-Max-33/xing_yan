"""The LLM client (spec §10.1): OpenRouter and DeepSeek through their OpenAI-compatible chat/completions endpoint,
with the httpx the data layer already uses — one endpoint does not need the SDK and its dependencies.

- Structured output: OpenRouter `json_schema` (strict, and `provider.require_parameters` so it only routes to
  providers that honour it); DeepSeek `json_object` (the prompt must say "json"). Either way the answer goes
  through the caller's own check; an answer that fails it gets one more try with the reason, then the raw
  answer comes back with the error — never silently dropped.
- Each task (the rule assistant, stock research) has its own provider, model and thinking strength (DeepSeek's
  `thinking` + `reasoning_effort`, OpenRouter's `reasoning.effort`); the key is read where every key is (env, .env,
  keychain) and never stored or logged here.
- DeepSeek since V4 (api-docs.deepseek.com, 2026-10): `deepseek-flash` and `deepseek-v4-pro`, thinking on by default
  (strength high); `deepseek-chat` / `deepseek-reasoner` stopped on 2026-07-24 and a config saved with them is moved
  to the current models. Its prices: a cache hit, a cache miss and the output, each half off-peak (deepseek_peak).
- Every call is logged: task, model, the prompt template's and the inputs' hashes (so an answer can be replayed
  when it has to be reproduced), what came back, tokens, cost (元) and time. A day and a month budget stop AI
  before they are spent; without a key, over budget or offline, AI is unavailable and nothing else is affected."""
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect
from wb.secrets import get_secret, secret_source

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "secret": "deepseek_api_key",
                 "structured": "json_object"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "secret": "openrouter_api_key",
                   "structured": "json_schema"},
}
TASKS = {"rule_assistant": "规则助手", "research": "个股研究"}
RETRY_STATUS = {429, 500, 502, 503, 504}
BEIJING = ZoneInfo("Asia/Shanghai")
# stopped 2026-07-24 (DeepSeek 更新日志, 2026-04-24), each moved to what DeepSeek itself pointed it at while the names
# were phased out: chat → flash without thinking, reasoner → flash thinking — never to a dearer model
RETIRED = {"deepseek-chat": ("deepseek-flash", "none"), "deepseek-reasoner": ("deepseek-flash", "high")}
# the rule assistant drafts in the rule language and is worth the stronger model; a research summarises a pack
DEFAULT_TASKS = {"rule_assistant": ("deepseek-v4-pro", "high"), "research": ("deepseek-flash", "high")}


def deepseek_peak(t: datetime, holiday: Callable[[date], bool] | None = None) -> bool:
    """DeepSeek's peak hours (模型 & 价格): Beijing time, Monday to Friday except China's public holidays, 9:00–12:00
    and 14:00–18:00. Every other hour costs half."""
    bj = t.astimezone(BEIJING)
    if bj.weekday() >= 5 or (holiday is not None and holiday(bj.date())):
        return False
    h = bj.hour + bj.minute / 60
    return 9 <= h < 12 or 14 <= h < 18


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskModel(_M):
    provider: Literal["deepseek", "openrouter"]
    model: str = Field(min_length=1)
    # how hard the model thinks before it answers: default = the provider's own (DeepSeek: on, high); none = off
    effort: Literal["default", "none", "low", "high", "max"] = "default"


class Price(_M):   # never 0: a price of nothing would leave the budget blind
    input: float = Field(gt=0)                  # 元 per million tokens: the prompt, a cache miss (DeepSeek: peak)
    cached: float | None = Field(None, gt=0)    # the prompt, a cache hit (DeepSeek); none: as a miss
    output: float = Field(gt=0)


def _default_tasks() -> dict:
    return {t: TaskModel(provider="deepseek", model=m, effort=e) for t, (m, e) in DEFAULT_TASKS.items()}


def _default_prices() -> dict:
    # DeepSeek's peak list prices (模型 & 价格, 2026-10; off-peak is half); edit them in 设置 → AI when they change
    return {"deepseek-flash": Price(input=2.0, cached=0.04, output=8.0),
            "deepseek-v4-pro": Price(input=9.0, cached=0.30, output=27.0)}


def _current(data: dict, stored: bool = True) -> dict:
    """A config as it is now, whenever it was saved: a task it does not name keeps its default; a retired DeepSeek model
    moves to what it became (a strength you chose stays); the retired prices go and the current models' come in; the
    rest is as you set it. `stored`: read back — a price of nothing saved before the check is dropped (a save refuses it)."""
    tasks = {**{t: m.model_dump() for t, m in _default_tasks().items()}, **dict(data.get("tasks") or {})}
    for t, tm in tasks.items():
        if isinstance(tm, dict) and tm.get("provider") == "deepseek" and tm.get("model") in RETIRED:
            model, effort = RETIRED[tm["model"]]
            tasks[t] = {**tm, "model": model, "effort": effort if tm.get("effort", "default") == "default" else tm["effort"]}
    kept = {k: v for k, v in (data.get("prices") or {}).items() if k not in RETIRED and not (
        stored and isinstance(v, dict) and min(v.get("input") or 0, v.get("output") or 0) <= 0)}
    return {**data, "tasks": tasks, "prices": {**{k: p.model_dump() for k, p in _default_prices().items()}, **kept}}


class AIConfig(_M):
    tasks: dict[Literal["rule_assistant", "research"], TaskModel] = Field(default_factory=_default_tasks)
    day_cap: float = Field(5.0, ge=0)        # 元
    month_cap: float = Field(50.0, ge=0)
    usd_cny: float = Field(7.2, gt=0)        # OpenRouter bills in US dollars
    prices: dict[str, Price] = Field(default_factory=_default_prices)


class AIUnavailable(Exception):
    """AI cannot be used right now (no key, over budget, offline); everything else still works."""


class AIError(Exception):
    """A call that went wrong, with the model's raw answer when there was one, and the HTTP status when it was one (429
    is a rate limit: PA_Agent's backoff reads it there)."""

    def __init__(self, message: str, raw: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.raw, self.status_code = raw, status_code


@dataclass
class Answer:
    value: Any
    call_id: str
    replayed: bool
    cost: float | None
    latency_ms: int
    model: str
    attempts: int = 1


@dataclass
class Turn:
    """One call of a multi-turn conversation (价格行为分析, design 2026-10-20 §7): the caller checks the answer itself."""
    content: str
    call_id: str
    cost: float | None
    latency_ms: int
    usage: dict


def _detail(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        return str(err.get("message") if isinstance(err, dict) else err)[:200]
    except (ValueError, AttributeError):
        return r.text[:200]


class LLM:
    RETRIES = 2

    def __init__(self, paths: Paths, transport: httpx.BaseTransport | None = None, timeout: float = 600.0):
        # thinking hard (max) can take minutes before the first byte: a long read, a short connect
        self.paths, self.conn = paths, connect(paths.shared_db, "shared")
        self.http = httpx.Client(transport=transport, timeout=httpx.Timeout(timeout, connect=15.0))
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], datetime] = lambda: datetime.now(BEIJING)
        self._closed: set[date] | None = None

    def holiday(self, d: date) -> bool:
        """A weekday China's exchanges are closed: a public holiday, off-peak for DeepSeek."""
        if self._closed is None:
            try:
                cal = Store(self.paths, "CN").read("calendar")
                self._closed = set(cal.filter(~pl.col("is_open"))["date"].to_list()) if cal.height else set()
            except Exception:  # noqa: BLE001 - no calendar here: weekdays are peak days
                self._closed = set()
        return d in self._closed

    # ---- configuration and budget ---------------------------------------------------------------------
    def config(self) -> AIConfig:
        row = self.conn.execute("SELECT value FROM setting WHERE key='ai'").fetchone()
        return AIConfig.model_validate(_current(json.loads(row[0]))) if row else AIConfig()

    def save(self, data: dict) -> AIConfig:
        cfg = AIConfig.model_validate(_current(data, stored=False))   # what the page shows back is what runs
        self.conn.execute("INSERT OR REPLACE INTO setting VALUES ('ai', ?)", (cfg.model_dump_json(),))
        return cfg

    def spent(self) -> dict:
        """元 spent today and this month (local time)."""
        now = datetime.now()
        q = "SELECT COALESCE(SUM(cost), 0) FROM llm_call WHERE created_at >= ?"
        return {"day": self.conn.execute(q, (now.date().isoformat(),)).fetchone()[0],
                "month": self.conn.execute(q, (now.strftime("%Y-%m-01"),)).fetchone()[0]}

    @staticmethod
    def _unpriced(cfg: AIConfig, tm: TaskModel) -> str | None:
        """DeepSeek does not report what a call cost: a model missing from the price list would slip past the budget."""
        if tm.provider == "deepseek" and tm.model not in cfg.prices:
            return f"模型 {tm.model} 不在价格表里，花费算不出来，预算管不住它；先在 设置 → AI 的价格表里加上它的价格"
        return None

    @staticmethod
    def _over(cfg: AIConfig, spent: dict) -> str | None:
        if spent["day"] >= cfg.day_cap:
            return f"今天的 AI 预算（¥{cfg.day_cap:g}）已经用完，明天恢复；也可以在设置里调高"
        if spent["month"] >= cfg.month_cap:
            return f"本月的 AI 预算（¥{cfg.month_cap:g}）已经用完；也可以在设置里调高"
        return None

    def status(self) -> dict:
        cfg, spent = self.config(), self.spent()
        tasks = {}
        for t, tm in cfg.tasks.items():
            prov = PROVIDERS[tm.provider]
            src = secret_source(prov["secret"])
            reason = None if src else f"还没有填 {prov['name']} 的 API key（设置 → AI）"
            reason = reason or self._unpriced(cfg, tm) or self._over(cfg, spent)
            tasks[t] = {"name": TASKS[t], "provider": tm.provider, "model": tm.model, "key": src, "ready": reason is None,
                        "reason": reason}
        return {"tasks": tasks, "spent": spent, "day_cap": cfg.day_cap, "month_cap": cfg.month_cap,
                "providers": {k: {"name": v["name"], "key": secret_source(v["secret"])} for k, v in PROVIDERS.items()}}

    def calls(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, task, provider, model, ok, error, tokens_in, tokens_out, cost, latency_ms, created_at FROM llm_call "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- the call ---------------------------------------------------------------------------------------
    def chat_json(self, task: str, system: str, user: str, schema: dict, schema_name: str, validate: Callable[[Any], Any], *,
                  template_hash: str, evidence_hash: str, replay: bool = False, retries: int = 1) -> Answer:
        """Ask `task`'s model for a JSON answer that `validate` accepts (it returns the checked value or raises
        ValueError with the reason in Chinese); a refused answer goes back with the reason, `retries` times.
        `replay` gives back the last good answer to the same template and inputs instead of calling again."""
        cfg = self.config()
        tm = cfg.tasks[task]
        prov = PROVIDERS[tm.provider]
        if replay:
            row = self.conn.execute(
                "SELECT id, output, cost, latency_ms, model FROM llm_call WHERE task=? AND template_hash=? AND evidence_hash=? "
                "AND ok=1 ORDER BY created_at DESC, rowid DESC LIMIT 1", (task, template_hash, evidence_hash)).fetchone()
            if row:
                return Answer(validate(json.loads(row["output"])), row["id"], True, row["cost"], row["latency_ms"], row["model"])
        key = get_secret(prov["secret"])
        if not key:
            raise AIUnavailable(f"还没有填 {prov['name']} 的 API key（设置 → AI）")
        over = self._unpriced(cfg, tm) or self._over(cfg, self.spent())
        if over:
            raise AIUnavailable(over)
        if prov["structured"] == "json_object" and "json" not in (system + user).lower():
            raise ValueError("json_object 模式要求提示里出现 json 字样")  # a task's prompt is wrong: a programming error
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last: tuple[str, str] = ("", "")
        costs, ms = [], 0                      # every try is paid for and waited on: the answer reports them all
        for attempt in range(1, retries + 2):  # the answer, then `retries` more tries with the reason
            content, call_id, cost, latency, _ = self._post(cfg, tm, prov, key, task, messages, schema, schema_name,
                                                            template_hash, evidence_hash)
            costs, ms = costs + ([cost] if cost is not None else []), ms + latency
            try:
                return Answer(validate(json.loads(content)), call_id, False, sum(costs) if costs else None, ms, tm.model, attempt)
            except json.JSONDecodeError:
                reason = "不是 JSON"
            except (ValueError, TypeError, KeyError) as e:
                reason = str(e) or type(e).__name__
            self.conn.execute("UPDATE llm_call SET ok=0, error=? WHERE id=?", (reason, call_id))
            last = (reason, content)
            messages = [*messages, {"role": "assistant", "content": content},
                        {"role": "user", "content": f"上面的输出不符合要求：{reason}。请改正后重新输出完整的 JSON，不要其他文字。"}]
        raise AIError(f"模型试了 {retries + 1} 次都没有给出要求的结果：{last[0]}", raw=last[1])

    def chat_messages(self, task: str, tm: TaskModel, messages: list[dict], *, template_hash: str, evidence_hash: str,
                      max_tokens: int | None = None) -> Turn:
        """One call of a conversation the caller keeps itself (价格行为分析, design 2026-10-20 §7): `messages` as they are,
        no response_format (PA_Agent sends none), `tm` the model (a task whose setting is kept outside AIConfig), the
        budget and prices of the shared AIConfig; the caller checks the answer and decides whether to ask again. Logged
        like every call."""
        cfg = self.config()
        prov = PROVIDERS[tm.provider]
        key = get_secret(prov["secret"])
        if not key:
            raise AIUnavailable(f"还没有填 {prov['name']} 的 API key（设置 → AI）")
        over = self._unpriced(cfg, tm) or self._over(cfg, self.spent())
        if over:
            raise AIUnavailable(over)
        content, call_id, cost, latency, usage = self._post(cfg, tm, prov, key, task, messages, {}, "", template_hash,
                                                            evidence_hash, structured="none", max_tokens=max_tokens)
        return Turn(content, call_id, cost, latency, usage)

    def _post(self, cfg: AIConfig, tm: TaskModel, prov: dict, key: str, task: str, messages: list, schema: dict,
              schema_name: str, template_hash: str, evidence_hash: str, *, structured: str | None = None,
              max_tokens: int | None = None) -> tuple[str, str, float | None, int, dict]:
        body: dict[str, Any] = {"model": tm.model, "messages": messages}
        if tm.provider == "deepseek":   # thinking ignores temperature; DeepSeek's default is thinking on, high
            if tm.effort == "none":
                body |= {"thinking": {"type": "disabled"}, "temperature": 0}
            elif tm.effort != "default":
                body |= {"thinking": {"type": "enabled"}, "reasoning_effort": tm.effort}
        elif tm.effort == "default":     # a model that cannot reason rejects the setting: sent only when chosen
            body["temperature"] = 0
        else:                            # and a reasoning model rejects any temperature but its own
            body["reasoning"] = {"effort": tm.effort, "exclude": True}
        if structured == "none":   # 价格行为分析, as PA_Agent sends it: no response_format, its own validator reads the answer
            pass
        elif (structured or prov["structured"]) == "json_schema":
            body |= {"response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
                     "provider": {"require_parameters": True}}
        else:
            body["response_format"] = {"type": "json_object"}
        if tm.provider == "openrouter":   # it reports what a call cost only when asked
            body["usage"] = {"include": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        # WB_AI_BASE_URL points every provider at a local stand-in (end-to-end tests, development); it is an
        # environment variable on purpose, never a setting, so a key cannot be sent elsewhere from the UI
        base_url = os.environ.get("WB_AI_BASE_URL") or prov["base_url"]
        t0 = time.monotonic()

        def log(ok: bool, output: str | None = None, error: str | None = None, usage: dict | None = None,
                cost: float | None = None) -> str:
            call_id = uuid.uuid4().hex[:16]
            usage = usage or {}
            self.conn.execute(
                "INSERT INTO llm_call VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, task, tm.provider, tm.model, template_hash, evidence_hash,
                 json.dumps({k: v for k, v in body.items()}, ensure_ascii=False), output, int(ok), error,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"), cost, int((time.monotonic() - t0) * 1000),
                 datetime.now().isoformat(timespec="seconds")))
            return call_id

        r = None
        for attempt in range(self.RETRIES + 1):
            try:
                r = self.http.post(f"{base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError as e:
                if attempt < self.RETRIES:
                    self.sleep(2 ** attempt)
                    continue
                err = f"连不上 {prov['name']}（{type(e).__name__}）：可能离线，或者网络不通"
                log(False, error=err)
                raise AIUnavailable(err) from None
            if r.status_code in RETRY_STATUS and attempt < self.RETRIES:
                self.sleep(2 ** attempt)
                continue
            break
        if r.status_code != 200:
            err = {401: "API key 不对或者已经失效（设置 → AI）", 402: f"{prov['name']} 账户余额不足",
                   429: "请求太频繁，被限流了，过一会儿再试"}.get(r.status_code, f"{prov['name']} 返回 {r.status_code}：{_detail(r)}")
            log(False, error=err)
            raise AIError(err, status_code=r.status_code)
        data = r.json()
        usage = data.get("usage") or {}
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        cost = self._cost(cfg, tm, usage)
        call_id = log(True, output=content, usage=usage, cost=cost)
        return content, call_id, cost, int((time.monotonic() - t0) * 1000), usage

    def _cost(self, cfg: AIConfig, tm: TaskModel, usage: dict) -> float | None:
        """元: OpenRouter reports its cost (US$); otherwise tokens × the price list — the prompt's cache hits and misses
        each at their price, the output (thinking included) at its own; DeepSeek off-peak, half."""
        if usage.get("cost") is not None:
            return float(usage["cost"]) * cfg.usd_cny
        p = cfg.prices.get(tm.model)
        if p is None or usage.get("prompt_tokens") is None:
            return None
        hit = usage.get("prompt_cache_hit_tokens") or 0
        miss = usage.get("prompt_cache_miss_tokens")
        miss = usage["prompt_tokens"] - hit if miss is None else miss
        cost = (hit * (p.input if p.cached is None else p.cached) + miss * p.input
                + (usage.get("completion_tokens") or 0) * p.output) / 1e6
        if tm.provider == "deepseek" and not deepseek_peak(self.clock(), self.holiday):
            cost /= 2
        return cost
