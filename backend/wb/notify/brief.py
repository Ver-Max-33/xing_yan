"""开盘前简报 (spec §7.1, 1.34; 2026-10-10): one card per market and trading day, sent before the open — what the last
close left for you to look at today: your watched stocks that something happened to, and the 选股 views you put into it
(a rule with its 附加条件 and maybe 同时满足另一条规则, in your order): the new signals of the ones listed, their 潜伏
stocks a few percent under their box top, the counts of the ones counted; and the market in one line.
Composed from what is already stored plus the listed stocks' announcements; written from templates — no LLM, no
trading words."""
import json
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.datause import allowed, quiet, source_of
from wb.db import connect
from wb.engine.l3.rule import RuleError
from wb.engine.l3.run import run_rows
from wb.markets import load_market
from wb.screening.scan import enabled_rules, previous_run
from wb.screening.screen import WHERE_NAMES, Unreadable, for_market, found_by, holds
from wb.workspace.briefing import ALSO, BriefItems
from wb.workspace.rules import Rules
from wb.workspace.store import Workspace
from wb.workspace.watch import ALERTING


def brief_on(settings: dict, market: str, profile: str) -> bool:
    """Whether the market's briefing goes out: as you set it, or ("auto") for a market on the formal profile — a test
    profile's data is only a sample."""
    v = settings.get(f"brief.{market}", "auto")
    return profile == "prod" if v == "auto" else v == "on"


MARKET_NAME = {"CN": "A 股", "JP": "日股"}
WEEKDAY = "一二三四五六日"
STATE = {1: "上升", 0: "震荡", -1: "下降"}                     # INDEX_STATE, as the 市场 page reads it
SCALE = {"short": "小箱", "mid": "中箱", "long": "大箱", "slant": "斜箱"}
LABEL = {"BAND_VOL_RATIO": "突破量比", "VOL_RATIO": "量比", "VOL_MULT": "倍量"}   # a condition's number, by its formula
FOOTER = "完整名单在电脑上的应用「今日」页 · 点股票名在手机上打开行情页"
NONE_CHOSEN = "简报里还没有选股：在选股页设好规则和附加条件，点「放进开盘前简报」"


@dataclass
class RuleDay:
    """One item of the briefing on the data day: its rule's scan, narrowed as its 选股 view is — its rows that
    signalled or are 潜伏, in the run's own order."""
    rule_id: str
    name: str                      # the view by name: the rule, its 附加条件 and 同时满足
    mode: str                      # list / count
    rows: list[dict]               # listing_id, status, signal_date, detail
    signals: int
    prev_signals: int | None       # the scan of the trading day before (None: its first scan, or a view narrowed)
    ranked: bool                   # the rule ranks its hits itself: the run's order is kept
    error: str | None = None       # why it has nothing today, in words


@dataclass
class Inputs:
    market: str
    day: date                      # the session the briefing is for
    data_day: date                 # the close it reads
    expected: date | None          # the close it should read: the trading day before `day`
    problem: str | None = None     # why the expected day's data is not there
    pending: bool = False          # the latest data is not scanned yet (a correction, or a scan you cancelled)
    late: bool = False             # made after the open (补发)
    note: str | None = None        # said first (演示数据 in a preview)
    index: dict | None = None
    breadth: dict | None = None
    rules: list[RuleDay] = field(default_factory=list)
    quotes: dict[str, dict] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    watched: list[dict] = field(default_factory=list)
    chosen: int | None = None      # how many items the briefing has in this market (0: none put in yet)
    announcements: dict[str, list[dict]] | None = None
    ann_problem: str | None = None
    quiet: bool = False            # the market's prices kept out of notifications (§4.10)
    danger: dict[str, list[dict]] = field(default_factory=dict)   # 危险提醒 per listing (design 2026-10-15)


def _price(v: float, market: str) -> str:
    return f"{v:.2f}" if market == "CN" else (f"{v:,.0f}" if v >= 1000 else f"{v:,.1f}")


def _quote(inp: Inputs, lid: str, amount: bool = True) -> list[str]:
    q = inp.quotes.get(lid) or {}
    if inp.quiet or not q.get("close"):
        return []
    out = [f"收 {_price(q['close'], inp.market)}" + (f" {q['close'] / q['pre_close'] - 1:+.1%}" if q.get("pre_close") else "")]
    if amount and q.get("amount"):
        out.append(f"额 {q['amount'] / 1e8:.1f} 亿" + ("日元" if inp.market == "JP" else ""))
    return out


def _key_numbers(detail: dict) -> list[str]:
    """The structure's size (中箱 39 根) and the first condition with a known name (量比 1.72)."""
    out = []
    for b in detail.get("bindings", []):
        parts = str(b.get("id", "")).rsplit(":", 2)
        if b.get("kind") == "structure" and b.get("bars") and len(parts) == 3 and parts[2].isdigit() and parts[1] in SCALE:
            out.append(f"{SCALE[parts[1]]} {b['bars']} 根")
            break
    for c in detail.get("conditions", []):
        fn = next((k for k in LABEL if f"{k}(" in str(c.get("expr", ""))), None)
        if fn and isinstance(c.get("value"), (int, float)):
            out.append(f"{LABEL[fn]} {c['value']:.2f}")
            break
    return out


def _box_top(detail: dict) -> float | None:
    """The box's own upper edge (上沿, `edge`) — not the break line (`threshold`, 上沿 × (1 + tol)); a run kept before the
    edge was recorded has the break line only."""
    x = next((d for d in detail.get("distances", []) if d.get("id") == "break_up"), None)
    top = x and (x.get("edge") if x.get("edge") is not None else x.get("threshold"))
    return float(top) if top is not None else None


def _ann(inp: Inputs, lid: str) -> str | None:
    xs = (inp.announcements or {}).get(lid) or []
    if not xs:
        return None
    return "公告：" + "；".join(x["title"] for x in xs[:2]) + (f" 等 {len(xs)} 篇" if len(xs) > 2 else "")


def _tags(inp: Inputs, lid: str) -> str | None:
    """A listed stock's 危险提醒, by their short names (design 2026-10-15 §3)."""
    ws = inp.danger.get(lid) or []
    return f"危险：{'、'.join(w['label'] for w in ws)}" if ws else None


def _stock(inp: Inputs, lid: str, parts: list) -> dict:
    code = lid.split(":", 1)[1]
    return {"kind": "stock", "listing_id": lid, "name": inp.names.get(lid) or code, "code": code,
            "text": " · ".join(p for p in parts if p)}


def _market_line(inp: Inputs) -> str:
    i, b = inp.index, inp.breadth
    if i is None:
        return "大盘：没有指数数据，算不出来"
    d60, d200 = i.get("dist60"), i.get("dist200")
    where = None if d60 is None or d200 is None else \
        "在 60、200 日线之上" if d60 > 0 and d200 > 0 else "在 60、200 日线之下" if d60 < 0 and d200 < 0 else "在 60、200 日线之间"
    parts = [f"大盘：{i['name']}" + ("" if inp.quiet else f" {i['close']:,.2f}"), where, STATE.get(i.get("state"))]
    if b and b.get("total") and not inp.quiet:
        parts.append(f"上涨 {b['up'] / b['total']:.0%}")
        parts += [f"{k} {b[c]}" for k, c in (("涨停", "limit_up"), ("跌停", "limit_down")) if b.get(c) is not None]
    return " · ".join(p for p in parts if p)


def compose(inp: Inputs, per_rule: int = 3, near_pct: int = 3, near_max: int = 8, empty: bool = True) -> dict | None:
    """The briefing: a title, the lines said first, the sections — 你的观察, 新信号, 快到突破, 选股器 — and a footer.
    None when there is nothing to look at, nothing is wrong, and the empty one is not wanted."""
    m, d = inp.market, inp.data_day
    listing = [r for r in inp.rules if r.mode == "list" and not r.error]
    # 新信号: the data day's signals, a stock once (under its first rule, the others named), each rule's best first
    by_stock: dict[str, list[str]] = {}
    sigs = []
    for r in listing:
        xs = [x for x in r.rows if x["status"] == "signal" and x.get("signal_date") == d]
        if not r.ranked:
            xs.sort(key=lambda x: -((inp.quotes.get(x["listing_id"]) or {}).get("amount") or 0))
        sigs.append((r, xs))
        for x in xs:
            by_stock.setdefault(x["listing_id"], []).append(r.name)
    seen: set[str] = set()
    new_lines: list[dict] = []
    for r, xs in sigs:
        if not xs:
            continue
        new_lines.append({"kind": "group", "text": f"{r.name} · {len(xs)} 只"})
        fresh = [x for x in xs if x["listing_id"] not in seen][:per_rule]
        for x in fresh:
            lid = x["listing_id"]
            also = [n for n in by_stock[lid] if n != r.name]
            new_lines.append(_stock(inp, lid, [_tags(inp, lid), *_quote(inp, lid), *([] if inp.quiet else _key_numbers(x["detail"])),
                                               f"（也是{'、'.join(also)}）" if also else None, _ann(inp, lid)]))
            seen.add(lid)
        if not fresh:
            new_lines.append({"kind": "note", "text": "都已列在上面"})
    if len(by_stock) > len(seen):
        new_lines.append({"kind": "note", "text": f"另有 {len(by_stock) - len(seen)} 只，见应用「今日」"})
    # 快到突破: 潜伏 stocks whose close is under their box's upper edge (上沿, as everywhere) by at most near_pct
    near: dict[str, dict] = {}
    for r in listing:
        for x in r.rows:
            top, q = _box_top(x["detail"]) if x["status"] == "watch" else None, inp.quotes.get(x["listing_id"]) or {}
            if top is None or not q.get("close") or not 0 < top / q["close"] - 1 <= near_pct / 100:
                continue
            n = near.setdefault(x["listing_id"], {"gap": top / q["close"] - 1, "top": top, "close": q["close"], "rules": []})
            if r.name not in n["rules"]:
                n["rules"].append(r.name)
    near_lines = [_stock(inp, lid, [_tags(inp, lid)] + ([] if inp.quiet else [f"收 {_price(n['close'], m)}", f"上沿 {_price(n['top'], m)}（差 {n['gap']:.1%}）"])
                         + ["、".join(n["rules"]), _ann(inp, lid)])
                  for lid, n in sorted(near.items(), key=lambda kv: kv[1]["gap"])[:near_max]]
    # 你的观察: only the ones something happened to
    watch_lines = []
    for w in inp.watched:
        lid, parts = w["listing_id"], []
        hit = [r.name for r, xs in sigs if any(x["listing_id"] == lid for x in xs)]
        if hit:
            parts.append(f"出信号：{'、'.join(hit)}")
        if lid in near:
            parts.append("快到箱顶" + ("" if inp.quiet else f"（箱顶 {_price(near[lid]['top'], m)}，差 {near[lid]['gap']:.1%}）"))
        for f in w["fired"]:
            if f["kind"] == "fail":
                parts.append(f"失效条件：{f['text']}" + ("" if inp.quiet or f.get("close") is None else f"（收 {_price(f['close'], m)}）"))
            else:
                parts.append(f"该复核了：{f['text']}")
        parts += [f"危险提醒：{w['brief']}" for w in inp.danger.get(lid, []) if w.get("new")]
        if (a := _ann(inp, lid)) is not None:
            parts.append(a)
        if parts:
            if hit:
                parts[1:1] = _quote(inp, lid, amount=False)
            watch_lines.append(_stock(inp, lid, parts))
    # 选股器: how many, and how that changed since the trading day before
    counts = []
    for r in inp.rules:
        if r.mode != "count" or r.error:
            continue
        delta = "" if r.prev_signals is None else f"（{r.signals - r.prev_signals:+,}）" if r.signals != r.prev_signals else "（持平）"
        counts.append(f"{r.name} {r.signals:,}{delta}")
    head = [inp.note] if inp.note else []
    head.append(f"数据截至 {d:%m-%d} 收盘" + ("（最新的数据还没扫完，下面是上一次扫描的结果）" if inp.pending else ""))
    head.append(_market_line(inp))
    head += [f"「{r.name}」{r.error}" for r in inp.rules if r.error]
    if inp.chosen == 0:
        head.append(NONE_CHOSEN)
    warning = None
    if inp.expected and d < inp.expected:
        warning = f"{inp.expected:%m-%d} 的数据没取到" + (f"：{inp.problem}" if inp.problem else "") + f"；下面是 {d:%m-%d} 收盘的结果"
    sections = []
    if watch_lines:
        sections.append({"title": f"你的观察（{len(inp.watched)} 只里 {len(watch_lines)} 只有事）", "lines": watch_lines})
    if new_lines:
        sections.append({"title": f"新信号（{d:%m-%d} 收盘，每项最多 {per_rule} 只）", "lines": new_lines})
    if near_lines:
        sections.append({"title": f"快到突破（离上沿 {near_pct}% 以内）", "lines": near_lines})
    if counts:
        sections.append({"title": "选股器", "lines": [{"kind": "text", "text": " · ".join(counts)}]})
    if not (watch_lines or new_lines or near_lines):
        if not empty and warning is None:
            return None
        sections.insert(0, {"title": "今天没有要看的", "lines": [{"kind": "text", "text": "没有新信号，没有快到突破的潜伏，你的观察也没有变化"}]})
    return {"market": m, "day": inp.day.isoformat(),
            "title": f"{MARKET_NAME[m]} · {inp.day:%m-%d} 周{WEEKDAY[inp.day.weekday()]} 开盘前" + ("（补发）" if inp.late else ""),
            "warning": warning, "head": head, "sections": sections,
            "footer": FOOTER + (f" · 公告没取到：{inp.ann_problem}" if inp.ann_problem else ""),
            "line": f"新信号 {len(by_stock)} 只 · 快到突破 {len(near)} 只 · 你的观察 {len(watch_lines)} 只有事"}


def shown(b: dict) -> list[str]:
    """The listings a briefing names, in order, once each."""
    return list(dict.fromkeys(x["listing_id"] for s in b["sections"] for x in s["lines"] if x["kind"] == "stock"))


INDEX = {"CN": "CN:IDX:000001"}   # the market's main index; 日股: its benchmark in the data profile
INDEX_NAME = {"CN:IDX:000001": "上证指数", "JP:IDX:N225": "日经 225", "JP:IDX:TOPIX": "TOPIX"}
ROUTINE = ("H股公告", "证券变动月报表", "付息", "英文版")   # announcements that come every month or year: not listed


def gather(paths: Paths, market: str, day: date, expected: date | None, state: dict, *, late: bool = False,
           note: str | None = None) -> Inputs | None:
    """What the market's briefing reads: each enabled rule's latest scan of the latest snapshot's day (and, for a
    screener, the one of the trading day before); that day's closes; the 市场 page's main index and breadth; your
    watched stocks and the conditions the close fired. None when the market has no data yet."""
    from wb.market.view import market_view   # heavy imports, only when a briefing is made
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        return None
    d, settings = snap.as_of, Workspace(paths).settings()
    shared = connect(paths.shared_db, "shared")
    failed = {k: v.get("error") for k, v in json.loads(state.get("scan_summary") or "{}").items() if v.get("error")}
    rules, chosen = [], 0
    kept, on = Rules(paths), {rule.id for rule, _ in enabled_rules(paths)}
    for it in BriefItems(lambda m: paths).list():
        try:
            rule = kept.get(it["rule_id"])[0]
            other = kept.get(it["also"])[0] if it["also"] else None
        except RuleError:
            rules.append(RuleDay(it["rule_id"], it["rule_id"], it["mode"], [], 0, None, False, error="这条规则已经删了"))
            chosen += 1
            continue
        if market not in rule.markets or (other is not None and market not in other.markets):   # 同时满足 a rule
            continue                                                                    # never scanned here: nothing to list
        chosen += 1
        entry = RuleDay(rule.id, view_name(rule.name, it, other), it["mode"], [], 0, None, bool(rule.ranking))
        rules.append(entry)
        narrowed = bool(it["wheres"] or other)
        run = previous_run(paths, rule.id, market, d + timedelta(days=1))
        if rule.id not in on or (other is not None and other.id not in on):
            entry.error = "没有启用，不在每日扫描里" if rule.id not in on else f"「{other.name}」没有启用，不在每日扫描里"
        elif rule.id in failed:
            entry.error = "扫描出错，见应用「今日」"
        elif run is None or run["day"] != d.isoformat():   # enabled after that day's scan
            entry.error = f"没有 {d:%m-%d} 的扫描"
        if entry.error:
            continue
        try:
            only = _narrow(paths, market, d, it, other)
        except LookupError as e:
            entry.error = str(e)
            continue
        want = ["signal", "watch"] if it["mode"] == "list" else ["signal"]
        if it["mode"] == "list" or narrowed:
            try:
                got = run_rows(paths, run["id"]).filter(pl.col("status").is_in(want))
            except (RuleError, OSError):   # the run's file is gone: said, not the whole briefing
                entry.error = "这次扫描的结果文件不见了，见应用「今日」"
                continue
            if only is not None:
                got = got.filter(pl.col("listing_id").is_in(list(only)))
            entry.rows = [{"listing_id": r["listing_id"], "status": r["status"], "signal_date": r["signal_date"],
                           "detail": json.loads(r["detail"]) if r["detail"] else {}} for r in got.iter_rows(named=True)]
        if narrowed:   # how many after its conditions; the day before would read them all again: not compared
            entry.signals = sum(x["status"] == "signal" for x in entry.rows)
            if it["mode"] == "count":
                entry.rows = []
        else:
            prev = previous_run(paths, rule.id, market, d)
            entry.signals = int(json.loads(run["counts"]).get("signal", 0))
            entry.prev_signals = int(json.loads(prev["counts"]).get("signal", 0)) if prev else None
    watched = [r["listing_id"] for r in shared.execute(
        f"SELECT listing_id FROM watch_entry WHERE status IN ({', '.join('?' * len(ALERTING))}) AND listing_id LIKE ? "
        "ORDER BY rowid", (*ALERTING, f"{market}:%"))]
    fired: dict[str, list[dict]] = {}
    for r in shared.execute("SELECT listing_id, kind, text, fired_value FROM watch_condition WHERE fired_day >= ? "
                            "AND listing_id LIKE ? ORDER BY id", (d.isoformat(), f"{market}:%")):
        fired.setdefault(r["listing_id"], []).append({"kind": r["kind"], "text": r["text"], "close": r["fired_value"]})
    lids = sorted({x["listing_id"] for r in rules for x in r.rows} | set(watched))
    lf = store.scan("bars_1d", snap)
    cols = [c for c in ("pre_close", "amount") if c in lf.collect_schema().names()]   # a test profile may lack them
    bars = lf.filter((pl.col("date") == d) & pl.col("listing_id").is_in(lids)).select("listing_id", "close", *cols).collect()
    quotes = {r["listing_id"]: r for r in bars.iter_rows(named=True)}
    names = dict(store.read("instruments", snap).select("listing_id", "name").iter_rows())
    danger = {}
    if market == "CN":
        try:
            from wb.danger import active
            danger = active(store, lids, d, snap)["warnings"]
        except Exception:  # noqa: BLE001 - the warnings are left out; the briefing still goes
            danger = {}
    index = breadth = None
    try:
        view = market_view(paths, market)
        iid = INDEX.get(market) or load_market(market).benchmark[paths.profile]
        index = next(({**i, "name": INDEX_NAME.get(iid, iid)} for i in view["indexes"] if i["index_id"] == iid), None)
        breadth = next((b for b in view["breadth"] if b["date"] == d.isoformat()), None)
    except Exception:  # noqa: BLE001 - the market line says it cannot be worked out; the rest still goes
        pass
    return Inputs(market=market, day=day, data_day=d, expected=expected, problem=state.get("message"),
                  pending=snap.id != state.get("scanned_snapshot"), late=late, note=note, index=index, breadth=breadth,
                  rules=rules, quotes=quotes, names=names,
                  watched=[{"listing_id": lid, "fired": fired.get(lid, [])} for lid in watched], chosen=chosen,
                  quiet=market in quiet(settings, {market: source_of(market, paths.profile)}), danger=danger)


def view_name(rule_name: str, item: dict, other) -> str:
    """An item by name, as the 选股 page says its view: the rule · its 附加条件 (a ready one by its words) · 同时满足."""
    parts = [rule_name]
    if item["wheres"]:
        parts.append("、".join(WHERE_NAMES.get(w, w) for w in item["wheres"]))
    if other is not None:
        parts.append(f"同时满足「{other.name}」" + ("（命中或潜伏）" if item["also_status"] == "watch" else ""))
    return " · ".join(parts)


def _narrow(paths: Paths, market: str, d: date, item: dict, other) -> set[str] | None:
    """The listings the item's 附加条件 all hold for on the data day and its other rule's scan of that day found too
    (None: nothing narrows it). Raises LookupError, in words, when one of them cannot be read."""
    only = None
    if other is not None:
        orun = previous_run(paths, other.id, market, d + timedelta(days=1))
        if orun is None or orun["day"] != d.isoformat():
            raise LookupError(f"「{other.name}」没有 {d:%m-%d} 的扫描")
        only = found_by(paths, orun, ALSO[item["also_status"]])
    for w in item["wheres"]:
        try:
            own = for_market(w, market)
            if own is None:   # written for the other market only: not cut here
                continue
            hit, _ = holds(paths, market, own, d)
        except Unreadable as e:
            raise LookupError(f"附加条件读不了（{w}）：{e}") from None
        only = hit if only is None else only & hit
    return only


def announce(inp: Inputs, cninfo, lids: list[str], until: date) -> None:
    """The named stocks' announcements from the data day on (cninfo gives a day, no time), routine ones left out; the
    first failure stops it and is said."""
    out: dict[str, list[dict]] = {}
    for lid in lids:
        try:
            rows = cninfo.announcements(lid, inp.data_day, until)
        except Exception as e:  # noqa: BLE001 - a site that does not answer: the footer says so
            inp.ann_problem = f"巨潮连不上（{type(e).__name__}）"
            return
        keep = [{"day": r["day"], "title": r["title"]} for r in rows.iter_rows(named=True)
                if not any(w in r["title"] for w in ROUTINE)]
        if keep:
            out[lid] = keep
    inp.announcements = out


def make_brief(paths: Paths, market: str, day: date, expected: date | None, state: dict, *, late: bool = False,
               note: str | None = None, cninfo=None, per_rule: int = 3, near_pct: int = 3, near_max: int = 8,
               empty: bool = True) -> dict | None:
    """The briefing as it goes out: composed once to know which stocks it names, their announcements fetched (A 股,
    when the announcements may go into notifications), composed again."""
    inp = gather(paths, market, day, expected, state, late=late, note=note)
    if inp is None:
        return None
    opts = {"per_rule": per_rule, "near_pct": near_pct, "near_max": near_max, "empty": empty}
    first = compose(inp, **opts)
    if first is None or cninfo is None or market != "CN":
        return first
    if not allowed(Workspace(paths).settings(), "cninfo", "notify"):
        inp.ann_problem = "公告关了「出现在通知中」（设置 → 数据使用）"
    else:
        announce(inp, cninfo, shown(first), day)
    return compose(inp, **opts)
