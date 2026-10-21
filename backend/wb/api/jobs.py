"""The daily cycle and what it found (spec §7.1): where each market stands, a manual run, the 今日 feed (what the
latest scan of each enabled rule changed against its previous run, and data problems), the notification channels
as they declare their settings (§3.5), the outbox, and a test card to your own bot."""
import json
from datetime import date, datetime

from fastapi import Body, FastAPI, HTTPException, Query

from wb.api.context import Context
from wb.data.capability import probe_market
from wb.data.store import Store
from wb.db import connect
from wb.engine.l3.rule import RuleError
from wb.engine.l3.understand import names as rule_names
from wb.engine.l3.run import run_rows
from wb.jobs.daily import DATASET_NAME, DEMO
from wb.markets import MARKETS
from wb.notify.feishu import Feishu, FeishuError, brief_card, cards, check_webhook
from wb.notify.notices import MARKET_NAME
from wb.notify.outbox import Outbox
from wb.realtime.alerts import Alerts
from wb.registry import CHANNELS, REALTIME
from wb.screening.scan import KINDS, compare
from wb.screening.screen import WHERE_NAMES, NotYet, Unreadable, for_market, found_by, holds, latest_run, screen, stage_labels
from wb.workspace.briefing import BriefItems
from wb.secrets import get_secret, secret_source
from wb.workspace.rules import Rules

ORDER = {k: i for i, k in enumerate(("data", "signal", "live", "advance", "invalid", "watch"))}
ALSO_STATUS = {"signal": ("signal",), "watch": ("signal", "watch")}   # 同时满足另一条规则: 命中 / 命中或潜伏
SOURCE = {"env": "环境变量", ".env": "仓库 .env 文件", "keychain": "系统钥匙串"}


def stage_names(paths, rule_id: str) -> dict[str, str]:
    """How a rule's stages read in 今日's lines (names(): 中箱, 金品, 信号 …); a rule no longer there: none, ids show."""
    try:
        return rule_names(Rules(paths).get(rule_id)[0])["stages"]
    except RuleError:
        return {}


def add_routes(app: FastAPI, ctx: Context) -> None:
    def outbox() -> Outbox:
        return Outbox(ctx.paths(MARKETS[0]))  # shared.sqlite: the same whatever the profile

    def summary(st: dict) -> dict:
        return json.loads(st["scan_summary"]) if st["scan_summary"] else {}

    def channels() -> dict:
        """Each channel as it declares its settings, where each is set, and whether it can send: a required setting
        missing, or a value that cannot work (a webhook in .env that is not Feishu's), is said."""
        out = {}
        for key, ch in CHANNELS.items():
            fields = [{**f, "source": secret_source(f["name"])} for f in ch.FIELDS]
            problem = next((f"还没有填{f['label']}" for f in fields if f["required"] and not f["source"]), None)
            if problem is None and (url := get_secret("feishu_webhook")) and key == "feishu":
                try:
                    check_webhook(url)
                except ValueError as e:
                    problem = f"{e}（现在的地址来自{SOURCE.get(fields[0]['source'], fields[0]['source'])}）"
            out[key] = {"name": ch.NAME, "fields": fields, "ready": problem is None, "problem": problem}
        return out

    @app.get("/api/jobs")
    def jobs():
        d, out = app.state.daily, []
        for m in MARKETS:
            st, latest = d.state(m), ctx.latest(m)
            out.append({**st, "scan_summary": summary(st), "running": d.running(m), "progress": d.progress(m),
                        "latest": {"id": latest.id, "as_of": str(latest.as_of), "demo": latest.note == DEMO} if latest else None})
        return {"markets": out, "outbox": outbox().stats(), "feishu": channels()["feishu"]["ready"]}

    @app.post("/api/jobs/{market}/run")
    def run(market: str):
        """A manual run; `fetch`: whether it fetches, or only scans what there is again (the market open, demo data, a
        补历史 to finish)."""
        if market not in MARKETS:
            raise HTTPException(404, market)
        fetch = app.state.daily.fetches(market)
        return {"started": app.state.daily.run_now(market), "fetch": fetch}

    def market_of(market: str) -> str:
        if market not in MARKETS:
            raise HTTPException(404, market)
        return market

    @app.post("/api/jobs/{market}/pause")
    def pause(market: str):
        """暂停: the market's running scan stops at its next step and keeps the rules it finished; False: none runs."""
        return {"paused": app.state.daily.pause(market_of(market))}

    @app.post("/api/jobs/{market}/cancel")
    def cancel(market: str):
        """取消: the running (or paused) scan is given up; 今日 keeps the last scan's results. False: none to cancel."""
        return {"cancelled": app.state.daily.cancel_scan(market_of(market))}

    @app.post("/api/jobs/{market}/resume")
    def resume(market: str):
        """继续: a paused scan goes on from the first rule it had not finished; False: nothing paused, or busy."""
        return {"started": app.state.daily.resume(market_of(market))}

    def started(go) -> dict:
        try:
            ok = go()
        except ValueError as e:   # demo data: never fetched into
            raise HTTPException(409, str(e)) from None
        if not ok:
            raise HTTPException(409, "这个市场正在更新、扫描或补历史，等它做完再来")
        return {"started": True}

    @app.post("/api/data/{market}/backfill")
    def backfill(market: str, body: dict = Body(...)):
        """补历史 (the CLI's `wb backfill`): from `start` to the last finished day, resumable; `datasets`: only these."""
        market_of(market)
        try:
            start = date.fromisoformat(str(body.get("start")))
        except ValueError:
            raise HTTPException(422, "起始日要写成 YYYY-MM-DD") from None
        if start >= date.today():
            raise HTTPException(422, "起始日要在今天之前")
        datasets = body.get("datasets")
        if datasets is not None and (not isinstance(datasets, list) or not all(isinstance(x, str) for x in datasets)
                                     or not set(datasets) <= set(DATASET_NAME)):
            raise HTTPException(422, f"datasets 只能写这些：{', '.join(DATASET_NAME)}")
        if datasets is not None and "bars_1d" not in datasets and ctx.latest(market) is None:
            raise HTTPException(422, "先补日线：这个市场还没有数据，只补这几项会得到一个没有日线的快照")
        return started(lambda: app.state.daily.backfill_now(market, start, None, datasets))

    @app.post("/api/data/{market}/crosscheck")
    def crosscheck_now(market: str):
        """抽样核对 (the CLI's `wb crosscheck`): 30 stocks × 5 days against an independent source, reported only."""
        return started(lambda: app.state.daily.crosscheck_now(market_of(market)))

    @app.post("/api/data/{market}/probe")
    def probe(market: str):
        """What the data plans cover, asked again now (spec §4.9; the CLI's `wb probe`)."""
        paths = ctx.paths(market_of(market))
        probe_market(connect(paths.app_db, "app"), paths.profile, market)
        return {"ok": True}

    @app.get("/api/today")
    def today():
        """`events`: what changed; `first`: the rules whose scan had nothing earlier to compare with (a first scan) —
        what they found, without a change per stock (every match would be "new": thousands on a whole market)."""
        events, first = [], []
        for m in MARKETS:
            st = app.state.daily.state(m)
            if st["status"] in ("delayed", "error", "interrupted"):
                events.append({"kind": "data", "market": m, "status": st["status"], "text": st["message"], "day": st["update_day"]})
            scans = summary(st)
            if not scans:
                continue
            paths = ctx.paths(m)
            names = dict(ctx.instruments(m).select("listing_id", "name").iter_rows())
            for rule_id, s in scans.items():
                if "error" in s:  # the rule failed in the scan: its reason, where the data problems are
                    events.append({"kind": "data", "market": m, "status": "error", "day": (st["scan_at"] or "")[:10] or None,
                                   "text": f"规则「{s['name']}」扫描出错：{s['error']}"})
                    continue
                if "waiting" in s:  # its data comes later than the bars (spec §4.5): scanned when it comes
                    events.append({"kind": "data", "market": m, "status": "delayed", "day": (st["scan_at"] or "")[:10] or None,
                                   "text": f"规则「{s['name']}」{s['waiting']}，到了再扫"})
                    continue
                if not s.get("previous_run"):
                    first.append({"market": m, "rule_id": rule_id, "rule_name": s["name"], "version": s["version"],
                                  "day": s["day"], "counts": s["counts"]})
                    continue
                prev = run_rows(paths, s["previous_run"])
                changes = compare(prev, run_rows(paths, s["run_id"]), s.get("stages") or [])
                label = stage_names(paths, rule_id)
                for kind in KINDS:
                    for x in changes[kind]:
                        events.append({"kind": kind, "market": m, "listing_id": x["listing_id"], "name": names.get(x["listing_id"]),
                                       "rule_id": rule_id, "rule_name": s["name"], "version": s["version"], "day": s["day"],
                                       "run_id": s["run_id"], "stage": x.get("stage"), "was": x.get("was"),
                                       "stage_label": label.get(x.get("stage"), x.get("stage")), "was_label": label.get(x.get("was"), x.get("was")),
                                       "signal_date": x.get("signal_date"), "from_watch": x.get("from_watch"),
                                       "structure": x.get("structure")})
            day = max((s["day"] for s in scans.values() if "day" in s), default=None)
            for a in Alerts(paths).day(date.fromisoformat(day), m) if day else []:   # the close's verdicts (design 14 盘中)
                if a["status"] in ("confirmed", "revoked"):
                    events.append({"kind": "live", "market": m, "listing_id": a["listing_id"], "name": names.get(a["listing_id"]),
                                   "rule_id": a["key"], "day": a["day"], "verdict": a["status"], "at": a["at"], "price": a["price"],
                                   "text": a["text"], "detail": a["detail"]})
        events.sort(key=lambda e: (ORDER[e["kind"]], e["market"], e.get("rule_id") or "", e.get("listing_id") or ""))
        return {"events": events, "first": first}

    @app.get("/api/screen/{rule_id}")
    def screen_get(rule_id: str, limit: int = 1000, also: str | None = None, also_status: str = "signal",
                   where: list[str] | None = Query(None), industry: str | None = None):
        """The 选股 page: the rule's latest scan in each of its markets, its funnel and the listings that got anywhere.
        `also` (同时满足另一条规则): only the listings another rule's latest scan of the same market found too —
        `also_status` signal (命中) or watch (命中或潜伏); each market says that scan's day and whether it is the same.
        `where` (附加条件, any number): only the listings every formula holds for on that scan's day — any rule, no rule to
        write; each market says, formula by formula, how many it holds for across the market and how many could not be
        read, or why it cannot read it (then the market keeps nothing). `industry` (`l1:名称` / `l2:名称`, from the 市场
        page): only the listings in that industry on each market's scan day — 申万 for A shares, 33 業種 for 日股; a market
        without that industry keeps nothing; a name no market has, or a ref written wrong, is a 422 with the reason."""
        from wb.market import industry as ind
        wheres = [w.strip() for w in where or [] if w.strip()]
        ref = industry.strip() if industry and industry.strip() else None
        if ref:
            try:
                ind.parse(ref, "CN")
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        missing: list[str] = []
        store = Rules(ctx.paths(MARKETS[0]))
        try:
            rule, _, version = store.get(rule_id)
            other = store.get(also)[0] if also else None
        except RuleError as e:
            raise HTTPException(404, str(e)) from None
        if other is not None and also_status not in ALSO_STATUS:
            raise HTTPException(422, "also_status 只能是 signal（命中）或 watch（命中或潜伏）")
        markets, rows, unread = {}, [], {}   # unread: formula → the markets that cannot read it
        for m in MARKETS:
            run = latest_run(ctx.paths(m), rule_id, m) if m in rule.markets else None
            if run is None:
                markets[m] = None
                continue
            ran = store.get(rule_id, run["version"])[0] if run["version"] != version else rule
            stages = stage_labels(ran)
            only = with_ = None
            if other is not None:   # a market the other rule has not scanned keeps nothing
                orun = latest_run(ctx.paths(m), other.id, m) if m in other.markets else None
                only = found_by(ctx.paths(m), orun, ALSO_STATUS[also_status]) if orun else set()
                with_ = {"day": orun["day"] if orun else None, "count": len(only), "same_day": bool(orun) and orun["day"] == run["day"]}
            met = []
            for w in wheres:   # 附加条件: what each holds for, all of them, with 同时满足 too
                try:
                    own = for_market(w, m)
                    if own is None:   # written for the other market only: not cut here
                        met.append({"skip": True})
                        continue
                    hit, unknown = holds(ctx.paths(m), m, own, date.fromisoformat(run["day"]))
                    met.append({"count": len(hit), "unknown": unknown})
                    only = hit if only is None else only & hit
                except Unreadable as e:
                    met.append({"error": str(e)})
                    only = set()
                    unread.setdefault(w, []).append(e)
            if ref:   # 行业 (redesign §6.1): like 同时满足, before the page's first rows are taken
                try:
                    st = Store(ctx.paths(m), m)
                    inside = ind.members(st, st.latest(), m, ref, date.fromisoformat(run["day"]))
                except ValueError as e:
                    inside = set()
                    missing.append(str(e))
                only = inside if only is None else only & inside
            funnel, found, total = screen(ctx.paths(m), m, run, len(stages), limit, only)
            markets[m] = {"run_id": run["id"], "version": run["version"], "day": run["day"], "snapshot_id": run["snapshot_id"],
                          "created_at": run["created_at"], "counts": json.loads(run["counts"]), "stages": stages, "funnel": funnel,
                          "listed": len(found), "total": total, **({"also": with_} if with_ is not None else {}),
                          **({"where": met} if wheres else {})}
            rows += found
        scanned = sum(v is not None for v in markets.values())
        if ref and scanned and len(missing) == scanned:   # no market has it: the name itself
            raise HTTPException(422, f"行业筛选有误（{ref}）：{missing[0]}")
        for w, why in unread.items():   # no market can read it: the formula itself, or its data not come yet
            if len(why) == scanned:
                said = "现在算不了" if isinstance(why[0], NotYet) else "有误"
                raise HTTPException(422, f"附加条件{said}（{WHERE_NAMES.get(w, w)}）：{why[0]}")
        return {"rule": {"id": rule.id, "name": rule.name, "version": version, "markets": list(rule.markets)},
                **({"also": {"id": other.id, "name": other.name, "status": also_status}} if other is not None else {}),
                **({"where": [{"expr": w} for w in wheres]} if wheres else {}),
                **({"industry": {"ref": ref, "level": ref.partition(":")[0], "name": ref.partition(":")[2].strip()}} if ref else {}),
                "stages": stage_labels(rule),
                "ranking": [{"expr": r.expr, "dir": r.dir, **n} for r, n in zip(rule.ranking, rule_names(rule)["ranks"], strict=True)],
                "markets": markets, "rows": rows}

    @app.get("/api/notify")
    def notify():
        """The channels, the outbox, and each market's intraday source — whether it is realtime (盘中提醒 go out only
        from one, spec §7.1, 1.34)."""
        from wb.jobs.live import source_for   # (it imports the daily job, as this module does)
        box = outbox()
        live = {m: source_for(ctx.paths(m), m).name for m in MARKETS}   # TickFlow in place of rt_k once its key is set
        return {"channels": channels(), "stats": box.stats(), "recent": box.recent(30),
                "live": {m: {"name": n, "realtime": REALTIME[n].delay == 0} for m, n in live.items()}}

    @app.get("/api/brief/{market}")
    def brief_preview(market: str):
        """预览: the 开盘前简报 as it would go out now, without the announcements (fetching them takes a minute)."""
        return {"brief": app.state.daily.brief(market_of(market), datetime.now().astimezone(), test=True, announcements=False)}

    def items_now() -> list[dict]:
        """The briefing's items in your order, each named as the briefing names it and with what keeps it from the
        briefing now (a rule deleted or not enabled), if anything."""
        from wb.notify.brief import view_name
        store = Rules(ctx.paths(MARKETS[0]))
        on = {r["id"] for r in store.catalog() if r["enabled"]}
        out = []
        for it in BriefItems(ctx.paths).list():
            try:
                rule = store.get(it["rule_id"])[0]
                other = store.get(it["also"])[0] if it["also"] else None
            except RuleError:
                out.append({**it, "name": it["rule_id"], "markets": [], "problem": "这条规则已经删了"})
                continue
            problem = "没有启用，不在每日扫描里" if rule.id not in on else \
                f"「{other.name}」没有启用，不在每日扫描里" if other is not None and other.id not in on else None
            out.append({**it, "name": view_name(rule.name, it, other), "markets": list(rule.markets), "problem": problem})
        return out

    def items_do(fn) -> list[dict]:
        try:
            fn(BriefItems(ctx.paths))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None
        return items_now()

    @app.get("/api/brief-items")
    def brief_items():
        """开盘前简报's items (2026-10-10): the 选股 views you put into it — a rule, its 附加条件, maybe 同时满足 — listed or
        counted, in your order."""
        return items_now()

    @app.post("/api/brief-items")
    def brief_item_add(body: dict = Body(...)):
        """Put a 选股 view into the briefing (last); refused, 422 with why, when the briefing could not use it."""
        wheres = body.get("wheres") or []
        if not isinstance(wheres, list) or not all(isinstance(w, str) for w in wheres):
            raise HTTPException(422, "wheres 要写附加条件的列表")
        return items_do(lambda b: b.add(str(body.get("rule_id", "")), wheres, also=body.get("also") or None,
                                        also_status=str(body.get("also_status") or "signal"), mode=str(body.get("mode") or "list")))

    @app.put("/api/brief-items/{item_id}")
    def brief_item_mode(item_id: int, mode: str = Body(..., embed=True)):
        return items_do(lambda b: b.set_mode(item_id, mode))

    @app.post("/api/brief-items/{item_id}/move")
    def brief_item_move(item_id: int, step: int = Body(..., embed=True)):
        return items_do(lambda b: b.move(item_id, step))

    @app.delete("/api/brief-items/{item_id}")
    def brief_item_remove(item_id: int):
        return items_do(lambda b: b.remove(item_id))

    @app.post("/api/brief/{market}/send")
    def brief_send(market: str):
        """发一份到飞书: the briefing made now, announcements and all, sent at once (a test: not the day's one)."""
        market = market_of(market)
        webhook = get_secret("feishu_webhook")
        if not webhook:
            raise HTTPException(409, "还没有填飞书机器人的 webhook（设置 → 通知）")
        latest = Store(ctx.paths(market), market).latest()
        if latest is None:   # (the user's 日股 on 测试档 with nothing in it: not demo data)
            raise HTTPException(409, f"{MARKET_NAME[market]}现在的数据档里还没有数据：没有可以发的")
        if latest.note == DEMO:
            raise HTTPException(409, "这里是演示数据：只能预览，不会发到飞书")
        b = app.state.daily.brief(market, datetime.now().astimezone(), test=True)
        if b is None:
            raise HTTPException(409, "还没有可以发的：这个市场没有扫描结果")
        try:
            Feishu(webhook, get_secret("feishu_secret"), transport=getattr(app.state, "feishu_transport", None)).send(brief_card(b))
        except (ValueError, FeishuError) as e:
            raise HTTPException(502, str(e)) from None
        return {"ok": True}

    @app.post("/api/notify/test")
    def notify_test():
        webhook = get_secret("feishu_webhook")
        if not webhook:
            raise HTTPException(409, "还没有填飞书机器人的 webhook（设置 → 通知）")
        try:
            bot = Feishu(webhook, get_secret("feishu_secret"), transport=getattr(app.state, "feishu_transport", None))
            bot.send(cards([{"kind": "test", "title": "通知通道可用", "line": "收到这条，收盘扫描的提醒就会发到这里"}], "形研")[0])
        except (ValueError, FeishuError) as e:
            raise HTTPException(502, str(e)) from None
        return {"ok": True}
