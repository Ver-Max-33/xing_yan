"""「我的理解」(spec §6.1): a rule read back in plain Chinese, rendered by the program from each primitive's
and function's own description (the registry) — never written by the AI — so what you confirm is exactly
what runs: where each stage sits, what it binds and with which settings, every condition as written, when
the signal comes, what goes on the watch list, how hits are ranked, and what every word in the formulas
means.

A line is a list of segments: text, or a parameter {"param", "value", "text"} the page shows as a chip you
can change (design 05「点蓝色参数可直接改数值」); "text" is how it reads as plain text in that spot."""
import re
from typing import Any

from wb.engine.l1 import ast as A
from wb.engine.l1.fields import lookup as field
from wb.engine.l1.parser import ParseError, parse
from wb.engine.l3.rule import PRESSURE, STRUCTURES, Plan, Rule, Stage, _exprs, _walk, is_aggregation
from wb.registry import FUNCTIONS, PRIMITIVES, autoload

KIND = {"structure": "结构", "event": "事件", "window": "时间窗"}
STRUCT_NAME = {"BOX": "箱体", "SLANT_BOX": "斜箱", "PIVOTS": "高低点", "SWING": "波段", "MACD_DIV": "MACD 背离",
               "GOLD": "金品", "NEST": "箱中箱", "RELATION": "箱体关系", "FALSE_BOTTOM": "假底"}
SCALE = {"short": "小箱", "mid": "中箱", "long": "大箱"}
EVENT = {"break_up": "向上突破", "break_down": "向下跌破", "spike_up": "刺穿上沿（又收回）", "spike_down": "刺破下沿（又收回）",
         "return": "破箱后回到破箱线以内", "leave_up": "上破离箱", "leave_down": "下破离箱", "limit_hit_top": "涨停撞到箱顶",
         "too_long": "超长结束", "touch_top": "碰到上沿", "touch_bottom": "碰到下沿", "curve_to_flat": "曲箱走平"}
# words only rules have (spec §5.5): aggregations over a stage's bars and functions that read a bound structure
RULE_WORDS = {
    "MEAN": "一段 K 线上的平均值（of=阶段：那个阶段的 K 线；last=N：最近 N 根）", "MAX": "一段 K 线上的最大值",
    "MIN": "一段 K 线上的最小值", "SUM": "一段 K 线上的合计", "COUNT": "一段 K 线里条件成立的根数",
    "RATIO": "一段 K 线里条件成立的根数占比", "FIRST": "一段 K 线的第一根的值", "LAST": "一段 K 线的最后一根的值",
    "CHANGE": "一段 K 线从第一根到最后一根的变化比例", "SLOPE": "一段 K 线上的直线斜率（每根）",
    "RUNUP": "一段 K 线里从此前最低点算起的最大涨幅（写作 RUNUP(H, L)）",
    "DRAWDOWN": "一段 K 线里从此前最高点算起的最大回撤（写作 DRAWDOWN(H, L)）",
    "REL_PERF": "那段时间个股比它的参照指数多涨（或少跌）多少",
    "BAND_VOL_RATIO": "这一根的成交量 ÷ 箱体期间靠近上沿的那些 K 线里最大的成交量",
}
PRESSURE_DOC = {"space": "到左侧压力的箱外空间（比例；上方没有压力时为无穷大）", "bottom": "左侧压力的下沿", "top": "左侧压力的上沿",
                "bars": "左侧压力箱的根数", "heavy_down": "左侧压力箱里放量下跌的根数", "half_year_high": "半年最高价",
                "one_year_high": "一年最高价", "space_half_year": "到半年最高价的空间", "space_one_year": "到一年最高价的空间"}
EXCLUDE = {"st": "ST", "s_share": "未股改（名称以 S 开头）", "suspended": "停牌"}
# `with:` filters by a structure's fixed attributes, as they read
FILTER = {"context": "背景", "position": "位置", "kind": "类别", "variant": "变异", "shape": "形状", "direction": "方向"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
MARKET = {"CN": "A 股", "JP": "日股"}
# the formula's operators as they read (a single "=" is a keyword argument: last=10)
_OPS = [(re.compile(r"\s*>=\s*"), " ≥ "), (re.compile(r"\s*<=\s*"), " ≤ "), (re.compile(r"\s*!=\s*"), " ≠ "),
        (re.compile(r"\s*==\s*"), " = "), (re.compile(r"\bAND\b"), "且"), (re.compile(r"\bOR\b"), "或"),
        (re.compile(r"\bNOT\b"), "非")]

Seg = str | dict
Line = list[Seg]


def _n(v: Any) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.6g}"
    return str(v)


def _in(v: Any, unit: str) -> str:
    """A value in its unit (spec §5.6): % a share ×100 (0.15 → 15%), 倍 a multiple, 根 bars; plain otherwise."""
    if not unit or isinstance(v, bool) or not isinstance(v, (int, float)):
        return _n(v)
    return f"{_n(round(v * 100, 6))}%" if unit == "%" else f"{_n(v)} {unit}"


def _money(v: float, unit: str) -> str:
    if v >= 1e8:
        return f"{v / 1e8:g} 亿{unit}"
    if v >= 1e4:
        return f"{v / 1e4:,.0f} 万{unit}"
    return f"{v:,.0f} {unit}"


def _chip(rule: Rule, pid: str, text: str | None = None) -> dict:
    """A parameter the page shows as a chip, in its unit (it edits in it too: 15 for 15%)."""
    v, unit = rule.param(pid), next((p.unit for p in rule.params if p.id == pid), "")
    return {"param": pid, "value": v, "unit": unit, "text": text if text is not None else f"${pid}（现在 {_in(v, unit)}）"}


def _line(*parts: Seg | Line) -> Line:
    """Flatten parts into one line, joining neighbouring text."""
    out: Line = []
    for p in parts:
        for x in (p if isinstance(p, list) else [p]):
            if isinstance(x, str) and out and isinstance(out[-1], str):
                out[-1] += x
            elif x != "":
                out.append(x)
    return out


def _formula(rule: Rule, text: str) -> Line:
    """A formula as written, its operators as they read, each $parameter a chip."""
    out: Line = []
    for i, piece in enumerate(re.split(r"\$([A-Za-z_]\w*)", text)):
        if i % 2:
            out.append(_chip(rule, piece))
        else:
            for pat, rep in _OPS:
                piece = pat.sub(rep, piece)
            out.append(piece)
    return _line(out)


def _value(rule: Rule, v: Any) -> Seg:
    """A setting's value; a `$param` is a chip; a list reads as its choices."""
    if isinstance(v, list):
        return "或".join(SCALE.get(x, _n(x)) if isinstance(x, str) else _n(x) for x in v)
    return _chip(rule, v[1:]) if isinstance(v, str) and v.startswith("$") else _n(v)


def _scale(v: Any) -> str:
    return "或".join(SCALE.get(x, x) for x in v) if isinstance(v, list) else SCALE.get(v or "", "")


def _span(rule: Rule, bars) -> Line:
    """A number of bars or a range of them; a length that is a parameter is a chip."""
    if bars is None:
        return []
    if isinstance(bars, (tuple, list)):
        return _line(_value(rule, bars[0]), "–", _value(rule, bars[1]), " 根")
    return _line(_value(rule, bars), " 根")


def _place(rule: Rule, s: Stage) -> Line:
    """Where the stage sits against the others."""
    parts: list[Line] = []
    if s.before:
        parts.append(_line(f"在 {s.before.stage} 开始之前 ", _span(rule, s.before.bars), "之内"))
    if s.after:
        g = s.after.gap
        gap = _line(f"（{s.after.stage} 结束后隔 ", _value(rule, g[0]), "–", _value(rule, g[1]), " 根开始）") if g else []
        parts.append(_line(f"在 {s.after.stage} 之后", _line(" ", _span(rule, s.after.bars)) if s.after.bars else [], gap))
    if s.within:
        parts.append(_line(f"在 {s.within.stage} 之后 ", _span(rule, s.within.bars), "之内"))
    out: Line = []
    for k, p in enumerate(parts):
        out = _line(out, "；" if k else "", p)
    return out


def _settings(rule: Rule, s: Stage) -> list[Line]:
    """A structure stage's `with:` settings named by the primitive's own parameter descriptions."""
    if not s.use or s.use not in STRUCTURES:
        return []
    prim = PRIMITIVES[STRUCTURES[s.use][0]]
    docs = {p.name: p.doc for p in prim.params}
    scale = s.with_.get("scale")
    out = []
    for k, v in s.with_.items():
        if k == "scale":
            continue
        if k == "of":
            out.append(_line(f"是 {v} 那个大箱里的"))
        elif k in FILTER and k not in docs:
            out.append(_line(f"{FILTER[k]}：", _value(rule, v)))
        elif k == "min_touch" and isinstance(v, dict):
            sides = [_line(f"{'上沿' if side == 'top' else '下沿'}至少 ", _value(rule, n), " 个触点") for side, n in v.items()]
            out.append(_line(*[x for i, sd in enumerate(sides) for x in (["、"] if i else []) + sd]))
        elif k == "spike" and isinstance(v, dict):
            bits = []
            if "return_within" in v:
                bits.append(_line("当天连续收在箱外最多 ", _value(rule, v["return_within"]), " 根"))
            out.append(_line(*[x for i, b in enumerate(bits) for x in (["、"] if i else []) + b]))
        else:
            one = rule.resolve(scale) if scale is not None else None   # a parameter ($sc) as it runs
            one = one[0] if isinstance(one, list) and one else one
            doc = docs.get(k) or (docs.get(f"{one}.{k}", "").split("：", 1)[-1] if isinstance(one, str) else "") or k
            out.append(_line(f"{doc}{'' if doc.endswith('）') else ' '}= ", _value(rule, v)))
    return out


def _head(rule: Rule, i: int, s: Stage) -> Line:
    num = CIRCLED[i] if i < len(CIRCLED) else f"({i + 1})"
    what: Line = []
    if s.kind == "structure" and s.use:
        name = STRUCT_NAME.get(s.use, s.use)
        doc = PRIMITIVES[STRUCTURES[s.use][0]].doc.split("：", 1)[0] if s.use in STRUCTURES else ""
        sv = s.with_.get("scale")
        tail = f"（{s.use}{'，' + doc if doc and doc != name else ''}）"
        what = (_line(_chip(rule, sv[1:]), tail) if isinstance(sv, str) and sv.startswith("$")   # a parameter: a chip
                else [f"{_scale(sv) or name}{tail}"])
    elif s.kind == "event" and s.on:
        x, ev = s.on.split(".", 1)
        what = [f"{x} {EVENT.get(ev, ev)}的那一根"]
    elif s.kind == "event" and s.when:
        what = _line("「", _formula(rule, s.when), "」成立的那一根")
    place = _place(rule, s)
    name = names(rule)["stages"][s.id]
    head = f"{num} {name}（{s.id}，{KIND[s.kind]}）：" if name != s.id else f"{num} {s.id}（{KIND[s.kind]}）："
    return _line(head, what, "；" if what and place else "", place)


def _glossary(rule: Rule) -> list[str]:
    """What every function, field and stage attribute in the rule's formulas means (from the registry)."""
    autoload("wb.engine.l1.functions")
    stages = {s.id: s for s in rule.stages}
    seen: dict[str, str] = {}
    for _, _, text in _exprs(rule):
        try:
            tree = parse(text)
        except ParseError:
            continue
        for node in _walk(tree):
            if isinstance(node, A.Call):
                name = node.name.upper()
                # an aggregation over a stage's bars, or the L1 function of the same name (COUNT(cond, 60))
                words = is_aggregation(node) or name == "BAND_VOL_RATIO"
                doc = RULE_WORDS.get(name, "") if words else (FUNCTIONS[name].doc if name in FUNCTIONS else "")
                if doc:  # by name and by use: COUNT(C > O) over a stage is not COUNT(cond, 60)
                    seen.setdefault(f"{name}|{words}", f"{name}：{doc.removeprefix(name + '：')}")
            elif isinstance(node, A.Name) and node.name not in stages:
                f, name = field(node.name), node.name.upper()
                if f:
                    seen.setdefault(f.name, f"{f.name}：{f.doc}")
                elif name in FUNCTIONS:  # a function without arguments (LIMIT_UP_STREAK)
                    seen.setdefault(name, f"{name}：{FUNCTIONS[name].doc.removeprefix(name + '：')}")
            elif isinstance(node, A.Attr) and isinstance(node.obj, A.Name):
                obj, attr = node.obj.name, node.name
                if obj == "pressure" and attr in PRESSURE:
                    seen.setdefault(f"pressure.{attr}", f"pressure.{attr}：{PRESSURE_DOC.get(attr, attr)}")
                elif obj in stages and stages[obj].use in STRUCTURES:
                    attrs = PRIMITIVES[STRUCTURES[stages[obj].use][0]].attributes
                    if attr in attrs:
                        seen.setdefault(f"{obj}.{attr}", f"{obj}.{attr}：{STRUCT_NAME.get(stages[obj].use, '')}的{attrs[attr]}")
                elif obj in FUNCTIONS:
                    seen.setdefault(obj, f"{obj}：{FUNCTIONS[obj].doc.removeprefix(obj + '：')}")
    return list(seen.values())


def understand(rule: Rule) -> dict:
    """{"summary": "名称 · 市场 · N 个阶段", "sections": [{"title", "lines": [[segment, …], …]}]} — the rule as it will run."""
    autoload("wb.engine.l2")
    markets = "和".join(MARKET[m] for m in MARKET if m in rule.markets)
    scope = [markets]
    ex = [EXCLUDE.get(x) or (f"上市不足 {rule.universe.listed_days} 天" if x.startswith("listed_lt_")
                             else f"长期停牌复牌不足 {rule.universe.resumed_bars} 根" if x.startswith("resumed_lt_") else x)
          for x in rule.universe.exclude]
    if ex:
        joined = "、".join(ex)
        scope.append(f"排除 {joined}{' ' if joined[-1].isascii() else ''}的股票")
    u = rule.universe
    for label, names in (("只看板块：", u.boards), ("只看行业：", u.industries), ("只看带题材标签：", u.tags)):
        if names:
            scope.append(label + "、".join(names))
    if u.watchlist:
        scope.append(f"只看自选分组「{u.watchlist}」里的股票")
    if u.index:
        scope.append(f"只看指数 {u.index} 的成分股（按成分表那天的下一天起算）")
    if rule.universe.min_avg_amount_20d:
        unit = {"CN": "元", "JP": "日元"}
        scope.append("近 20 日平均成交额：" + "，".join(f"{MARKET[m]}至少 {_money(v, unit[m])}"
                                                    for m, v in rule.universe.min_avg_amount_20d.items() if m in rule.markets))
    def span(p) -> str:   # a parameter's range, in its unit: 8%–30%, 1.5–3 倍
        lo, hi = float(p.range[0]), float(p.range[1])
        return f"{_in(lo, p.unit)}–{_in(hi, p.unit)}" if p.unit == "%" else f"{_n(lo)}–{_in(hi, p.unit)}"

    params = [_line(f"{p.id} = ", _chip(rule, p.id, _in(p.value, p.unit)),
                    (f"（可调 {span(p)}" + (f"，步长 {_in(float(p.step), p.unit)}" if p.step else "")
                     + ("，校准时会调" if p.tune else "") + "）") if p.range else "") for p in rule.params]
    stages: list[Line] = []
    for i, s in enumerate(rule.stages):
        stages.append(_head(rule, i, s))
        settings = _settings(rule, s)
        if settings:
            stages.append(_line("   设置：", *[x for k, st in enumerate(settings) for x in (["；"] if k else []) + st]))
        stages += [_line(f"   期间：{c.label or c.id} — ", _formula(rule, c.expr)) for c in s.during]
        stages += [_line(f"   那一根：{c.label or c.id} — ", _formula(rule, c.expr)) for c in s.at]
        if s.all:
            stages.append(_line("   每一根都满足：", _formula(rule, s.all)))
        if s.any:
            stages.append(_line("   至少一根满足：", _formula(rule, s.any)))
    sid, _, part = rule.signal_at.partition(".")
    when = {"end": f"{sid} 结束的那一根", "start": f"{sid} 开始的那一根", "at": f"{sid} 那一根"}.get(part, rule.signal_at)
    sections = [{"title": "适用", "lines": [[x] for x in scope]}]
    if params:
        sections.append({"title": "参数", "lines": params})
    sections += [{"title": "阶段", "lines": stages}, {"title": "信号", "lines": [[f"{when}收盘后出信号"]]}]
    if rule.watch_stage:
        sections.append({"title": "潜伏名单", "lines": [[f"正在 {rule.watch_stage} 里（还没结束）的股票进潜伏名单，并给出离下一步的距离"]]})
    if rule.plan is not None or rule.judge is not None:   # 交易计划 (design 2026-10-09 §3): how the backtest trades its signals
        sections.append({"title": "交易计划", "lines": [[x] for x in plan_lines(rule.trade_plan) + _cross_skips(rule)]})
    else:
        sections.append({"title": "交易计划", "lines": [["这条规则是候选名单，不定失效线：验证时第二天开盘买，拿 20 个交易日"]]})
    if rule.ranking:
        sections.append({"title": "排序", "lines": [[f"{r.label + '（' + r.expr + '）' if r.label else r.expr} {'从小到大' if r.dir == 'asc' else '从大到小'}"]
                                                   for r in rule.ranking]})
    if rule.reads_pressure:
        sections.append({"title": "左侧压力", "lines": [[f"只算{'、'.join(SCALE[x] for x in rule.pressure.scales)}，往左看 {rule.pressure.lookback} 根"]]})
    glossary = _glossary(rule)
    if glossary:
        sections.append({"title": "公式里用到的", "lines": [[x] for x in glossary]})
    return {"summary": f"{rule.name} · {markets} · {len(rule.stages)} 个阶段", "sections": sections}


def _share(x: float) -> str:
    return "全卖" if x >= 1 else "卖一半" if abs(x - 0.5) < 1e-9 else f"卖 {x:.0%}"


def _cross_skips(rule: Rule) -> list[str]:
    """What a cross entry does not read (design 2026-10-11 §6): the conditions on the bar that breaks the line — at the
    cross its close is not there yet (its volume is the minute replay's volume ratio, confirm_volume)."""
    ids = [s.id for s in rule.stages]
    if rule.trade_plan.entry != "cross" or rule.watch_stage not in ids or ids.index(rule.watch_stage) + 1 >= len(ids):
        return []
    nxt = rule.stages[ids.index(rule.watch_stage) + 1]
    names = [c.label or c.id for c in nxt.at]
    return [f"越线买不看突破那一根上的条件（{'、'.join(names)}）：越线那一刻这根 K 线还没收盘；量要看的话，分钟回放里看量比（confirm_volume）"] if names else []


def plan_lines(p: Plan) -> list[str]:
    """A trade plan sentence by sentence (the rule page's 交易计划, design 2026-10-09 §10)."""
    out = [{"close": "买：信号那天尾盘，按收盘价", "open": "买：信号第二天开盘，按开盘价",
            "cross": "买：前一天收盘在潜伏名单里的，当天价格第一次碰到潜伏等着突破的箱顶（盯盘提醒的那条线）就买，按箱顶价；"
                     "开盘就在箱顶上方的按开盘价；前一天收盘已经站上箱顶的，箱顶算已经越过，第二天不再算"}[p.entry]]
    if p.max_gap is not None:
        out.append(f"开盘价高出箱顶 {p.max_gap * 100:g}% 以上的不追")
    if p.confirm_volume is not None:
        out.append(f"越线时量比要到 {p.confirm_volume:g}（只在分钟回放里用：日线回放不知道越线那一刻的量）")
    for x in p.stop:
        out.append(f"错了走：收盘跌破{x.label or '失效线'}（{x.line}）就{_share(x.share)}")
    for x in p.take:
        out.append(f"赚了卖：最高价碰到{x.label or '目标位'}（{x.line}）就{_share(x.share)}；开盘就在它上面的按开盘价")
    for x in p.exit:
        out.append(f"卖出条件：收盘时{x.label or x.when}（{x.when}）就{_share(x.share)}")
    if p.stall:
        out.append(f"有新高再看高：连续 {p.stall} 个交易日收盘没创买入以来的新高就全卖")
    out.append(f"最多拿到信号后第 {p.days} 个交易日收盘")
    out.append("收盘才知道的卖出：当天尾盘按收盘价成交" if p.fills == "close" else "收盘才知道的卖出：第二天开盘成交")
    if len(p.stop) > 1:
        out.append("几条失效线都是全卖时，起作用的是最高的那条")
    return out


def as_text(u: dict) -> dict:
    """The same with every line as plain text (the CLI, tests, the AI's prompt)."""
    return {**u, "sections": [{**s, "lines": ["".join(x if isinstance(x, str) else x["text"] for x in line) for line in s["lines"]]}
                              for s in u["sections"]]}


MA_OF_CLOSE = ("MA_SLOPE_PCT", "MA_DIST")   # functions whose first argument is a close MA's period …
MA_LISTS = ("MA_ALIGN", "MA_SPREAD")         # … or whose list argument holds them


def price_mas(rule: Rule) -> list[int]:
    """The close MAs a rule reads — MA(C, 20), MA_SLOPE_PCT(60, 5), MA_ALIGN([5, 10, 20]) — that 今日's chart draws."""
    def n(x) -> Any:
        if isinstance(x, A.Num):
            return x.value
        return rule.param(x.name) if isinstance(x, A.Param) else None
    out: set = set()
    for _, _, text in _exprs(rule):
        try:
            tree = parse(text)
        except ParseError:
            continue
        for node in _walk(tree):
            if not isinstance(node, A.Call) or not node.args:
                continue
            name, first = node.name.upper(), node.args[0]
            if name == "MA" and len(node.args) == 2 and isinstance(first, A.Name) and (f := field(first.name)) and f.name == "C":
                out.add(n(node.args[1]))
            elif name in MA_OF_CLOSE:
                out.add(n(first))
            elif name in MA_LISTS and isinstance(first, A.Num) and isinstance(first.value, tuple):
                out.update(x.value if isinstance(x, A.Num) else x for x in first.value)
    return sorted(int(x) for x in out if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 and float(x).is_integer())


def _unit_of(rule: Rule, text: str) -> str:
    """The unit a condition's values read in: that of the $parameter it compares with (X >= $vol); none otherwise —
    a parameter inside a computed threshold (MA(V, 5) <= MA(V, 20) * $quiet) says nothing of the values' unit."""
    if re.search(r"\b(?:AND|OR)\b", text, re.IGNORECASE):   # several joined: no one unit
        return ""
    m = re.search(r"(?:>=|<=|==|!=|>|<)\s*\$([A-Za-z_]\w*)\s*$", text)
    p = next((p for p in rule.params if p.id == m.group(1)), None) if m else None
    return p.unit if p else ""


def names(rule: Rule) -> dict:
    """How a rule's parts read (spec §5.6, 1.36) — explanations, 选股, 今日 and 盯盘 read the rule through it, never by
    its ids: each stage by its label, else by what it is (a structure by its name — 金品, 中箱 —, an event on a
    structure by the event — 向上突破 —, a rule's only formula event 「信号」); each condition by its label, else its
    stage's name and its id, with the unit its threshold reads in; each ranking by its label and unit."""
    events = [s for s in rule.stages if s.kind == "event"]
    stages: dict[str, str] = {}
    for s in rule.stages:
        if s.label:
            stages[s.id] = s.label
        elif s.kind == "structure":
            one = s.scale if isinstance(s.scale, str) else None   # a box of one scale reads as it (中箱); several: 箱体
            kind = s.with_.get("kind")   # a 金品 by its kind (变异下品), a MACD 背离 by its side (底背离)
            if s.use == "GOLD" and kind in ("天品", "上品", "下品"):
                stages[s.id] = ("变异" if s.with_.get("variant") is True else "") + kind
            elif s.use == "MACD_DIV" and kind in ("bull", "bear"):
                stages[s.id] = "MACD " + ("底背离" if kind == "bull" else "顶背离")
            else:
                stages[s.id] = (SCALE.get(one) if s.use == "BOX" and one else None) or STRUCT_NAME.get(s.use or "", s.id)
        elif s.kind == "event" and s.on:
            stages[s.id] = EVENT.get(s.on.split(".")[-1], s.id)
        elif s.kind == "event" and len(events) == 1:
            stages[s.id] = "信号"
        else:
            stages[s.id] = s.id
    conds: dict[str, str] = {}
    units: dict[str, str] = {}
    for s in rule.stages:
        for c in (*s.at, *s.during):
            conds[f"{s.id}.{c.id}"] = c.label or f"{stages[s.id]} · {c.id}"
            units[f"{s.id}.{c.id}"] = c.unit or _unit_of(rule, c.expr)
        if s.all or s.any:   # a window's every-bar (any-bar) condition is the window itself
            conds[f"{s.id}.{s.id}"] = stages[s.id]
        if s.when:
            conds[f"{s.id}.when"] = stages[s.id]
            units[f"{s.id}.when"] = _unit_of(rule, s.when)
    return {"stages": stages, "conds": conds, "units": units,
            "ranks": [{"label": r.label or r.expr, "unit": r.unit} for r in rule.ranking]}
