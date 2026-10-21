"""Chinese text for a match result (spec §5.6: explanations are rendered from the result itself, so
every number shown is one the matcher computed)."""
import math

STATUS = {"signal": "信号", "watch": "潜伏", "unknown": "无法判断", "fail": "未通过"}
MARK = {"pass": "✓", "fail": "✗", "unknown": "?"}
OPS = {">": ">", "<": "<", ">=": "≥", "<=": "≤", "==": "=", "!=": "≠"}
SCALE = {"short": "小箱", "mid": "中箱", "long": "大箱", "slant": "斜箱"}   # a box's id is <listing>:<scale>:<n>
EDGE = {"break_up": "向上突破", "break_down": "向下跌破"}
ISSUES = {"ohlc": "开高低收互相矛盾", "volume_negative": "成交量为负", "vwap_range": "成交额÷成交量不在当天高低价之间",
          "limit_range": "收盘价超出当天涨跌停价", "adj_identity": "复权因子对不上"}


def num(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float) and math.isinf(v):  # pressure.space with no box above
        return "∞" if v > 0 else "-∞"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return f"{int(v):,}"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}"


def _binding(b: dict, names: "_Names") -> str:
    parts = str(b["id"]).split(">")[-1].rsplit(":", 2)   # a 金品 / 箱中箱: <big box>><small box>, read by its small box
    scale = SCALE.get(parts[1]) if len(parts) == 3 and parts[2].isdigit() else None   # a box: <listing>:<scale>:<n>
    if not names.on:   # a result kept before names: as it always read
        if b["kind"] == "structure":   # a box reads as its scale (中箱), not as the program's id for it
            return f"{b['stage']}：{scale or b['id']}，{b['start']} 至 {b['end']}，{b['at']} 成立"
        if b["kind"] == "event":
            return f"{b['stage']}：{b['at']}"
        return f"{b['stage']}：{b['start']} 至 {b['end']}" + (f"（{b['bars']} 根）" if b.get("bars") else "")
    label = names.stage(b["stage"])
    if b["kind"] == "structure":   # by name; a box's scale said when the name is not it — never the program's id
        name = f"{label}（{scale}）" if scale and scale != label else label
        return f"{name}：{b['start']} 至 {b['end']}，{b['at']} 成立"
    if b["kind"] == "event":
        return f"{label}：{b['at']}"
    return f"{label}：{b['start']} 至 {b['end']}" + (f"（{b['bars']} 根）" if b.get("bars") else "")


def _expr(e: str) -> str:
    for a in (">=", "<=", "!=", "=="):
        e = e.replace(a, OPS[a])
    return e


def _condition(c: dict, names: "_Names") -> list[str]:
    """✓ / ✗ / ? the condition by name: its measured value against its threshold, in its unit, the formula below."""
    if not names.on:   # a result kept before names: one line, as it always read
        head = f"{MARK[c['status']]} {c['stage']}.{c['id']}：{_expr(c['expr'])}"
        if c["kind"] in ("all", "any"):
            return [f"  {head}（{num(c['value'])}/{num(c['threshold'])} 根满足）"]
        if c["op"]:
            return [f"  {head}（{num(c['value'])} {OPS[c['op']]} {num(c['threshold'])}）"]
        return [f"  {head}"]
    head, u = f"{MARK[c['status']]} {names.cond(c['stage'], c['id'])}", names.unit(c["stage"], c["id"])
    if c["kind"] in ("all", "any"):
        head += f"：{num(c['value'])}/{num(c['threshold'])} 根满足"
    elif c["op"]:
        head += f"：{_in(c['value'], u)} {OPS[c['op']]} {_in(c['threshold'], u)}"
    return [f"  {head}", f"    公式：{_expr(c['expr'])}"]


def _edge(up: bool, close: float, edge: float, line: float) -> str:
    """距上沿 (spec §5.5, one way app-wide): from the close to the box's own edge — the rise (fall) still needed, or by how
    much it is over — with the break line (edge × (1 ± tol)) said beside it; past that line the box decides (待确认).
    The same as the lists (frontend components/rules/distance.ts) and the briefing."""
    name, move, past, word = ("上沿", "涨", "越过", "突破线") if up else ("下沿", "跌", "跌破", "跌破线")
    if (close < edge) if up else (close > edge):
        need = edge / close - 1 if up else 1 - edge / close
        return f"距{name}：收 {num(close)}，{name} {num(edge)}，还要{move} {need:.1%}（{word} {num(line)}，{name}{'之上' if up else '之下'} {abs(line / edge - 1):.1%}）"
    over = close / edge - 1 if up else 1 - close / edge
    head = f"距{name}：收 {num(close)}，已{past}{name} {num(edge)} 有 {over:.1%}"
    if (close >= line) if up else (close <= line):
        return f"{head}，过了{word} {num(line)}，待确认"
    return f"{head}，还没到{word} {num(line)}"


def _in(v, unit: str) -> str:
    """A value in its unit: % a share ×100 (0.184 → 18.4%), 倍 a multiple, 根 bars; plain otherwise."""
    if unit == "%" and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return num(round(v * 100, 6)) + "%"   # 18.4%, a slope's −0.05% too
    if unit in ("倍", "根") and isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{num(v)} {unit}"
    return num(v)


class _Names:
    """A result's names (Matcher.names: how the rule's parts read). A result kept before names were recorded has none:
    it reads by the program's ids, as it always did."""

    def __init__(self, detail: dict):
        n = detail.get("names") or {}
        self.on, self.stages, self.conds, self.units = bool(n), n.get("stages") or {}, n.get("conds") or {}, n.get("units") or {}

    def stage(self, sid):
        return self.stages.get(sid, sid) if sid else sid

    def cond(self, stage: str, cid: str) -> str:
        if not self.on:
            return f"{stage}.{cid}"
        if f"{stage}.{cid}" in self.conds:
            return self.conds[f"{stage}.{cid}"]
        return self.stage(stage) if cid in ("when", stage) else f"{self.stage(stage)} · {cid}"

    def unit(self, stage: str, cid: str) -> str:
        return self.units.get(f"{stage}.{cid}", "")


def render(detail: dict) -> list[str]:
    """Lines for one listing on one day: status, the bound stages, every condition, and the closest
    miss (fail / unknown) or the distances to the next stage (watch)."""
    status = detail["status"]
    head = f"{detail['listing_id']} {detail['date']} {STATUS.get(status, '不在股票池')}"
    names = _Names(detail)
    if status in ("fail", "unknown"):
        if detail["deepest_stage"]:
            head += f"（走到 {names.stage(detail['deepest_stage'])}）"
        elif detail["bindings"]:
            first = names.stage(detail["bindings"][0]["stage"])
            head += f"（{first}{' ' if first.isascii() else ''}就{'没通过' if status == 'fail' else '无法判断'}）"
        else:
            head += "（没有候选结构）"
    lines = [head]
    lines += ["  " + _binding(b, names) for b in detail["bindings"]]
    for c in detail["conditions"]:
        lines += _condition(c, names)
    for r in detail.get("unknown_reasons", []):
        if r["kind"] == "history":
            lines.append(f"  无法判断的原因：窗口要从第一根 K 线之前 {r['bars']} 根开始（历史不够）")
        elif r["kind"] == "young":
            lines.append(f"  无法判断的原因：到这天只有 {r['bars']} 根 K 线，可能比公式要的窗口短")
        else:
            lines.append(f"  无法判断的原因：{r['date']} 数据有问题（{'、'.join(ISSUES.get(x, x) for x in r['issues'])}）")
    miss = detail.get("closest_miss")
    if miss and miss["kind"] in ("all", "any"):
        lines.append(f"  差在：{names.cond(miss['stage'], miss['id'])}，{num(miss['threshold'])} 根里 {num(miss['value'])} 根满足")
    elif miss and names.on and (not miss.get("op") or miss.get("threshold") is None):   # a condition true or false: not a 0
        lines.append(f"  差在：{names.cond(miss['stage'], miss['id'])}（不满足）")
    elif miss:
        u = names.unit(miss["stage"], miss["id"])
        lines.append(f"  差在：{names.cond(miss['stage'], miss['id'])}，现在 {_in(miss['value'], u)}，要 {OPS.get(miss['op'], '')} {_in(miss['threshold'], u)}")
    for d in detail.get("distances", []):
        v, th = d["value"], d["threshold"]
        if not (isinstance(v, float) and isinstance(th, float)):
            gap = ""
        elif d["id"] in ("break_up", "break_down") and isinstance(d.get("edge"), float) and v and d["edge"]:
            lines.append("  " + _edge(d["id"] == "break_up", v, d["edge"], th))
            continue
        elif d["id"] in ("break_up", "break_down"):  # kept before the edge was recorded: measured to the break line
            if (v >= th) if d["id"] == "break_up" else (v <= th):
                lines.append(f"  距离{EDGE[d['id']]}：现在 {num(v)}，过了 {num(th)}，已越线，待确认")
                continue
            gap = f"，还差 {th / v - 1:+.1%}" if v else ""
        else:  # a ratio or count: how much is missing ("量比还差 0.4")
            u = names.unit(d.get("stage", ""), d["id"])
            gap = f"，还差 {_in(th - v, u)}"
            if names.on and d["id"] not in EDGE:
                lines.append(f"  距离{names.cond(d.get('stage', ''), d['id'])}：现在 {_in(v, u)}，要到 {_in(th, u)}{gap}")
                continue
        lines.append(f"  距离{EDGE.get(d['id']) or ' ' + d['id']}：现在 {num(v)}，要到 {num(th)}{gap}")
    return lines
