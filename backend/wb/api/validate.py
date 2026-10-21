"""验证 (spec §9): start an event study, a check-up or a single-condition test, follow it, read what it found."""
import json
from datetime import date

from fastapi import Body, FastAPI, HTTPException

from wb.api.context import Context
from wb.api.labels import calibration_inputs
from wb.engine.l3.calibrate import axes as grid_axes
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import RuleError, load_rule
from wb.engine.l1.frame import available_columns
from wb.markets import MARKETS
from wb.validate.backtest import book_settings
from wb.validate.jobs import KINDS, TABLES, Validations, _json, setup, split_day
from wb.validate.portfolio import RATES, Settings

COST_NAMES = {"commission": "佣金", "min_commission": "最低佣金", "transfer": "过户费", "stamp": "印花税", "slippage": "滑点"}
from wb.workspace.rules import Rules


def add_routes(app: FastAPI, ctx: Context) -> None:
    runs = app.state.validations = Validations(ctx.paths, lambda rule_id: calibration_inputs(ctx, rule_id))

    def markets_of(v) -> list[str]:
        ms = [m for m in (v.split(",") if isinstance(v, str) else v or MARKETS) if m]
        if not ms or any(m not in MARKETS for m in ms):
            raise HTTPException(422, "markets 要写 CN、JP")
        return list(dict.fromkeys(ms))

    def day(v, default: date | None) -> date | None:
        if v in (None, ""):
            return default
        try:
            return date.fromisoformat(str(v))
        except ValueError:
            raise HTTPException(422, f"日期要写成 2026-10-01 这样：{v}") from None

    def books(body: dict, markets: list[str]) -> dict[str, Settings]:
        """Each market's book from the run's `book` (positions, exits), `costs` and `cash` by market — refused with the
        reason before it is queued."""
        book = dict(body.get("book") or {})

        def bad(text):
            raise HTTPException(422, text)
        mp = book.get("max_positions", 10)
        if isinstance(mp, bool) or not isinstance(mp, int) or not 1 <= mp <= 50:
            bad("最多持仓要在 1 到 50 只之间")
        hold = book.get("hold", 20)
        if hold is not None and (isinstance(hold, bool) or not isinstance(hold, int) or not 1 <= hold <= 250):
            bad("持有天数要在 1 到 250 个交易日之间（或者不按时间离场）")
        for key, name, hi in (("trail", "移动止损", 1.0), ("target", "止盈", 10.0)):
            v = book.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < hi):
                bad(f"{name}要大于 0、小于 {hi:.0%}")
        if not isinstance(book.get("box_stop", True), bool):
            bad("箱底止损只能开或关")
        if not isinstance(book.get("own", True), bool):
            bad("按规则自己的判断离场只能开或关")
        stall = book.get("stall")
        if stall is not None and (isinstance(stall, bool) or not isinstance(stall, int) or not 1 <= stall <= 250):
            bad("不创新高的天数要在 1 到 250 个交易日之间（或者不用）")
        weak = book.get("weak_positions")
        if weak is not None and (isinstance(weak, bool) or not isinstance(weak, int) or not 0 <= weak <= mp):
            bad(f"大盘弱时最多持仓要在 0 到 {mp} 只之间（不超过平时的最多持仓）")
        if hold is None and not book.get("box_stop", True) and book.get("trail") is None and book.get("target") is None and stall is None:
            bad("至少要有一种离场：持有天数、跌破箱底、移动止损、止盈或 N 天不创新高")
        out = {}
        for m in markets:
            costs = dict((body.get("costs") or {}).get(m) or {})
            for k, v in costs.items():
                hi = 1000.0 if k == "min_commission" else 0.05
                if k not in RATES or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= hi:
                    bad(f"{m} 的{COST_NAMES.get(k, k)}要在 0 到 {'1000' if k == 'min_commission' else '5%'} 之间")
            cash = (body.get("cash") or {}).get(m)
            if cash is not None and (isinstance(cash, bool) or not isinstance(cash, (int, float)) or not 10_000 <= cash <= 1e12):
                bad(f"{m} 的初始资金要在 1 万到 1 万亿之间")
            out[m] = book_settings(m, {**book, "costs": costs, "cash": cash})
        return out

    @app.get("/api/validate/setup")
    def validate_setup(markets: str = "CN,JP"):
        return _json(setup(ctx.paths, markets_of(markets)))

    @app.post("/api/validate")
    def validate_start(body: dict = Body(...)):
        kind = body.get("kind")
        if kind not in KINDS:
            raise HTTPException(422, "kind 要写 event（事件研究）、checkup（条件体检）、single（单条件检验）、backtest（组合回测）或 robust（稳健性检验）")
        rule, version, expr = None, None, None
        if kind == "single":
            expr = str(body.get("expr") or "").strip()
            if not expr:
                raise HTTPException(422, "要写一个条件，例如 CROSS(DIF, DEA) AND DIF > 0")
            markets = markets_of(body.get("markets"))
            text = {"id": "single", "name": "单条件", "markets": markets, "stages": [{"id": "x", "kind": "event", "when": expr}],
                    "signal_at": "x.at"}
            try:
                check = load_rule(json.dumps(text, ensure_ascii=False))
            except RuleError as e:
                raise HTTPException(422, str(e).replace("stages[0].when：", "")) from None
        else:
            try:
                rule, _, version = Rules(ctx.paths(MARKETS[0])).get(str(body.get("rule_id") or ""), body.get("version"))
            except RuleError as e:
                raise HTTPException(404, str(e)) from None
            markets = [m for m in markets_of(body.get("markets") or rule.markets) if m in rule.markets]
            if not markets:
                raise HTTPException(422, f"规则 {rule.id} 只适用于 {'、'.join(rule.markets)}")
            check = rule
        markets = [m for m in markets if ctx.latest(m) is not None]  # a market without data is left out
        if not markets:
            raise HTTPException(422, "这些市场还没有数据")
        s = setup(ctx.paths, markets)
        for m in markets:  # what would not compile is said now, not after the queue
            snap = ctx.latest(m)
            if snap is not None:
                try:
                    Matcher.program(check, m, available_columns(ctx.store(m), snap))
                except RuleError as e:  # a formula on its own: its error without the stage it was put in
                    raise HTTPException(422, f"{m}：{str(e).replace(f'x.when（{expr}）：', '') if expr else e}") from None
        start, end = day(body.get("start"), s["start"]), day(body.get("end"), s["end"])
        if not start < end:
            raise HTTPException(422, "开始日期要早于结束日期")
        oos = split_day(start, end)
        settings = books(body, markets) if kind in ("backtest", "robust") else None
        axes = None
        if kind == "robust" and any(p.tune for p in rule.params):
            try:
                grid = grid_axes(rule)   # the §6.5 grid: what it refuses is said now
            except RuleError as e:
                raise HTTPException(422, str(e)) from None
            axes = list(body.get("axes") or [])[:2] or None
            wrong = [a for a in axes or [] if a not in grid]
            if wrong:
                raise HTTPException(422, f"热力图的参数要从标了 tune 的里选（{'、'.join(grid)}），没有 {'、'.join(map(str, wrong))}")
        return {"id": runs.start(kind, markets, start, end, oos, rule=rule, version=version, expr=expr, settings=settings, axes=axes)}

    def act(do, rid: str) -> dict:
        try:
            runs.get(rid)   # an unknown run is said
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        try:
            return {"ok": do(rid)}
        except ValueError as e:   # 继续: its rule version is gone
            raise HTTPException(409, str(e)) from None

    @app.post("/api/validate/{rid}/pause")
    def validate_pause(rid: str):
        """暂停: the run stops after the chunk under way and keeps what it finished; ok False: it is not running."""
        return act(runs.pause, rid)

    @app.post("/api/validate/{rid}/resume")
    def validate_resume(rid: str):
        """继续 a paused run from what it kept; ok False: it is not paused."""
        return act(runs.resume, rid)

    @app.post("/api/validate/{rid}/cancel")
    def validate_cancel(rid: str):
        """取消: the run stops at its next step (at once when it waits or is paused); nothing of it kept."""
        return act(runs.cancel, rid)

    @app.get("/api/validate")
    def validate_list(rule_id: str | None = None, kind: str | None = None, limit: int = 20):
        return runs.runs(rule_id, kind, min(max(limit, 1), 200))

    @app.get("/api/validate/{rid}")
    def validate_get(rid: str):
        try:
            return runs.get(rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None

    @app.get("/api/validate/{rid}/rows")
    def validate_rows(rid: str):
        try:
            rows = runs.rows(rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None
        names = {}
        for m in rows["market"].unique().to_list():
            inst = ctx.instruments(m).select("listing_id", "name")
            names.update(dict(zip(inst["listing_id"], inst["name"], strict=True)))
        return _json([{**r, "name": names.get(r["listing_id"])} for r in rows.to_dicts()])

    @app.get("/api/validate/{rid}/{table}")   # after /rows: a book's trades, cash ledger or NAV (组合回测)
    def validate_table(rid: str, table: str):
        if table not in TABLES:
            raise HTTPException(404, f"没有 {table}")
        try:
            return _json(runs.table(rid, table).to_dicts())
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None
