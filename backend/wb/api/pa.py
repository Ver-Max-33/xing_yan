"""价格行为分析 (design 2026-10-20 §6, dev branch): what an analysis would use before the click, a start that runs in the
background, its record (with how its plans were settled), a stock's analyses, cancelling, every analysis with the
evaluation, and its model setting. The fixed routes come before /api/pa/{analysis_id}: FastAPI matches in order."""
import logging

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import ValidationError

from wb.ai.client import LLM, AIUnavailable
from wb.api.context import Context
from wb.listing import parse_id
from wb.pa import records, settle
from wb.pa.frame import daily_input
from wb.pa.jobs import RUNNER, Busy, ready
from wb.pa.settings import load_task, save_task, status


log = logging.getLogger(__name__)


def add_routes(app: FastAPI, ctx: Context) -> None:
    def paths():
        return ctx.paths("CN")       # the records live in the shared database: any profile's paths reach them

    @app.get("/api/pa/preview")
    def pa_preview(listing_id: str):
        """§6.1: the day the analysis would read, shown before the click, and why it could not start now."""
        market, _ = parse_id(listing_id)
        p, reason, inp = ctx.paths(market), None, None
        try:
            ready(ctx.paths, listing_id)
        except (ValueError, AIUnavailable) as e:
            reason = str(e)
        if market == "CN":
            try:
                inp = daily_input(p, listing_id)
            except ValueError as e:   # not enough bars
                reason = reason or str(e)
        return {"supported": market == "CN", "day": inp.day.isoformat() if inp else None, "model": load_task(p).model,
                "ready": reason is None, "reason": reason, "adjusted": p.profile != "test", "demo": bool(inp and inp.demo)}

    @app.get("/api/pa/settings")
    def pa_settings_get():
        tm = load_task(paths())
        return {"task": tm.model_dump(), "status": status(LLM(paths()), tm)}

    @app.put("/api/pa/settings")
    def pa_settings_put(body: dict = Body(...)):
        try:
            return save_task(paths(), body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, f"模型设置不对：{e.errors()[0]['msg']}") from None

    @app.get("/api/pa/recent")
    def pa_recent(limit: int = Query(200, ge=1, le=1000)):
        """Every analysis, newest first, and the evaluation; those due are settled on the way, as on the daily run."""
        try:
            settle.settle_all(ctx.paths, wait=False)
        except Exception:  # noqa: BLE001 — the records still show; the daily run tries again
            log.exception("价格行为 settle failed")
        return {"records": records.recent(paths(), limit), "evaluation": settle.evaluation(paths())}

    @app.post("/api/pa", status_code=202)
    def pa_start(body: dict = Body(...)):
        lid = body.get("listing_id") if isinstance(body, dict) else None
        if not isinstance(lid, str):
            raise HTTPException(422, "要写 listing_id")
        try:
            s = RUNNER.start(ctx.paths, lid, transport=getattr(app.state, "llm_transport", None))
            return {"id": s.id, "reused": s.reused}   # reused: the same input, its analysis already made
        except Busy as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:   # the market, the listing, the data-use switch, not enough bars
            raise HTTPException(422, str(e)) from None

    @app.get("/api/pa")
    def pa_history(listing_id: str):
        """The stock's analyses, newest first, each with how its plans were settled."""
        return [{**r, "trades": records.trades(paths(), r["id"])} for r in records.history(paths(), listing_id)]

    @app.get("/api/pa/{analysis_id}")
    def pa_get(analysis_id: str):
        row = records.get(paths(), analysis_id)
        if row is None:
            raise HTTPException(404, f"没有这次分析：{analysis_id}")
        return {**row, "trades": records.trades(paths(), analysis_id)}

    @app.post("/api/pa/{analysis_id}/cancel")
    def pa_cancel(analysis_id: str):
        return {"ok": RUNNER.cancel(analysis_id)}
