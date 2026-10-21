"""AI settings, status, a test call and the call log (spec §10.1); the rule assistant (§6.1–§6.4). The keys go
through /api/secrets like every other."""
import difflib

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import ValidationError

from wb.ai.client import LLM, TASKS
from wb.ai.tasks.rule_assistant import apply_choices, draft, edit
from wb.api.context import Context
from wb.engine.l3.rule import load_rule
from wb.engine.l3.understand import understand
from wb.markets import MARKETS
from wb.workspace.labels import HitLabels

TEST_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def _zh(e: ValidationError) -> str:
    return "；".join(f"{'.'.join(str(x) for x in err['loc'])}：{err['msg']}" for err in e.errors())


def add_routes(app: FastAPI, ctx: Context) -> None:
    def llm() -> LLM:
        # shared.sqlite is common to every profile; tests hand in a transport that answers like the provider
        return LLM(ctx.paths(MARKETS[0]), transport=getattr(app.state, "llm_transport", None))

    def text_of(body: dict, key: str, what: str) -> str:
        v = body.get(key) if isinstance(body, dict) else None
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(422, f"要写{what}")
        return v

    @app.get("/api/ai/status")
    def ai_status():
        return llm().status()

    @app.get("/api/ai/settings")
    def ai_settings():
        return llm().config().model_dump()

    @app.put("/api/ai/settings")
    def ai_put_settings(body: dict = Body(...)):
        try:
            return llm().save(body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, f"AI 设置不对：{_zh(e)}") from None

    @app.post("/api/ai/test")
    def ai_test(body: dict = Body(default={})):
        """A tiny call to the task's model: is the key right, does the model give structured output."""
        task = body.get("task", "rule_assistant")
        if task not in TASKS:
            raise HTTPException(422, f"task 要选 {'、'.join(TASKS)} 之一")

        def check(d):
            if d.get("ok") is not True:
                raise ValueError("ok 要是 true")
            return d
        a = llm().chat_json(task, '只回复 json：{"ok": true}', "测试连接", TEST_SCHEMA, "ok", check,
                            template_hash="test", evidence_hash="test")
        return {"ok": True, "model": a.model, "latency_ms": a.latency_ms, "cost": a.cost}

    @app.get("/api/ai/calls")
    def ai_calls(limit: int = Query(50, ge=1, le=500)):
        return llm().calls(limit)

    @app.post("/api/assistant/draft")
    def assistant_draft(body: dict = Body(...)):
        """Your description → a checked rule, its reading, the assumptions and the phrases to decide."""
        return draft(llm(), text_of(body, "text", "一段选股描述"))

    @app.post("/api/assistant/edit")
    def assistant_edit(body: dict = Body(...)):
        """The current rule + what to change → the whole new rule, the note and the difference. With `rule_id`, what
        you marked on that rule goes along (spec §6.1 修改时附上用户的标注摘要)."""
        rule_text, instruction = text_of(body, "rule_text", "当前的规则"), text_of(body, "instruction", "要怎么改")
        labels = ""
        if isinstance(body.get("rule_id"), str):
            marks = HitLabels(ctx.paths(MARKETS[0])).all(body["rule_id"])
            if marks:
                right = sum(m["verdict"] == "right" for m in marks)
                labels = f"用户在样例图集里给这条规则的命中标过：对 {right} 个、错 {len(marks) - right} 个。"
        return edit(llm(), rule_text, instruction, labels)

    @app.post("/api/assistant/apply")
    def assistant_apply(body: dict = Body(...)):
        """An assumption's option or an unmapped phrase's stand-in, applied by the program (no AI)."""
        text = text_of(body, "text", "规则")
        try:
            out = apply_choices(text, body.get("changes") or [], body.get("add") or [])
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(422, f"改不了：{e}") from None
        diff = "".join(difflib.unified_diff((text.strip() + "\n").splitlines(True), (out.strip() + "\n").splitlines(True), "现在", "改后"))
        return {"text": out, "diff": diff, "understanding": understand(load_rule(out))}  # RuleError → 422 with its reason
