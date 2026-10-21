"""个股研究 (spec §10.3) and its record (§10.4): the evidence pack to read before anything is sent, a research on the
latest day, a stock's researches each with what changed since the one before, and every research with the forward
evaluation (researches whose 20 trading days have passed are settled on the way); the documents you import for a
stock (用户导入的 IR 资料)."""
from datetime import date

from fastapi import Body, FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from wb.ai import journal
from wb.ai.client import LLM
from wb.ai.evidence import pack
from wb.api.context import Context
from wb.data.store import Store
from wb.docs import ir
from wb.docs.profile import profile
from wb.devdata import DEMO
from wb.listing import parse_id
from wb.markets import MARKETS


def add_routes(app: FastAPI, ctx: Context) -> None:
    def llm() -> LLM:
        return LLM(ctx.paths(MARKETS[0]), transport=getattr(app.state, "llm_transport", None))

    @app.get("/api/research/pack")
    def research_pack(listing_id: str, refresh: bool = False):
        """What a research would be given now; `refresh` asks for the stock's latest announcements first."""
        market, _ = parse_id(listing_id)
        paths = ctx.paths(market)
        snap = Store(paths, market).latest()
        if snap is None:
            raise HTTPException(422, f"{market} 还没有数据")
        if refresh and snap.note == DEMO:
            raise HTTPException(422, "演示数据不取真实公告")
        missing = journal.fresh_docs(paths, listing_id, snap.as_of) if refresh and market == "CN" else []
        return {**pack(ctx.paths, listing_id, snap.as_of, left_out=missing), "demo": snap.note == DEMO}   # demo: no real source asked

    @app.get("/api/profile")
    def company_profile(listing_id: str, as_of: date | None = None, refresh: bool = False):
        """股票资料 · 公司 (wb.docs.profile): asked from Tushare once a day (`refresh`: now), as of a day (today by default)."""
        market, _ = parse_id(listing_id)
        paths = ctx.paths(market)
        snap = Store(paths, market).latest()
        if snap is not None and snap.note == DEMO:
            return {"listing_id": listing_id, "as_of": str(as_of or date.today()), "unavailable": "演示数据不取真实的公司资料", "errors": {}}
        return profile(paths, listing_id, as_of or date.today(), source=getattr(app.state, "profile_source", None), refresh=refresh)

    @app.post("/api/research")
    def research_run(body: dict = Body(...)):
        lid = body.get("listing_id") if isinstance(body, dict) else None
        if not isinstance(lid, str):
            raise HTTPException(422, "要写 listing_id")
        try:
            return journal.run(llm(), ctx.paths, lid)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/research")
    def research_history(listing_id: str):
        """The stock's researches, newest first, each with what changed in its evidence since the one before."""
        h = journal.history(ctx.paths(parse_id(listing_id)[0]), listing_id)
        return [{**r, "diff": journal.diff(h[i + 1]["pack"], r["pack"]) if i + 1 < len(h) else None} for i, r in enumerate(h)]

    @app.get("/api/research/recent")
    def research_recent(limit: int = Query(100, ge=1, le=1000)):
        paths = ctx.paths(MARKETS[0])
        journal.settle(ctx.paths)
        return {"records": journal.recent(paths, limit), "evaluation": journal.evaluation(paths)}

    # ---- 你导入的 IR 资料 ----------------------------------------------------------------------
    @app.post("/api/docs/ir")
    async def ir_add(request: Request, listing_id: str, filename: str):
        """The file as the request's body (no form encoding: no extra dependency), refused past the limit before it is
        all read; parsed off the event loop."""
        parse_id(listing_id)
        big = HTTPException(413, f"文件太大，最多 {ir.MAX_BYTES / 1e6:.0f} MB")
        if int(request.headers.get("content-length") or 0) > ir.MAX_BYTES:
            raise big
        data = bytearray()
        async for chunk in request.stream():   # a body without a length: counted as it comes
            data += chunk
            if len(data) > ir.MAX_BYTES:
                raise big
        try:
            return await run_in_threadpool(ir.add, ctx.paths(MARKETS[0]), listing_id, filename, bytes(data))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/docs/ir")
    def ir_list(listing_id: str):
        return ir.docs(ctx.paths(MARKETS[0]), listing_id)

    @app.get("/api/docs/ir/{rid}")
    def ir_one(rid: str):
        paths = ctx.paths(MARKETS[0])
        try:
            return {"doc": ir.get(paths, rid), "pages": ir.page_texts(paths, rid)}
        except KeyError as e:
            raise HTTPException(404, str(e)) from None

    @app.put("/api/docs/ir/{rid}")
    def ir_excerpt(rid: str, body: dict = Body(...)):
        try:
            return ir.set_excerpt(ctx.paths(MARKETS[0]), rid, str(body.get("excerpt") or ""), int(body.get("page") or 0))
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.delete("/api/docs/ir/{rid}")
    def ir_remove(rid: str):
        try:
            ir.remove(ctx.paths(MARKETS[0]), rid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        return {"ok": True}
