"""Fake HTTP servers for provider tests (no network in the default test run)."""
import json

import httpx


def tushare_server(tables: dict, denied: set[str] = frozenset(), calls: list | None = None,
                   columns: dict[str, set[str]] | None = None):
    """tables: api_name -> list of row dicts (or a callable(params) -> list of row dicts). `columns`: the fields an api
    has; one asked for that it does not have is left out of the answer, as Tushare does (no error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        api, params, fields = body["api_name"], body["params"], body["fields"].split(",")
        if columns and api in columns:
            fields = [f for f in fields if f in columns[api]]
        if calls is not None:
            calls.append((api, params))
        if api in denied:
            return httpx.Response(200, json={"code": 40203, "msg": "抱歉，您没有访问该接口的权限", "data": None})
        rows = tables.get(api, [])
        rows = rows(params) if callable(rows) else rows
        rows = rows[params.get("offset", 0): params.get("offset", 0) + params.get("limit", 10**9)]
        return httpx.Response(200, json={"code": 0, "msg": None, "data": {
            "fields": fields, "items": [[r.get(f) for f in fields] for r in rows]}})

    return httpx.MockTransport(handler)


def jquants_server(routes: dict, calls: list | None = None, page_size: int = 10**9):
    """routes: path (without /v2) -> list of rows, or (status, message) for an error, a dict for a body as it is
    (/bulk/get's {"url": …}) or bytes for a file (a bulk download, any host).
    Splits responses into pages of `page_size` linked by pagination_key."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        params = dict(request.url.params)
        if calls is not None:
            calls.append((path, params, request.headers.get("x-api-key")))
        route = routes.get(path, [])
        if isinstance(route, tuple):
            return httpx.Response(route[0], json={"message": route[1]})
        if isinstance(route, dict):
            return httpx.Response(200, json=route)
        if isinstance(route, bytes):
            return httpx.Response(200, content=route)
        rows = route(params) if callable(route) else route
        start = int(params.get("pagination_key", 0))
        body = {"data": rows[start:start + page_size]}
        if start + page_size < len(rows):
            body["pagination_key"] = str(start + page_size)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)
