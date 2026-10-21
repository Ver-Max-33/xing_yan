"""规则助手 (spec §6.1–§6.4, §10.2): your description in Chinese → a rule.

- The prompt is built from the registry — the L1 functions, the formula fields, the structure primitives with
  their settings, attributes and events, the words only rules have — and from the template cards (a person's
  own words for each template → its rule) as examples; nobody writes primitive descriptions by hand for it.
- The model answers a small JSON envelope: the whole rule as YAML text, the assumptions it made (each with the
  place in the rule and the options), the phrases it could not compute (each with computable stand-ins), and a
  note. The rule goes through the same loader and compiler as any rule; their reasons go back to the model, at
  most twice, and after that the raw answer comes back with them — never silently dropped. (A JSON Schema for
  every field of a rule is not used: a stage's `with:` is an open object that differs by primitive, which
  strict structured output cannot describe within its limits; the loader checks names just as strictly.)
- Choosing an option or a stand-in changes the rule by the program, without calling AI again.
- An edit sends the current rule and what to change; the whole new rule comes back with a note, and the
  difference is computed by the program."""
import difflib
import hashlib
import re
from functools import cache
from typing import Any

import yaml

from wb.ai.client import LLM
from wb.engine.l1.fields import FIELDS
from wb.engine.l1.spec import REQUIRED
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import PRESSURE, SMALL_BOX_EVENTS, SMALL_BOX_STAGES, STRUCTURES, RuleError, load_rule, load_text
from wb.engine.l3.understand import EVENT, PRESSURE_DOC, RULE_WORDS, understand
from wb.registry import FUNCTIONS, PRIMITIVES, autoload
from wb.workspace.rules import builtin_rules, templates

OUTPUT = {
    "type": "object", "additionalProperties": False, "required": ["rule", "assumptions", "unmapped", "note"],
    "properties": {
        "rule": {"type": "string", "description": "完整的规则，YAML 文字"},
        "assumptions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text", "path", "chosen", "options"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "path": {"type": "string"},
                           "chosen": {"type": "string"},
                           "options": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["value", "label"],
                               "properties": {"value": {"type": "string"}, "label": {"type": "string"}}}}}}},
        "unmapped": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["phrase", "reason", "proxies"],
            "properties": {"phrase": {"type": "string"}, "reason": {"type": "string"},
                           "proxies": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["label", "stage", "where", "expr"],
                               "properties": {"label": {"type": "string"}, "stage": {"type": "string"},
                                              "where": {"type": "string", "enum": ["at", "during", "all", "any"]},
                                              "expr": {"type": "string"}}}}}}},
        "note": {"type": "string", "description": "修改说明（修改时写）；起草时写空字符串"},
    },
}

INTRO = """你是选股工作台的规则助手：把用户用中文描述的选股流程翻译成一条规则（YAML）。计算都由程序完成，你只做翻译。
只用下面目录里有的函数、字段、结构、设置、属性和事件，不要编造名字；描述里没给死的数字一律写成参数（params，给 range 和 step）。
不输出买入、卖出、持有、仓位、目标价、止损价之类的建议：这是选股和研究工具，规则只决定"什么时候出信号"。"""

GRAMMAR = """## 规则的写法（YAML）
id: 英文小写和下划线（例如 shrink_breakout）
name: 中文名
doc: 一两句说明（可选）
markets: [CN, JP]   # A 股、日股；描述只针对一个市场时只写一个
universe: {exclude: [st, listed_lt_120d]}   # 可选：排除 ST、上市不足 N 天；min_avg_amount_20d: {CN: 50000000, JP: 300000000}；
  #   只看某些板块 boards: [创业板, 科创板]、行业 industries: [电子]、题材标签 tags: [锂电]、自选分组 watchlist: 分组名、
  #   指数成分 index: CN:IDX:000300（日股 JP:IDX:TOPIX / TOPIX100 / TOPIX500 / CORE30）（每一项都会缩小范围）
params:   # 条件里用 $名字 引用；tune: true 的参数最多 3 个（校准时会调）；unit 写它读作什么："%"（比例，0.15 读作 15%）、倍、根
  - {id: vol, value: 2.0, range: [1.5, 3.0], step: 0.25, tune: true, unit: 倍}
stages:   # 按时间先后
  - 结构：{id: box, kind: structure, use: BOX, with: {scale: mid}}   # with 写结构的设置，或成立后不变的属性来挑结构
  - 结构事件：{id: up, kind: event, on: box.break_up}
  - 公式事件：{id: t, kind: event, when: "C > MA(C, $n)"}   # 公式成立的那一根
  - 时间窗：{id: rally, kind: window, before: {stage: box, bars: [1, 60]}}   # box 开始前第 1–60 根；after: {stage: x, bars: [a, b]} 是 x 结束后
  # 关系：结构之间 after: {stage: x, gap: [a, b]}；事件 within: {stage: x, bars: N}（x 结束后 N 根以内取第一根成立的）
  # 条件：at（事件那一根）、during（整个阶段的汇总，用汇总函数）、all / any（时间窗里每一根 / 至少一根，写一个公式）
  #   all / any 写成一条公式文字（一个字符串，不是列表）；during 里的条件都要用汇总（MEAN、COUNT、RATIO……把逐根的式子包起来），
  #   逐根判断写在 all / any；时间窗的根数（bars、gap、within）可以写成整数参数（[1, $hold]），描述里没给死的天数就这样写
  #   每个 at / during 条件写成 {id: 英文名, label: 中文名, expr: "公式"}（label 是界面和说明里的叫法，如 突破量）；
  #   和固定数字比的条件再写 unit（RUNUP(H, L) >= 0.2 写 unit: "%"）；时间窗写 label（上涨段、站稳），结构和结构事件不用写
signal_at: up.at   # 出信号的时点（收盘后）：阶段.at / 阶段.end / 阶段.start
watch_stage: box   # 可选：正在这个结构里的股票进潜伏名单
ranking: [{expr: "box.width", dir: asc, label: 箱宽, unit: "%"}]   # 可选；x@阶段 取那个阶段锚点上的值；label 是选股表的列名
plan:   # 可选：交易计划——验证时按它买卖每个信号
  entry: close   # close：信号那天尾盘按收盘价买；open：第二天开盘买；cross：盘中越线买——前一天收盘在潜伏名单里的，当天价格
  #   第一次碰到潜伏等着突破的箱顶（盯盘提醒的线）就按箱顶价买（要有 watch_stage，它的下一个阶段是 on: <结构>.break_up）；
  #   可选 max_gap: 0.03（开盘高出箱顶 3% 以上不追）、confirm_volume: 1.5（越线时量比要够，只在分钟回放里用）
  stop: [{line: "box.false_bottom", label: 假底}, {line: "box.bottom * 0.985", label: 箱底}]   # 失效线：信号那一根上算出的价格，
  #   收盘跌破就卖；几条都写时起作用的是最高那条（箱体类：假底，没有假底用箱底；金品用 g.small_bottom）
  take: [{line: "pressure.bottom", share: 0.5, label: 左侧压力}]   # 可选：目标位，最高价碰到就卖 share（最初仓位的几成，1 是剩下的
  #   全卖；下品的目标用 g.big_top）
  exit: [{when: "C < MA(C, 5)", label: 跌破 5 日线}]   # 卖出条件：持有期间每天收盘时的公式，只按这只股票自己的 K 线算（不能用阶段）
  days: 60   # 最多拿到信号后第几个交易日收盘；可选 stall: 5（连续 5 天不创新高就卖）、fill: open（收盘才知道的卖出第二天开盘成交）
#   不写：验证时第二天开盘买、拿 20 天。候选名单类（养箱、复盘）不写。旧的 judge 写法读进来也是计划，不要和 plan 一起写
公式：+ - * /、> >= < <= == !=、AND OR NOT、括号；结构阶段的属性写 box.top；左侧压力写 pressure.space。"""

MUST_ASK = """## 遇到这些地方必须写进 assumptions 让用户选，不能自己定（箱体类澄清点）
- 级别：大箱、末端小箱（金品），还是两者都要；
- 背景：上横上还是下横上；短箱是否去掉"3–6 个月"；
- 金品：要不要求金品，要哪一类；下品按小箱拐点买（到大箱顶至少 10%）还是按大箱突破买（10% 以内）；
- 破箱回箱：箱体期间允许下破后回箱吗；
- 突破量能：先缩后放，还是箱内已有持续阳量群、突破日量能与近期相当即可；
- 撞顶涨停算不算突破；
- 位置：只要低位箱，还是中继箱也要；
- 突破后确认：信号在突破当天给出，还是等第 2、3 天没跌回突破日收盘价再给；
- 热点：写成条件，还是只在结果里提示（热点不能直接计算，写进 unmapped）。"""

OUTPUT_RULES = """## 只输出 json，格式如下，不要其他文字
{"rule": "完整的规则 YAML 文字", "assumptions": [...], "unmapped": [...], "note": "..."}
- assumptions：你替用户做的每个选择。{"id": "box_scale", "text": "你说的横盘一两个月，按中箱识别", "path": "stages.box.with.scale",
  "chosen": "mid", "options": [{"value": "short", "label": "小箱"}, {"value": "mid", "label": "中箱"}, {"value": "long", "label": "大箱"}]}
  path 指向规则里的位置：stages.<阶段id>.<字段>[.<设置>]、stages.<阶段id>.at.<条件id>.expr、stages.<阶段id>.during.<条件id>.expr、
  stages.<阶段id>.all / any、params.<参数id>.value、universe.<字段>、signal_at、markets（列表里的条目按 id 写，不用下标）；
  chosen 和每个 value 写成 YAML 的值（字符串），程序会把它放进 path 的位置再检查，所以每个选项都要能单独成立。
- unmapped：不能直接计算的说法（例如"主力洗盘"）。{"phrase": "原话", "reason": "为什么算不了", "proxies": [{"label": "用「缩量 + 长下影」近似",
  "stage": "box", "where": "during", "expr": "COUNT(L < MIN(O, C) * 0.97) >= 2"}]}；给 1–2 个可以计算的替代条件，用户也可以选择删掉这个说法。
  替代条件不要写进 rule，由用户决定后程序再加。
- 同一个说法不要既写进 assumptions 又写进 unmapped：能算的写进规则、在 assumptions 里说明你的读法；算不了的才放 unmapped。
- 每个参数都要在规则里用到（用 $参数名 引用），没用到的参数不要写。
- note：修改规则时写修改说明；起草时写空字符串。"""


def _args(f) -> str:
    out = []
    for a in f.args:
        out.append(a.name if a.default is REQUIRED else f"{a.name}={a.default}")
    return ", ".join(out)


def catalog() -> str:
    """The functions, fields, structures and rule-only words, from the registry."""
    autoload("wb.engine.l1.functions")
    autoload("wb.engine.l2")
    lines = ["## 公式函数（每根 K 线上算；只能在规则条件里用的汇总写法见后面）"]
    for name, f in sorted(FUNCTIONS.items()):
        lines.append(f"- {name}({_args(f)})：{f.doc}" if f.args else f"- {name}：{f.doc}（不带括号，直接写名字）")
    lines.append("\n## 字段")
    for f in FIELDS.values():
        only = "" if set(f.markets) == {"CN", "JP"} else f"（只有{'A 股' if f.markets == ('CN',) else '日股'}）"
        lines.append(f"- {f.name}：{f.doc}{only}")
    lines.append("\n## 结构（use: 名字）")
    for use, (prim, table) in STRUCTURES.items():
        p = PRIMITIVES[prim]
        evolving = set(p.tables[table].evolving) | set(p.tables[table].ignore)
        lines.append(f"### {use} — {p.doc}")
        params = [x for x in p.params if not x.name.startswith(("short.", "mid.", "long."))]
        lines.append("设置（写在 with 里）：" + "；".join(
            f"{x.name} {x.doc}，默认 {x.default}" + (f"（{x.lo}–{x.hi}）" if x.lo is not None else "") for x in params))
        if use == "BOX":
            lines.append("级别 scale: short / mid / long（小箱、中箱、大箱，也可以写列表 [mid, long]）；只写一个级别时，min_len、max_len、"
                         "min_top、min_bottom、max_width 指那个级别的（写列表时要写成 mid.max_width 这样）；min_touch: {top: 2, bottom: 2}；"
                         "回箱窗口 ret 不写时小箱、中箱 5 根，大箱 10 根")
        fixed = [f"{k} {v}" for k, v in p.attributes.items() if k not in evolving and k != "status"]  # rules use established ones
        moving = [f"{k} {v}" for k, v in p.attributes.items() if k in evolving]
        lines.append("属性（条件里写 阶段.属性；成立后不变的也能写在 with 里挑结构）：" + "；".join(fixed))
        if moving:
            lines.append("会变的属性（只能写成条件，按当时看到的样子判断）：" + "；".join(moving))
        events = SMALL_BOX_EVENTS if use in SMALL_BOX_STAGES else p.events
        if events:
            lines.append("事件（on: 阶段.事件）：" + "；".join(f"{e} {EVENT.get(e, e)}" for e in events)
                         + ("。金品、箱中箱的事件就是它的小箱的事件；with: {of: 某个 BOX 阶段} 要求它的大箱就是那个阶段的箱体"
                            if use in SMALL_BOX_STAGES else ""))
    lines.append("\n## 只能写在规则条件里的写法")
    lines.append("- 汇总（during 里，对整个阶段，或 of=阶段、last=N、first=N 取区间）：" + "；".join(
        f"{k} {v}" for k, v in RULE_WORDS.items() if k != "BAND_VOL_RATIO"))
    lines.append(f"- BAND_VOL_RATIO(box)：{RULE_WORDS['BAND_VOL_RATIO']}")
    lines.append("- 左侧压力（at 和排序里）：" + "；".join(f"pressure.{k} {PRESSURE_DOC.get(k, k)}" for k in PRESSURE))
    lines.append("- x@阶段：在那个阶段的锚点上取 x（排序里常用）")
    return "\n".join(lines)


def examples() -> str:
    out = ["## 例子（一句话描述 → 规则）"]
    for c in templates():
        _, text = builtin_rules()[c["id"]]
        out.append(f"### {c['describe']}\n```yaml\n{text.strip()}\n```")
    return "\n\n".join(out)


@cache
def system_prompt() -> str:
    return "\n\n".join([INTRO, GRAMMAR, catalog(), MUST_ASK, OUTPUT_RULES, examples()])


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---- changing a rule by the program -------------------------------------------------------------------
def _value(v: Any) -> Any:
    return load_text(v) if isinstance(v, str) else v  # as rules read YAML: `on` stays a word, not a boolean


def _find(items: list, key: str, where: str) -> dict:
    for x in items:
        if isinstance(x, dict) and str(x.get("id")) == key:
            return x
    raise ValueError(f"{where}：没有 {key}")


def _set(data: dict, path: str, value: Any) -> None:
    segs = path.split(".")
    node: Any = data
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        where = ".".join(segs[:i + 1])
        if isinstance(node, list):
            node = _find(node, seg, where)
            if last:
                raise ValueError(f"{path}：要指到一个字段，不能只指到 {seg}")
            continue
        if not isinstance(node, dict):
            raise ValueError(f"{where}：这里不能再往下写")
        if last:
            node[seg] = value
            return
        if seg not in node:
            if seg in ("stages", "params", "at", "during"):
                raise ValueError(f"{where}：规则里没有 {seg}")
            node[seg] = {}
        node = node[seg]


def _get(data: dict, path: str) -> Any:
    """What the rule holds at `path` (as `_set` walks it), None when nothing is written there."""
    node: Any = data
    for seg in path.split("."):
        if isinstance(node, list):
            node = next((x for x in node if isinstance(x, dict) and str(x.get("id")) == seg), None)
        elif isinstance(node, dict):
            node = node.get(seg)
        else:
            return None
    return node


def _by_id(data: Any, path: str) -> str | None:
    """A path written with list indexes (`stages.up.at[0].expr`) as the place it names, by id
    (`stages.up.at.volume.expr`); None when it names nothing."""
    node, out = data, []
    for seg in path.split("."):
        m = re.fullmatch(r"([^\[\]]+)\[(\d+)\]", seg)
        key, idx = (m.group(1), int(m.group(2))) if m else (seg, None)
        if isinstance(node, list):
            node = next((x for x in node if isinstance(x, dict) and str(x.get("id")) == key), None)
        elif isinstance(node, dict):
            node = node.get(key)
        else:
            return None
        out.append(key)
        if idx is not None:
            if not isinstance(node, list) or idx >= len(node) or not isinstance(node[idx], dict) or "id" not in node[idx]:
                return None
            node = node[idx]
            out.append(str(node["id"]))
    return ".".join(out)


def _add(data: dict, proxy: dict) -> None:
    stage = _find(data.get("stages") or [], str(proxy["stage"]), "stages")
    where, expr = proxy["where"], str(proxy["expr"])
    if where in ("at", "during"):
        conds = stage.setdefault(where, [])
        cid = str(proxy.get("id") or f"stand_in_{len(conds) + 1}")   # a name it came with
        conds[:] = [c for c in conds if c.get("id") != cid] + [{"id": cid, "expr": expr}]   # the same one again: updated
    elif where in ("all", "any"):
        stage[where] = f"({stage[where]}) AND ({expr})" if stage.get(where) else expr
    else:
        raise ValueError(f"where 要写 at、during、all 或 any，不是 {where}")


def apply_choices(text: str, changes=(), add=()) -> str:
    """The rule with values set at paths (an assumption's option) and conditions added (an unmapped phrase's
    stand-in): done by the program, no AI. Raises ValueError for a place that is not there; the caller loads
    the result to check it."""
    data = load_text(text)
    if not isinstance(data, dict):
        raise ValueError("规则要写成 key: value 的形式")
    for c in changes:
        _set(data, str(c["path"]), _value(c["value"]))
    for p in add:
        _add(data, p)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120)


def _load(text: str):
    rule = load_rule(text)
    for m in rule.markets:
        Matcher.columns(rule, m)   # formulas compile for every market the rule names
    return rule


def _check(d: Any) -> dict:
    """The model's answer, as any rule is checked; every option and stand-in must apply and load too."""
    if not isinstance(d, dict):
        raise ValueError("要输出一个 JSON 对象")
    for k, t, zh in (("rule", str, "字符串"), ("assumptions", list, "列表"), ("unmapped", list, "列表"), ("note", str, "字符串")):
        if not isinstance(d.get(k), t):
            raise ValueError(f"{k} 要是{zh}")
    try:
        rule = _load(d["rule"])
    except RuleError as e:
        raise ValueError(f"规则读不进来：\n{e}") from None
    errs, data = [], load_text(d["rule"])
    for i, a in enumerate(d["assumptions"]):
        tag = f"assumptions[{i}]（{a.get('id') if isinstance(a, dict) else ''}）"
        if not isinstance(a, dict) or not isinstance(a.get("options"), list) or not a["options"]:
            errs.append(f"{tag}：要写 id、text、path、chosen 和至少一个选项")
            continue
        path = str(a.get("path", ""))
        if re.search(r"\[\d+\]", path):  # a list item by index: say where it is by id, and leave the rest of this card
            fixed = _by_id(data, path)
            errs.append(f"{tag}：path 里列表的条目要按 id 写，不用下标" + (f"：应写成 {fixed}" if fixed else "（例如 stages.box.during.<条件id>.expr）"))
            continue
        # what the card shows as confirmed must be what runs: one of the options, and what the rule holds at path
        chosen, held = _value(a.get("chosen")), _get(data, path)
        if chosen not in [_value(o.get("value")) for o in a["options"] if isinstance(o, dict)]:
            errs.append(f"{tag}：chosen「{a.get('chosen')}」不是选项之一")
        elif held is None:
            errs.append(f"{tag}：规则里 {path} 没有写（或这个位置不存在）；规则要在这里写上 chosen 的值")
        elif held != chosen:
            errs.append(f"{tag}：规则里 {path} 是「{held}」，和 chosen「{a.get('chosen')}」不一致；规则要写成 chosen 的值")
        for o in a["options"]:
            try:
                _load(apply_choices(d["rule"], [{"path": a.get("path", ""), "value": o.get("value")}]))
            except (ValueError, RuleError, yaml.YAMLError, AttributeError) as e:
                errs.append(f"{tag} 的选项 {o.get('value') if isinstance(o, dict) else o}：{e}")
    for i, u in enumerate(d["unmapped"]):
        for p in (u.get("proxies") or []) if isinstance(u, dict) else []:
            try:
                _load(apply_choices(d["rule"], add=[p]))
            except (ValueError, RuleError, KeyError, yaml.YAMLError) as e:
                errs.append(f"unmapped[{i}]（{u.get('phrase')}）的替代「{p.get('label')}」：{e}")
    if errs:
        raise ValueError("\n".join(errs))
    return {**d, "_rule": rule}


def _ask(llm: LLM, user: str, evidence: str) -> dict:
    s = system_prompt()
    a = llm.chat_json("rule_assistant", s, user, OUTPUT, "rule_draft", _check, template_hash=_hash(s),
                      evidence_hash=_hash(evidence), retries=2)
    v = a.value
    return {"rule_text": v["rule"], "markets": list(v["_rule"].markets), "understanding": understand(v["_rule"]), "assumptions": v["assumptions"],
            "unmapped": v["unmapped"], "note": v["note"], "attempts": a.attempts, "call_id": a.call_id, "cost": a.cost,
            "model": a.model, "latency_ms": a.latency_ms}


def draft(llm: LLM, description: str) -> dict:
    """Your description → a checked rule, its reading, the assumptions to confirm and the phrases to decide."""
    user = f"用户的描述：\n{description.strip()}\n\n请起草规则，只输出 json。"
    return _ask(llm, user, description.strip())


def edit(llm: LLM, rule_text: str, instruction: str, labels: str = "") -> dict:
    """The current rule + what to change (+ what you marked on it) → the whole new rule, its note and the difference."""
    user = (f"当前的规则：\n```yaml\n{rule_text.strip()}\n```\n\n用户要求的修改：{instruction.strip()}\n"
            + (f"\n{labels.strip()}\n" if labels.strip() else "")
            + "\n请输出修改后的完整规则（rule），note 写修改说明，只输出 json。")
    out = _ask(llm, user, rule_text.strip() + "\n" + instruction.strip())
    out["diff"] = "".join(difflib.unified_diff((rule_text.strip() + "\n").splitlines(True),
                                               (out["rule_text"].strip() + "\n").splitlines(True), "现在", "修改后"))
    return out
