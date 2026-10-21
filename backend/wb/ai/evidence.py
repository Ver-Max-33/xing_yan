"""证据包 (spec §10.3): the facts about a stock on a day for its research — each with an id the research must cite.
Prices, indicators, structures and rule verdicts as of that day's close (§5.8); reports and announcements published by
that day, that day's included (a research is read that evening and its outcome is counted from the next open, §10.4);
prices in that day's exchange prices; only from the sources whose 发送给 LLM is on (§4.10: a source kept off gives
nothing and is named, so the research can say 资料不足). Derived facts only: rule verdicts with their conditions,
indicator values, structures, reports, announcement titles and excerpts — never a table of raw rows. Deterministic: the same day
gives the same pack, and its hash goes with every call."""
import hashlib
import json
from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.datause import SOURCES, allowed, source_of
from wb.engine.l1.compiler import CompileError, Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate
from wb.engine.l2.more import left_pressure
from wb.engine.l2.run import l2_bars
from wb.engine.l2.spec import compute
from wb.listing import parse_id

# id, formula, how it reads ({v} the value; {p} signed %, {u} unsigned %, {big} a large amount)
FORMULAS = [
    ("px.close", "RAW_C", "收盘 {v:,.2f}（{day}，交易所原始价）"),
    ("px.chg1", "C / REF(C, 1) - 1", "当天涨跌 {p}"),
    ("px.chg20", "C / REF(C, 20) - 1", "近 20 个交易日涨跌 {p}"),
    ("px.chg60", "C / REF(C, 60) - 1", "近 60 个交易日涨跌 {p}"),
    ("px.dist20", "MA_DIST(20)", "收盘离 20 日线 {p}"),
    ("px.dist60", "MA_DIST(60)", "收盘离 60 日线 {p}"),
    ("px.high250", "C / HHV(H, 250) - 1", "收盘离一年内最高价 {p}"),
    ("px.amp40", "AMPLITUDE(40)", "两个月（40 根）振幅 {u}"),
    ("vol.ratio", "VOL_RATIO(5)", "量比（当天量 ÷ 前 5 日均量）{v:.2f}"),
    ("vol.contract", "VOL_CONTRACT(10)", "最近 10 根的均量是再往前 10 根的 {v:.2f} 倍"),
    ("rel.20", "REL_PERF(20)", "20 日相对对照指数 {p}"),
    ("val.pe", "PE", "市盈率 {v:.1f}"),
    ("val.pb", "PB", "市净率 {v:.2f}"),
    ("val.mv", "TOTAL_MV", "总市值 {big}"),
]
SCALE = {"short": "小", "mid": "中", "long": "大"}
PERIOD = {"Q1": "一季报", "H1": "半年报", "Q3": "三季报", "FY": "年报"}


def _p(v: float, signed: bool = True) -> str:
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def _big(v: float, market: str) -> str:
    if market == "JP":
        return f"{v / 1e12:.2f} 兆円" if abs(v) >= 1e12 else f"{v / 1e8:.1f} 億円"
    return f"{v / 1e8:.2f} 亿元" if abs(v) >= 1e8 else f"{v / 1e4:.1f} 万元"


def _fact(fid: str, kind: str, source: str, as_of: date, text: str, value: float | None = None, **more) -> dict:
    return {"id": fid, "kind": kind, "source": source, "as_of": str(as_of), "text": text, "value": value, **more}


def _num(v) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:,.0f}" if abs(v) >= 1e4 else f"{v:.4g}"
    return str(v)


def _formulas(store: Store, snap, market: str, profile: str, lid: str, day: date, src: str) -> list[dict]:
    comp = Compiler(market, available=available_columns(store, snap))
    used = []
    for fid, expr, _ in FORMULAS:
        try:
            comp.add(fid.replace(".", "_"), expr, want="value")
            used.append(fid)
        except CompileError:   # a field this market's data has not got (valuation on the test profile, say)
            continue
    prog = comp.program()
    rows = evaluate(build_frame(store, market, profile, prog.columns, [lid], snap=snap), prog).filter(pl.col("date") <= day)
    if rows.is_empty():
        return []
    last = rows.row(-1, named=True)
    out = []
    for fid, _, how in FORMULAS:
        v = last.get(fid.replace(".", "_")) if fid in used else None
        if v is None or v != v:   # unknown (or NaN): not a fact
            continue
        text = how.format(v=v, p=_p(v), u=_p(v, False), big=_big(v, market), day=last["date"])
        out.append(_fact(fid, fid.split(".")[0], src, last["date"], text, float(v)))
    return out


def _structures(store: Store, snap, market: str, profile: str, lid: str, day: date, src: str) -> list[dict]:
    bars = l2_bars(store, market, profile, [lid], snap=snap).filter(pl.col("date") <= day)
    if bars.height < 20:
        return []
    tables = compute(bars)
    raw = (store.scan("bars_1d", snap).filter((pl.col("listing_id") == lid) & (pl.col("date") <= day))
           .select("date", "adj_factor").collect().sort("date"))
    adj = raw["adj_factor"].fill_null(strategy="forward").fill_null(1.0)[-1] if raw.height else 1.0
    px = lambda v: v / adj  # noqa: E731 - the day's exchange price of an adjusted one
    boxes = tables["BOX"]["boxes"].filter((pl.col("status") == "established") & (pl.col("known_at") <= day))
    if boxes.is_empty():
        return []
    last, out, latest = bars["date"].max(), [], {}
    for scale in ("long", "mid", "short"):   # 宏中微: the latest box of each scale
        part = boxes.filter(pl.col("scale") == scale)
        if part.is_empty():
            continue
        b = latest[scale] = part.sort("start", "known_at").row(-1, named=True)
        ended = b["ended_at"] is not None and b["ended_at"] <= day
        span = f"{b['start']} 起，{'到 ' + str(b['end']) + ' 结束（' + str(b['end_reason']) + '）' if ended else '到这天还没结束'}"
        out.append(_fact(f"box.{scale}", "box", src, b["known_at"],
                         f"最近一个已成立的{SCALE[scale]}箱（{span}）：上沿 {px(b['top']):,.2f}、下沿 {px(b['bottom']):,.2f}"
                         f"（宽 {_p(b['top'] / b['bottom'] - 1, False)}）"
                         + (f"，位置 {b['position']}" if b.get("position") else "") + (f"，背景 {b['context']}" if b.get("context") else ""),
                         px(b["top"])))
    big = latest.get("long") or latest.get("mid")
    gold = tables.get("BOX_DERIVED", {}).get("gold")
    if big and gold is not None and gold.height:
        g = gold.filter((pl.col("big_box_id") == big["box_id"]) & (pl.col("known_at") <= day)
                        & (pl.col("superseded_at").is_null() | (pl.col("superseded_at") > day)))
        if g.height:
            r = g.sort("known_at").row(-1, named=True)
            out.append(_fact("box.gold", "box", src, r["known_at"],
                             f"{SCALE[big['scale']]}箱的金品：{'变异' if r['variant'] else ''}{r['kind']}（{r['known_at']} 成立，"
                             f"{'突破后在原箱顶上方的小箱' if r['kind'] == '天品' else '箱子末端的小箱'}）"))
    p = left_pressure(tables["BOX"]["boxes"].filter(pl.col("scale").is_in(["mid", "long"])), bars,
                      pl.DataFrame({"listing_id": [lid], "date": [last]}), 500)
    if p.height and p["space"][0] is not None:
        s = p.row(0, named=True)
        text = ("上方左侧没有压力箱（一年内）" if s["space"] == float("inf")
                else f"上方左侧压力：最近的压力箱下沿 {px(s['pressure_bottom']):,.2f}，离收盘 {_p(s['space'])}")
        out.append(_fact("box.pressure", "box", src, last, text, None if s["space"] == float("inf") else float(s["space"])))
    return out


def _financials(store: Store, snap, lid: str, day: date, src: str, market: str) -> list[dict]:
    f = (store.scan("financials", snap).filter((pl.col("listing_id") == lid) & (pl.col("ann_date") <= day)).collect()
         if snap.manifest.get("financials") else pl.DataFrame())
    if f.is_empty():
        return []
    latest = f.sort("period_end", "ann_date").unique("period_end", keep="last").sort("period_end", descending=True).head(4)
    out = []
    for r in latest.iter_rows(named=True):
        parts = [f"营收 {_big(r['revenue'], market)}" if r.get("revenue") is not None else None,
                 f"同比 {r['revenue_yoy']:+.1f}%" if r.get("revenue_yoy") is not None else None,
                 f"归母净利 {_big(r['net_profit'], market)}" if r.get("net_profit") is not None else None,
                 f"同比 {r['profit_yoy']:+.1f}%" if r.get("profit_yoy") is not None else None,
                 f"ROE {r['roe']:.2f}%" if r.get("roe") is not None else None]
        label = f"{r['period_end'].year}{r['period_type'] or ''}"
        out.append(_fact(f"fin.{label}", "fin", src, r["ann_date"],
                         f"{r['period_end'].year} {PERIOD.get(r['period_type'], r['period_type'] or '')}（{r['ann_date']} 公告，累计）："
                         + "，".join(x for x in parts if x), r.get("net_profit")))
    return out


def _rules(paths: Paths, lid: str, day: date, src: str) -> list[dict]:
    """Each rule's latest daily scan by the day that signalled the stock or keeps it on the watch list, with the
    conditions it read (the explanation tree, §5.6)."""
    from wb.engine.l3.run import run_rows, runs
    from wb.workspace.rules import Rules
    seen, out = set(), []
    for r in runs(paths, limit=500):   # newest first: each rule's latest daily run up to the day
        if r["mode"] != "date" or not r["day"] or r["day"] > str(day) or r["rule_id"] in seen:
            continue
        seen.add(r["rule_id"])
        try:
            row = run_rows(paths, r["id"]).filter(pl.col("listing_id") == lid)
        except (OSError, ValueError):
            continue
        if row.height and row["status"][0] in ("signal", "watch"):
            x = row.row(0, named=True)
            what = "出信号" if x["status"] == "signal" else f"在潜伏名单里（走到 {x.get('stage') or '—'}）"
            try:
                name = f"「{Rules(paths).get(r['rule_id'], r['version'])[0].name}」"
            except (KeyError, ValueError):
                name = ""
            said = {"pass": "通过", "fail": "没通过", "unknown": "未知"}
            conds = [f"{c['expr']}（{said.get(c['status'], c['status'])}）" if c.get("threshold") is None or c.get("value") is None
                     else f"{c['expr']}：{_num(c['value'])}（门槛 {_num(c['threshold'])}，{said.get(c['status'], c['status'])}）"
                     for c in (json.loads(x["detail"]) if x.get("detail") else {}).get("conditions", [])[:8]]
            out.append(_fact(f"rule.{r['rule_id']}", "rule", src, date.fromisoformat(r["day"]),
                             f"规则 {r['rule_id']}{name} v{r['version']} 在 {r['day']} 的扫描里{what}"
                             + (f"；条件：{'；'.join(conds)}" if conds else "")))
    return out


def _announcements(paths: Paths, lid: str, day: date) -> list[dict]:
    """The last 90 days' announcement titles (the newest 30: a busy stock has more), and an excerpt of the newest ones'
    text (not the periodic reports: their figures are the financials')."""
    from wb.docs.announcements import for_text, kept, text_of
    from wb.docs.pdf import excerpt
    rows = kept(paths, lid, day - timedelta(days=90), day)
    out = [_fact(f"ann.{r['doc_id']}", "ann", "cninfo", r["day"], f"{r['day']} 公告：{r['title']}", url=r["url"])
           for r in rows.head(30).iter_rows(named=True)]
    for r in for_text(rows).iter_rows(named=True):
        if e := excerpt(text_of(paths, r["doc_id"]) or ""):
            out.append(_fact(f"doc.{r['doc_id']}", "doc", "cninfo", r["day"], f"《{r['title']}》摘录：{e}", url=r["url"]))
    return out


def _imported(paths: Paths, lid: str) -> list[dict]:
    """Every document you imported for the stock: your excerpt of each (on one line: the prompt is a line a fact), with
    its page."""
    from wb.docs.ir import docs
    return [_fact(f"ir.{d['id']}", "ir", "user", date.fromisoformat(d["added_at"][:10]),
                  f"你导入的资料《{d['title']}》第 {d['page']} 页：{' '.join(d['excerpt'].split())}", doc=d["id"], page=d["page"])
            for d in docs(paths, lid)]


def _notes(paths_of: Callable[[str], Paths], lid: str) -> list[dict]:
    """What you wrote about the stock (its watch entry): yours, always in the pack."""
    from wb.workspace.watch import Watch
    try:
        e = Watch(paths_of).entry(lid)
    except KeyError:
        return []
    out = [_fact(f"note.{k}", "note", "user", date.fromisoformat(e["updated_at"][:10]), f"{label}：{e[k]}")
           for k, label in (("reason", "你写的关注理由"), ("for", "你写的支持"), ("against", "你写的反对 / 风险"), ("note", "你的笔记"))
           if e.get(k)]
    for c in e.get("conditions", []):
        if c["kind"] == "fail" and c.get("price") is not None:
            out.append(_fact(f"note.fail{c['id']}", "note", "user", date.fromisoformat(c["created_at"][:10]),
                             f"你设的失效线：收盘 {c['op']} {c['price']:,.2f}（{c['text']}）", c["price"]))
    return out


def pack(paths_of: Callable[[str], Paths], listing_id: str, day: date | None = None, left_out: list[str] = ()) -> dict:
    """The stock's evidence on `day` (default: the latest day there is data for); `left_out` names more sources the
    research does not have (a fetch that failed)."""
    from wb.workspace.store import Workspace
    market, _ = parse_id(listing_id)
    paths = paths_of(market)
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        raise ValueError(f"{market} 还没有数据")
    day = min(day or snap.as_of, snap.as_of)
    settings, src = Workspace(paths).settings(), source_of(market, paths.profile)
    facts, left = [], list(left_out)
    if allowed(settings, src, "llm"):
        facts += _formulas(store, snap, market, paths.profile, listing_id, day, src)
        facts += _structures(store, snap, market, paths.profile, listing_id, day, src)
        facts += _financials(store, snap, listing_id, day, src, market)
        facts += _rules(paths, listing_id, day, src)
    else:
        left.append(SOURCES[src])
    if market == "CN":
        if allowed(settings, "cninfo", "llm"):
            facts += _announcements(paths, listing_id, day)
        else:
            left.append(SOURCES["cninfo"])
    facts += _imported(paths, listing_id) + _notes(paths_of, listing_id)   # yours: whatever the sources' switches
    names = store.read("instruments", snap).filter(pl.col("listing_id") == listing_id)["name"].to_list()
    body = {"listing_id": listing_id, "name": names[0] if names else None, "market": market, "day": str(day),
            "facts": facts, "left_out": left}
    return {**body, "hash": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]}
