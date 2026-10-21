"""观察条目 (spec §11): the stocks you watch, their status, texts with versions, fail lines and review dates."""
from fastapi import Body, FastAPI, HTTPException

from wb.api.context import Context
from wb.listing import parse_id
from wb.workspace.watch import Watch


def add_routes(app: FastAPI, ctx: Context) -> None:
    def watch() -> Watch:
        return Watch(ctx.paths)

    def named(e: dict) -> dict:
        market = e["listing_id"].split(":", 1)[0]
        names = ctx.instruments(market).select("listing_id", "name")
        hit = names.filter(names["listing_id"] == e["listing_id"])
        return {**e, "market": market, "name": hit["name"][0] if hit.height else None}

    def entry(lid: str) -> dict:
        try:
            return named(watch().entry(lid))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None

    def guard(fn):
        try:
            return fn()
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None

    @app.get("/api/watch")
    def entries(status: str | None = None):
        return guard(lambda: [named(e) for e in watch().entries(status)])

    @app.post("/api/watch")
    def add(body: dict = Body(...)):
        lid = str(body.get("listing_id", ""))
        parse_id(lid)
        guard(lambda: watch().add(lid, source=str(body.get("source", "")), reason=str(body.get("reason", "")),
                                  status=str(body.get("status", "watching"))))
        return entry(lid)

    @app.post("/api/watch/delete")
    def delete(listing_ids: list[str] = Body(..., embed=True)):
        """删除: these stocks' 观察条目 and all they had (spec §11) — not 已关闭, which keeps the record."""
        return {"deleted": watch().delete(listing_ids)}

    @app.get("/api/watch/{listing_id}")
    def one(listing_id: str):
        return entry(listing_id)

    @app.put("/api/watch/{listing_id}/status")
    def status(listing_id: str, status: str = Body(..., embed=True)):
        guard(lambda: watch().set_status(listing_id, status))
        ws = ctx.workspace()
        if status == "closed":                                    # off the lists the K-line page walks through
            for g in ws.groups_of(listing_id):
                ws.remove_item(g, listing_id)
        elif not ws.groups_of(listing_id) and (groups := ws.groups()):
            ws.add_item(groups[0]["id"], listing_id)               # opened again: back in the first group
        return entry(listing_id)

    @app.get("/api/watch/{listing_id}/fail-lines")
    def fail_lines(listing_id: str):
        """持有中: the fail lines its structures suggest, highest first (design 2026-10-10 §2) — you pick one to save."""
        parse_id(listing_id)
        return watch().fail_lines(listing_id)

    @app.put("/api/watch/{listing_id}/text")
    def text(listing_id: str, kind: str = Body(...), text: str = Body(...)):
        entry(listing_id)
        guard(lambda: watch().set_text(listing_id, kind, text))
        return entry(listing_id)

    @app.get("/api/watch/{listing_id}/versions")
    def versions(listing_id: str, kind: str):
        return watch().versions(listing_id, kind)

    @app.post("/api/watch/{listing_id}/conditions")
    def add_condition(listing_id: str, body: dict = Body(...)):
        entry(listing_id)
        cid = guard(lambda: watch().add_condition(listing_id, str(body.get("kind", "")), str(body.get("text", "")),
                                                  op=body.get("op"), price=body.get("price"), due=body.get("due")))
        return {"id": cid, **entry(listing_id)}

    @app.delete("/api/watch/{listing_id}/conditions/{cid}")
    def remove_condition(listing_id: str, cid: int):
        watch().remove_condition(listing_id, cid)
        return entry(listing_id)
