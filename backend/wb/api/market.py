"""The 市场 page (spec §7.3; designs 2026-10-12-market-live and -market-page-redesign): the board — live from the market
record while A shares trade, else the stored day's close — with today's read against yesterday's same minute, the 情绪
阶段 and 盘面变化; the industries' standings for 选股 and the K-line page; the record's rows for the small lines; breadth
by day and the indexes' state for the latest snapshot; and the 题材 tags you put on listings (rules read them with TAGGED
and universe.tags)."""
from datetime import date, datetime, time, timedelta

import polars as pl
from fastapi import Body, FastAPI, HTTPException

from wb.api.context import Context
from wb.listing import parse_id
from wb.market.board import compute, from_bars, tape_row
from wb.market.events import events as day_events
from wb.market.facts import Facts, build_facts
from wb.market.phase import classify, ladder_days, summary, today_read
from wb.market.view import market_view
from wb.markets import MARKETS
from wb.notify.notices import MARKET_NAME

SH = "CN:IDX:000001"
AUCTION, MATCHED = time(9, 15), time(9, 25)   # the call auction's reference prices, then its match (design §13)
IDS = ("sealed_ids", "broken_ids")


def _plain(row: dict) -> dict:
    """A record row for the page: its time as text, without the lists of stocks."""
    return {k: (v.isoformat(timespec="seconds") if isinstance(v, datetime) else v) for k, v in row.items() if k not in IDS}


def add_routes(app: FastAPI, ctx: Context, live=None) -> None:
    @app.get("/api/market/{market}")
    def market(market: str):
        if market not in MARKETS:
            raise HTTPException(404, market)
        snap = ctx.latest(market)  # computed once per snapshot (a few seconds on a whole market)
        key = ("market", market, ctx.paths(market).profile, snap.id if snap else None)
        return ctx.misc_cache.get(key, lambda: market_view(ctx.paths(market), market))

    def stored_close(market: str, snap) -> pl.DataFrame:
        """The stored day as the quote at its close would give it, read with the weeks before: a source without the
        close before (日股) takes the last one (from_bars). Cached per snapshot."""
        def make():
            back = pl.col("date").is_between(snap.as_of - timedelta(days=30), snap.as_of)
            return from_bars(ctx.store(market).scan("bars_1d", snap).filter(back).collect(), snap.as_of)
        return ctx.misc_cache.get(("mclose", market, ctx.paths(market).profile, snap.id), make)

    def close_board(market: str, snap) -> tuple[Facts, dict]:
        """The stored day's board, as a quote at its close would give it (cached per snapshot)."""
        def make():
            store = ctx.store(market)
            facts = build_facts(ctx.paths(market), market, snap.as_of, snap=snap)
            idx = {r["index_id"]: r["close"] for r in
                   store.scan("index_bars_1d", snap).filter(pl.col("date") == snap.as_of).collect().iter_rows(named=True)}
            return facts, compute(stored_close(market, snap), facts, datetime.combine(snap.as_of, time(15, 0)), 1.0, idx)
        return ctx.misc_cache.get(("mboard", market, ctx.paths(market).profile, snap.id), make)

    def phase_of(market: str, snap) -> dict | None:
        if market != "CN":
            return None
        profile = ctx.paths(market).profile
        return ctx.misc_cache.get(("mphase", profile, snap.id), lambda: summary(classify(ladder_days(ctx.store(market), profile, snap))))

    def changes(market: str, facts: Facts, rows: list[dict]) -> list[dict]:
        if market != "CN" or not rows:
            return []
        names = dict(zip(facts.stocks["listing_id"], facts.stocks["name"], strict=True))
        top = facts.stocks.filter(pl.col("listing_id").is_in(list(facts.top_ids)))["streak"].max() if facts.top_ids else None
        return day_events(rows, facts.indexes.get(SH), facts.top_ids, names, top + 1 if top is not None else None)

    def showing(market: str, now: datetime) -> dict:
        """What the page shows now (design §2–§7, §13; redesign §5): mode live (today's record), auction (9:15–9:25:
        the last close, the reference prices are not counted), auction_result (9:25–9:30) or close (the stored day) —
        with its session, snapshot, board, base and day."""
        s = live.session(market, now)
        tape = live.tape
        snap = ctx.latest(market)
        out = {"s": s, "snap": snap, "why": tape.why_not(market), "latest": tape.latest(market) or {}}
        if snap is None:
            return {**out, "mode": "close", "b": None, "facts": None, "day": None}
        t = s.now.time()
        latest = out["latest"]
        fresh = latest.get("board") if out["why"] is None and latest.get("day") == s.day and snap.as_of < s.day else None
        if s.state == "pre" and AUCTION <= t < MATCHED:
            mode = "auction"
        elif fresh and s.state == "pre":
            mode = "auction_result"
        elif fresh and s.state in ("trading", "lunch", "after"):
            mode = "live"
        else:
            mode = "close"
        if mode in ("live", "auction_result"):
            return {**out, "mode": mode, "b": fresh, "facts": tape.facts(market, s.day), "day": s.day}
        facts, b = close_board(market, snap)
        return {**out, "mode": mode, "b": b, "facts": facts, "day": snap.as_of}

    @app.get("/api/market/{market}/board")
    def board(market: str):
        """Everything the page shows now (see `showing`); asking it gives the record the 10-second cadence."""
        if market not in MARKETS:
            raise HTTPException(404, market)
        now = datetime.now().astimezone()
        live.tape.want(market, now)
        v = showing(market, now)
        s, snap, mode, b, facts = v["s"], v["snap"], v["mode"], v["b"], v["facts"]
        paths = ctx.paths(market)
        base = {"market": market, "session": {"state": s.state, "label": s.label, "day": s.day.isoformat(), "time": s.now.strftime("%H:%M:%S")}}
        if snap is None:
            return {**base, "mode": "close", "as_of": None, "stale": False, "error": None, "fetched_at": None, "board": None,
                    "why_not": f"{MARKET_NAME[market]}现在的数据档（{'测试档' if paths.profile == 'test' else '正式档'}）里还没有数据",
                    "read": None, "phase": None, "events": [], "yesterday": None, "amount_before": None}
        read = yesterday = None
        if mode == "live" and facts.prev_day is not None:
            then = [r for r in live.tape.rows(market, facts.prev_day) if r["i"] <= s.elapsed]
            if then:
                yesterday = _plain(then[-1])
                read = today_read(tape_row(b, s.elapsed), then[-1], s.elapsed)
        fetched = v["latest"].get("fetched")
        return {**base, "mode": mode, "as_of": snap.as_of.isoformat(), "stale": facts.stale, "why_not": v["why"],
                "amount_before": facts.amount_before,
                "error": v["latest"].get("error"), "fetched_at": fetched.isoformat(timespec="seconds") if fetched else None,
                "board": b, "read": read, "phase": None if facts.stale else phase_of(market, snap),   # days missing: unknown (§5.1)
                "events": changes(market, facts, live.tape.rows(market, v["day"])), "yesterday": yesterday}

    STANDING = ("name", "chg", "rank", "tag", "rel5", "prior15", "rel20", "limit_up", "parent", "small")

    @app.get("/api/market/{market}/industries")
    def industries(market: str):
        """Each industry's standing as the page shows it now (redesign §6, §8) — for 选股's column and the K-line's chip:
        the same board, without asking the record for 10-second fetches (only the page open does)."""
        if market not in MARKETS:
            raise HTTPException(404, market)
        v = showing(market, datetime.now().astimezone())
        b = v["b"]
        rows = {lv: [{k: r.get(k) for k in STANDING} for r in (b["industries"][lv] if b else [])] for lv in ("l1", "l2")}
        return {"market": market, "mode": v["mode"], "day": v["day"].isoformat() if v["day"] else None,
                "time": b["at"][11:16] if b and v["mode"] in ("live", "auction_result") else None,
                "of": {lv: sum(r["rank"] is not None for r in rows[lv]) for lv in rows}, **rows}

    @app.get("/api/market/{market}/tape")
    def tape_rows(market: str, day: str | None = None):
        """A day's record for the strip (§3.2): its rows by minute, the day before's 涨停 and 上证 by minute, and
        上证's lines; without `day`, today's — or the stored day's when today has none yet."""
        if market not in MARKETS:
            raise HTTPException(404, market)
        try:
            asked = date.fromisoformat(day) if day else None
        except ValueError:
            raise HTTPException(422, "日期要写成 YYYY-MM-DD") from None
        tape = live.tape
        snap = ctx.latest(market)
        d = asked or live.session(market, datetime.now().astimezone()).day
        rows = tape.rows(market, d)
        if not rows and asked is None and snap is not None:
            d = snap.as_of
            rows = tape.rows(market, d)
        facts = tape.facts(market, d) if snap is not None else None
        prev = facts.prev_day if facts is not None else None
        prev_rows = [{"i": r["i"], "limit_up": r["limit_up"], "sh_last": r["sh_last"]} for r in tape.rows(market, prev)] if prev else []
        return {"day": d.isoformat(), "rows": [_plain(r) for r in rows], "prev_day": prev.isoformat() if prev else None,
                "prev_rows": prev_rows, "index": facts.indexes.get(SH) if facts is not None else None}

    @app.get("/api/tags")
    def all_tags():
        return ctx.workspace().all_tags()

    @app.get("/api/tags/{listing_id}")
    def tags_of(listing_id: str):
        parse_id(listing_id)
        return ctx.workspace().tags_of(listing_id)

    @app.put("/api/tags/{listing_id}")
    def set_tags(listing_id: str, tags: list[str] = Body(..., embed=True)):
        parse_id(listing_id)  # a bad listing id is a 422 with the reason
        if any('"' in t or "\\" in t for t in tags):
            raise HTTPException(422, "题材标签不能带引号或反斜杠（规则里要用引号写它）")
        return ctx.workspace().set_tags(listing_id, tags)
