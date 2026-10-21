"""价格行为分析, one analysis (design 2026-10-20 §1, §5.1, §7): PA_Agent's two stages in its own order of steps —
preflight, stage 1 and its validator with retries, the gate, the strategy files, stage 2 continuing stage 1's
conversation — through our LLM client, so every call is logged and budgeted. Then the AI's own plan, the final plan
after PA's program and the A-share guards, and the list of changes between them."""
import copy
import functools
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from pa_agent.ai.decision_nodes import check_preflight_data
from pa_agent.ai.decision_tree import build_stage2_gate_wait_response
from pa_agent.ai.json_validator import JsonValidator, ValidationError
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.provider_errors import PROVIDER_QUOTA_USER_MESSAGE, is_provider_quota_exhausted
from pa_agent.ai.rate_limit import call_with_rate_limit_backoff
from pa_agent.ai.router import route_strategy_files
from pa_agent.ai.validation_messages import format_validation_errors
from pa_agent.config.paths import PROMPT_DIR
from pa_agent.orchestrator.validation_retry import validate_with_retry
from pa_agent.util.threading import CancelToken

from wb.ai.client import LLM, AIError, AIUnavailable, TaskModel
from wb.pa.frame import DailyInput
from wb.pa.plans import a_share_guard, ai_original, parse_reply, program_changes
from wb.pa.settings import TASK, max_tokens, pa_settings, prefix_chain

log = logging.getLogger(__name__)
STANCE, COOLDOWN = "balanced", 3     # PA's defaults (design §2): the balanced stance, 3 bars before a flip


class Cancelled(Exception):
    pass


class _Failed(Exception):
    pass


@dataclass
class Result:
    status: str = "ok"                # ok | failed | cancelled
    error: str | None = None
    stage1: dict | None = None
    stage2: dict | None = None
    ai_plan: dict | None = None
    final_plan: dict | None = None
    changes: list = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    cost: float | None = None
    latency_ms: int = 0
    prompt_hash: str = ""


def _digest(files: list[Path]) -> str:
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@functools.cache
def prompt_version() -> str:
    """Every prompt text PA sends and the A-share patch to its code (§5.1): change one, and the version changes."""
    return _digest(sorted(PROMPT_DIR.glob("*.txt")) + [PROMPT_DIR.parent / "a_share.patch"])


@functools.cache
def code_version() -> str:
    """Our own code around PA (wb/pa: the guard, the limits the table shows, the calls): with prompt_version, what has
    to stay the same for the same input to keep its analysis (10-21)."""
    return _digest(sorted(Path(__file__).parent.glob("*.py")))


class _Client:
    """PA's side of the model, through our client: the cancel checked before and after each call, ids and costs kept."""

    def __init__(self, llm: LLM, tm: TaskModel, inp: DailyInput, cancel: CancelToken):
        self.llm, self.tm, self.cancel = llm, tm, cancel
        self.evidence = hashlib.sha256(json.dumps(inp.bars_json, sort_keys=True).encode()).hexdigest()
        self.call_ids: list[str] = []
        self.costs: list[float | None] = []

    def call(self, messages: list[dict]) -> SimpleNamespace:
        if self.cancel.is_set():
            raise Cancelled
        t = call_with_rate_limit_backoff(   # as upstream's client: a 429 waits 5 → 80 s and asks again, cancellable
            lambda: self.llm.chat_messages(TASK, self.tm, messages, template_hash=prompt_version(),
                                           evidence_hash=self.evidence, max_tokens=max_tokens(self.tm)),
            cancel_token=self.cancel, stage_label="价格行为分析")
        self.call_ids.append(t.call_id)
        self.costs.append(t.cost)
        if self.cancel.is_set():
            raise Cancelled
        return SimpleNamespace(content=t.content, reasoning_content="",    # our client hands PA no thinking channel
                               usage=SimpleNamespace(prompt_tokens=t.usage.get("prompt_tokens") or 0,
                                                     completion_tokens=t.usage.get("completion_tokens") or 0))


def _json_truncation_hint(content: str, err: ValidationError) -> str | None:
    """Upstream two_stage._json_truncation_hint, as it is: incomplete JSON (the stream stopped) against a stray typo."""
    if err.category != "a":
        return None
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return None
    depth = 0
    in_string = False
    escape = False
    for ch in stripped:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if depth > 0 or not stripped.rstrip().endswith("}"):
        return (
            f"阶段 JSON 正文约 {len(stripped)} 字符，在输出过程中被截断"
            f"（未闭合对象约 {max(depth, 1)} 层，解析位置 {err.parse_position}）。"
            " 常见原因：completion 额度主要在思考区用尽，正文 JSON 只写了一小段。"
        )
    return None


def _failure(step: str, attempts: int, err: ValidationError, reply: SimpleNamespace) -> str:
    """PA's own message for an answer its checks refused (upstream two_stage._enrich_stage1/2_validation_message; their
    branches about the thinking channel are left out, since our client hands PA none), after which step and tries."""
    if err.category == "e" or is_provider_quota_exhausted(err.raw_text):
        return err.message or PROVIDER_QUOTA_USER_MESSAGE
    detail = format_validation_errors(err.invalid_fields, missing_fields=err.missing_fields)
    trunc = _json_truncation_hint((reply.content or "").strip(), err)
    msg = f"{err.message}。{trunc} completion_tokens≈{reply.usage.completion_tokens}，思考区约 0 字。" if trunc else err.message
    return f"{step}的回答试了 {attempts} 次都没通过检查：" + (f"{msg}。{detail}" if detail else msg)


def _asked(stage: str, step: str, messages: list[dict], client: _Client, validator: JsonValidator, s,
           kwargs: dict, on_stage: Callable[[str], None]):
    """One stage's call, then PA's validate_with_retry, which asks again with its feedback when it may."""
    reply = client.call(messages)
    tries = 0

    def again(msgs: list[dict]) -> SimpleNamespace:
        nonlocal tries
        tries += 1
        on_stage(f"{step}：重答第 {tries} 次")
        return client.call(msgs)

    vr = validate_with_retry(stage=stage, messages=messages, reply=reply, validator=validator,
                             validation_settings=s.validation, validate_kwargs=kwargs, call_api=again,
                             provider_settings=s.provider)
    if isinstance(vr.result, ValidationError):
        raise _Failed(_failure(step, vr.attempts, vr.result, vr.reply))
    return vr


def _two_stages(inp: DailyInput, client: _Client, tm: TaskModel, on_stage: Callable[[str], None], res: Result) -> None:
    """Upstream TwoStageOrchestrator.submit (two_stage.py 359–1186) without its streaming, events, fallback providers
    or pending-record writer; `res` fills as the stages finish, so a failure keeps what came before it."""
    frame, s = inp.frame, pa_settings(tm)
    pf = check_preflight_data(frame)
    if not pf.ok:
        raise _Failed(pf.reason)
    validator = JsonValidator(s)
    assembler = PromptAssembler(PROMPT_DIR, prompt_settings=s.prompt)
    on_stage("第一步：读 K 线")
    vr1 = _asked("stage1", "第一步", assembler.build_stage1(frame, analysis_mode="original"), client, validator, s,
                 {"kline_frame": frame, "incremental_new_bar_count": 0, "incremental_previous_stage1": None}, on_stage)
    res.stage1 = stage1 = vr1.result.obj
    strategy_files = route_strategy_files(stage1)
    if str(stage1.get("gate_result", "proceed")).lower() in ("wait", "unknown"):   # PA answers stage 2 itself
        res.stage2 = build_stage2_gate_wait_response(stage1)
        res.ai_plan = copy.deepcopy(res.stage2["decision"])
    else:
        on_stage("第二步：给方案")
        messages = assembler.build_stage2_continuation(
            frame=frame, stage1_messages=vr1.messages, stage1_reply_content=vr1.reply.content, stage1_json=stage1,
            strategy_files=strategy_files, experience_entries=[], decision_stance=STANCE, previous_record=None,
            enable_next_bar_prediction=False, provider_settings=s.provider, use_prefix_chain=prefix_chain(tm),
            structure_flip_cooldown_bars=COOLDOWN)
        vr2 = _asked("stage2", "第二步", messages, client, validator, s,
                     {"kline_frame": frame, "decision_stance": STANCE, "stage1_json": stage1, "skip_next_bar": True,
                      "previous_record": None, "structure_flip_cooldown_bars": COOLDOWN}, on_stage)
        stage2 = copy.deepcopy(vr2.result.obj)
        stage2.pop("next_bar_prediction", None)
        res.stage2 = stage2
        res.ai_plan = ai_original(parse_reply(vr2.reply.content, vr2.reply.reasoning_content), stage1)
    res.final_plan, guard = a_share_guard(res.stage2["decision"], next_limit_up=frame.next_limit_up)
    res.changes = program_changes(res.ai_plan, res.stage2["decision"], res.stage2) + guard


def analyze(inp: DailyInput, llm: LLM, tm: TaskModel, *, cancel: CancelToken,
            on_stage: Callable[[str], None] = lambda s: None) -> Result:
    """One analysis of `inp`. It never raises: a model, budget or validation problem is status "failed" with the
    reason in Chinese, a cancel "cancelled", and what was spent so far is kept either way."""
    t0 = time.monotonic()
    client = _Client(llm, tm, inp, cancel)
    res = Result(call_ids=client.call_ids, prompt_hash=prompt_version())
    try:
        _two_stages(inp, client, tm, on_stage, res)
    except Cancelled:
        res.status, res.error = "cancelled", "已取消"
    except (AIUnavailable, AIError, _Failed) as e:
        res.status, res.error = "failed", str(e)
    except Exception as e:   # a background job: the record must still close, with what was spent
        log.exception("价格行为分析出错")
        res.status, res.error = "failed", f"程序出错：{type(e).__name__}: {e}"
    res.cost = None if None in client.costs else sum(client.costs)
    res.latency_ms = int((time.monotonic() - t0) * 1000)
    return res
