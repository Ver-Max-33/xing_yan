"""价格行为分析's model (design 2026-10-20 §7, §8): kept under its own setting key, never inside main's `ai` setting — main's
AIConfig knows only its two tasks and would refuse a third. The budget and prices are the shared ones. PA_Agent's own
settings stay at its code defaults; only where its model call goes is filled in from the task."""
import json

from pa_agent.config.settings import Settings

from wb.ai.client import LLM, PROVIDERS, TaskModel
from wb.config import Paths
from wb.db import connect
from wb.secrets import secret_source

TASK = "price_action"     # the AI call log's task name
NAME = "价格行为"
KEY = "pa.model"          # its row in the shared setting table
DEFAULT = TaskModel(provider="deepseek", model="deepseek-v4-pro", effort="high")
MAX_TOKENS = 384_000   # as PA_Agent sends it to every gateway: min(the gateway's cap, 384,000); DeepSeek's is 393,216


def load_task(paths: Paths) -> TaskModel:
    row = connect(paths.shared_db, "shared").execute("SELECT value FROM setting WHERE key=?", (KEY,)).fetchone()
    return TaskModel.model_validate(json.loads(row[0])) if row else DEFAULT


def save_task(paths: Paths, data: dict) -> TaskModel:
    tm = TaskModel.model_validate(data)
    connect(paths.shared_db, "shared").execute("INSERT OR REPLACE INTO setting VALUES (?, ?)", (KEY, tm.model_dump_json()))
    return tm


def status(llm: LLM, tm: TaskModel) -> dict:
    """Whether an analysis could call its model now, and if not, why (the same reasons as the other AI tasks)."""
    cfg, spent = llm.config(), llm.spent()
    prov = PROVIDERS[tm.provider]
    reason = None if secret_source(prov["secret"]) else f"还没有填 {prov['name']} 的 API key（设置 → AI）"
    reason = reason or LLM._unpriced(cfg, tm) or LLM._over(cfg, spent)
    return {"name": NAME, "provider": tm.provider, "model": tm.model, "effort": tm.effort, "ready": reason is None,
            "reason": reason}


def pa_settings(tm: TaskModel) -> Settings:
    """PA_Agent's settings at its code defaults (design §2), with its provider fields pointing at the task's model."""
    return Settings(provider={"model": tm.model, "base_url": PROVIDERS[tm.provider]["base_url"], "thinking": tm.effort != "none"},
                    general={"decision_stance": "balanced"})


def max_tokens(tm: TaskModel) -> int | None:
    return MAX_TOKENS


def prefix_chain(tm: TaskModel) -> bool:
    """Stage 2 goes on from stage 1's conversation where the shared prefix is cached — PA's own rule: DeepSeek's API, or
    a DeepSeek model through another gateway."""
    return tm.provider == "deepseek" or "deepseek" in tm.model.lower()
