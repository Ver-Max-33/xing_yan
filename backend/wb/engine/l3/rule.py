"""Rules as data (spec §5.2): one Pydantic model checks hand-written YAML and AI-written JSON alike,
and the JSON Schema comes from it. Sets the user names (conditions, parameters) are lists of
`{id, ...}`, never free-keyed maps, so a strict JSON Schema can describe them (spec §6.1).

Checking happens in two layers: the model checks shapes and words; `_check` checks meaning —
references to stages and parameters, the structure kinds and their events, the primitive settings
under `with`, and that every expression parses. Errors are Chinese and name the place, e.g.
`stages[0].before.stage：没有叫 boxx 的阶段`."""
import hashlib
import json
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wb.engine.l1 import ast as A
from wb.engine.l1.parser import ParseError, parse
from wb.registry import PRIMITIVES, autoload

Market = Literal["CN", "JP"]

# structure kinds a stage can bind: (primitive, its table)
STRUCTURES = {"BOX": ("BOX", "boxes"), "SLANT_BOX": ("SLANT_BOX", "slant"), "PIVOTS": ("PIVOTS", "pivots"),
              "SWING": ("SWING", "swings"), "MACD_DIV": ("MACD_DIV", "divergences"),
              "GOLD": ("BOX_DERIVED", "gold"), "NEST": ("BOX_DERIVED", "nest"),
              "RELATION": ("BOX_DERIVED", "relations"), "FALSE_BOTTOM": ("BOX_DERIVED", "false_bottoms")}
# aggregations over a stage's bars (spec §5.5 during): name -> positional argument count
AGGREGATIONS = {"MEAN": 1, "MAX": 1, "MIN": 1, "SUM": 1, "COUNT": 1, "RATIO": 1, "CHANGE": 1, "SLOPE": 1,
                "FIRST": 1, "LAST": 1, "RUNUP": 2, "DRAWDOWN": 2,
                "REL_PERF": 0}  # REL_PERF(of=box): the stock against its reference index over the same bars
# functions that read a bound structure (spec §5.5): name -> positional argument count
STRUCTURE_FUNCTIONS = {"BAND_VOL_RATIO": 1}
# `pressure.<attr>`: the left-side pressure on the bar the expression is read (spec §5.4 PRESSURE),
# attribute -> column of wb.engine.l2.more.left_pressure
# a 金品 or 箱中箱 stage's events are its small box's (a 下品 is bought at its small box's turn or break)
SMALL_BOX_STAGES = ("GOLD", "NEST")
SMALL_BOX_EVENTS = ("spike_up", "spike_down", "break_up", "break_down", "return", "leave_up", "leave_down",
                    "limit_hit_top", "too_long", "touch_top", "touch_bottom", "curve_to_flat")
PRESSURE = {"space": "space", "bottom": "pressure_bottom", "top": "pressure_top", "bars": "pressure_bars",
            "heavy_down": "pressure_heavy_down", "half_year_high": "half_year_high", "one_year_high": "one_year_high",
            "space_half_year": "space_half_year", "space_one_year": "space_one_year"}
_ALIASES = {("min_touch", "top"): "{scale}.min_top", ("min_touch", "bottom"): "{scale}.min_bottom",
            ("spike", "return_within"): "spike_return"}
_PER_SCALE = ("min_len", "max_len", "min_top", "min_bottom", "max_width", "ret", "dense")
_ZH = {"missing": "缺少这一项", "extra_forbidden": "不认识的字段", "literal_error": "取值不在允许的范围里",
       "string_type": "要写文字", "int_type": "要写整数", "int_parsing": "要写整数", "float_type": "要写数字",
       "float_parsing": "要写数字", "list_type": "要写成列表", "dict_type": "要写成 key: value", "bool_type": "要写 true / false",
       "tuple_type": "要写成 [a, b]", "too_short": "个数不够", "too_long": "个数太多"}


class RuleError(ValueError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


Unit = Literal["", "%", "倍", "根"]   # how a value reads: % a share shown ×100 (0.2 → 20%), 倍 a multiple, 根 bars


class Param(_M):
    id: str
    value: float | int | bool | str
    range: tuple[float, float] | None = None
    step: float | None = None
    tune: bool = False
    unit: Unit = ""   # a condition comparing with it reads in it too (「涨幅 18.4% ≥ 15%」)


class Cond(_M):
    id: str
    expr: str
    label: str = ""   # how it reads (突破量); its stage's name and its id otherwise
    unit: Unit = ""   # what its value reads in, for a fixed threshold (RUNUP(H, L) >= 0.2: %); else its $parameter's


class Ref(_M):
    stage: str
    # a number of bars, or "$param" naming a whole-number parameter (§6.6: a number left open is a parameter)
    bars: tuple[int | str, int | str] | int | str | None = None   # window [a, b] (before / after); within: N
    gap: tuple[int | str, int | str] | None = None                 # after: the start is a..b bars after the stage's end


class Stage(_M):
    id: str
    kind: Literal["structure", "event", "window"]
    label: str = ""   # how it reads (上涨段); otherwise by what it is (中箱, 金品, 向上突破; a rule's only formula event 信号)
    use: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    on: str | None = None
    when: str | None = None
    before: Ref | None = None
    after: Ref | None = None
    within: Ref | None = None
    at: list[Cond] = []
    during: list[Cond] = []
    all: str | None = None
    any: str | None = None

    @property
    def scale(self) -> str | None:
        return self.with_.get("scale")


class Universe(_M):
    exclude: list[str] = []
    min_avg_amount_20d: dict[Market, float] | None = None
    # §7.1: only these boards / industries / your 题材 tags / one of your watch groups (each option narrows the pool)
    boards: list[str] = []
    industries: list[str] = []
    tags: list[str] = []
    watchlist: str | None = None
    index: str | None = None   # only this index's members (CN:IDX:000300, JP:IDX:TOPIX100 …)

    @property
    def listed_days(self) -> int | None:
        for x in self.exclude:
            m = re.fullmatch(r"listed_lt_(\d+)d", x)
            if m:
                return int(m.group(1))
        return None

    @property
    def resumed_bars(self) -> int | None:
        """`resumed_lt_<N>d`: the first N bars after a long suspension are out of the pool (10-13: a box from before it
        'broken' by the gap the day trading resumed)."""
        for x in self.exclude:
            m = re.fullmatch(r"resumed_lt_(\d+)d", x)
            if m:
                return int(m.group(1))
        return None


class Pressure(_M):
    lookback: int = Field(500, ge=1)   # bars to the left a pressure box may have ended in (about two years)
    # box scales that count as pressure: a few-day box on the way down is fine, months of
    # trapped supply in a big box is not
    scales: list[Literal["short", "mid", "long"]] = Field(default_factory=lambda: ["mid", "long"], min_length=1)


class Rank(_M):
    expr: str
    dir: Literal["asc", "desc"] = "desc"
    label: str = ""   # its column in 选股 (箱宽); the formula otherwise
    unit: Unit = ""


class Judge(_M):
    """How a signal of this rule is judged (spec §9.1 规则自己的判断): from the next day's open, a close under the 失效线
    (`fail`) is a failure, out at the next open; a high reaching the 目标位 (`target`) a success, out there; else out at
    the close of day `days`. Both are prices read on the signal bar, as a ranking is (e.g. g.small_bottom, box.top)."""
    fail: str | None = None
    target: str | None = None
    days: int = Field(20, ge=1, le=250)


class PlanLine(_M):
    """A price read on the signal bar as a ranking is (adjusted): a 失效线 under the price or a 目标位 over it."""
    line: str
    label: str = ""
    share: float = Field(1.0, gt=0, le=1)   # of the first position; 1: all that is left


class PlanExit(_M):
    """A formula read at each holding day's close on the stock's own bars (C < MA(C, 5))."""
    when: str
    label: str = ""
    share: float = Field(1.0, gt=0, le=1)


class Plan(_M):
    """交易计划 (design 2026-10-09 §3, 2026-10-11 §6): how a signal is bought (`entry`: the signal day's close, the next
    open, or `cross` — during the day after a 潜伏 evening, the first time the price reaches the line the list waits on,
    at the line or an open already over it, not when the open is more than `max_gap` over it; `confirm_volume`: the
    volume ratio a person sees at that moment, read only by the minute replay), where it is wrong (`stop`: a close under
    the line), where to take profit (`take`: a high reaching it), when to sell (`exit`: a formula at the close; `stall`:
    that many closes without a new highest close since entry; `days`: at the close of the days-th trading day after the
    signal) and where a sale decided at a close fills (`fill`; a cross entry's at the close, as 盯盘's 14:50)."""
    entry: Literal["close", "open", "cross"] = "close"
    stop: list[PlanLine] = []
    take: list[PlanLine] = []
    exit: list[PlanExit] = []
    stall: int | None = Field(None, ge=1, le=250)
    days: int = Field(60, ge=1, le=250)
    fill: Literal["close", "open"] | None = None
    max_gap: float | None = Field(None, ge=0, le=0.2)
    confirm_volume: float | None = Field(None, gt=0, le=10)

    @property
    def fills(self) -> str:
        return self.fill or ("open" if self.entry == "open" else "close")


class Rule(_M):
    id: str
    version: int = 1
    name: str
    doc: str = ""
    markets: list[Market]
    universe: Universe = Universe()
    params: list[Param] = []
    stages: list[Stage] = Field(min_length=1)
    signal_at: str
    watch_stage: str | None = None
    ranking: list[Rank] = []
    pressure: Pressure = Pressure()
    judge: Judge | None = None   # how its signals are judged; none: the event study's fixed days
    plan: Plan | None = None     # 交易计划 (design 2026-10-09): replaces judge

    @property
    def trade_plan(self) -> Plan:
        """The plan; `judge` read as one (the next open, its 失效线, its 目标位 all sold, its days, sales at the next
        open); neither: a candidate list — the next open, 20 days."""
        if self.plan is not None:
            return self.plan
        j = self.judge
        if j is None:
            return Plan(entry="open", days=20, fill="open")
        return Plan(entry="open", stop=[PlanLine(line=j.fail)] if j.fail else [], take=[PlanLine(line=j.target)] if j.target else [],
                    days=j.days, fill="open")

    def stage(self, sid: str) -> Stage:
        return next(s for s in self.stages if s.id == sid)

    def param(self, pid: str):
        return next(p.value for p in self.params if p.id == pid)

    @property
    def signal(self) -> tuple[str, str]:
        sid, _, part = self.signal_at.partition(".")
        return sid, part or "end"

    def resolve(self, v):
        """A `$param` reference as its value (nested dicts and lists too)."""
        if isinstance(v, str) and v.startswith("$"):
            return self.param(v[1:])
        if isinstance(v, dict):
            return {k: self.resolve(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.resolve(x) for x in v]
        return v

    def span(self, v):
        """A window's bars or gap, or a within count, with `$param` references read: whole numbers."""
        if v is None:
            return None
        if isinstance(v, (tuple, list)):
            return tuple(int(self.resolve(x)) for x in v)
        return int(self.resolve(v))

    def whole(self, pid: str) -> bool:
        """Whether parameter `pid` takes whole numbers: its value, its step and its range all are (a
        `value: 0` stepped by 0.01 is a fraction)."""
        p = next(x for x in self.params if x.id == pid)
        return (isinstance(p.value, int) and not isinstance(p.value, bool)
                and all(float(x).is_integer() for x in ((p.step,) if p.step is not None else ()) + tuple(p.range or ())))

    def with_params(self, values: dict) -> "Rule":
        """The rule with some parameters set (preview sliders, calibration): each must exist, keep its
        type and stay in its range."""
        known = {p.id: p for p in self.params}
        errs, out = [], {}
        for k, v in values.items():
            p = known.get(k)
            if p is None:
                errs.append(f"params：没有叫 {k} 的参数")
                continue
            number = isinstance(v, (int, float)) and not isinstance(v, bool)
            whole = self.whole(k)
            if isinstance(p.value, bool) or isinstance(p.value, str):
                if type(v) is not type(p.value):
                    errs.append(f"params.{k}：要写{'true / false' if isinstance(p.value, bool) else '文字'}")
                    continue
            elif not number:
                errs.append(f"params.{k}：要写数字")
                continue
            elif whole and not float(v).is_integer():
                errs.append(f"params.{k}：要写整数")
                continue
            elif p.range is not None and not p.range[0] <= v <= p.range[1]:
                errs.append(f"params.{k}：{v} 不在 {p.range[0]} 到 {p.range[1]} 之间")
                continue
            out[k] = v if isinstance(p.value, (bool, str)) else int(v) if whole else float(v)
        if errs:
            raise RuleError("\n".join(errs))
        return self.model_copy(update={"params": [p.model_copy(update={"value": out.get(p.id, p.value)}) for p in self.params]})

    @property
    def reads_pressure(self) -> bool:
        return any(is_pressure(n) for _, _, text in _exprs(self) for n in _walk(parse(text)))

    def primitive_params(self) -> dict[str, dict]:
        """Every structure stage's settings as parameters of the primitives, rule-wide (a GOLD stage
        uses the same BOX as the BOX stage)."""
        out: dict[str, dict] = {}
        for s in self.stages:
            if s.kind == "structure":
                for prim, values in _settings(s, self).items():
                    out.setdefault(prim, {}).update(values)
        return out

    def filters(self, s: Stage) -> dict[str, Any]:
        """Settings under `with` that pick structures by their attributes (e.g. scale: mid)."""
        prim, _ = STRUCTURES[s.use]
        p = PRIMITIVES[prim]
        names = {x.name for x in p.params}
        return {k: self.resolve(v) for k, v in s.with_.items()
                if k not in names and (k, None) not in _ALIASES and not isinstance(v, dict) and k not in _PER_SCALE and k != "of"}


def _settings(s: Stage, rule: Rule) -> dict[str, dict]:
    prim, _ = STRUCTURES[s.use]
    names = {x.name for x in PRIMITIVES[prim].params}
    scale = rule.resolve(s.scale) if s.scale is not None else None   # a parameter ($sc) as it runs; one scale or several
    scales = [x for x in (scale if isinstance(scale, list) else [scale]) if isinstance(x, str)]
    out: dict[str, Any] = {}
    for k, v in s.with_.items():
        v = rule.resolve(v)
        if isinstance(v, dict):
            for kk, vv in v.items():
                alias = _ALIASES[(k, kk)]
                for sc in (scales if "{scale}" in alias else [None]):
                    out[alias.format(scale=sc)] = vv
        elif k in _PER_SCALE and scales:
            out.update({f"{sc}.{k}": v for sc in scales})
        elif k in names:
            out[k] = v
    return {prim: out} if out else {}


def _zh(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(f"[{x}]" if isinstance(x, int) else ("with" if x == "with_" else str(x)) for x in err["loc"])
        what = _ZH.get(err["type"], err["msg"])
        if err["loc"][-1:] in (("all",), ("any",)) and err["type"] == "string_type":   # written like at / during
            what = f"要写成一条公式文字（例如 {err['loc'][-1]}: \"C > box.top\"），不是条件列表；几个条件用 AND 连起来"
        lines.append(f"{loc.replace('.[', '[')}：{what}")
    return "\n".join(lines)


class _Loader(yaml.SafeLoader):
    """YAML with only true / false as booleans: YAML 1.1 reads the key `on:` (and yes / no / off)
    as a boolean, which would silently break `on: box.break_up`."""


_Loader.yaml_implicit_resolvers = {k: [r for r in v if r[0] != "tag:yaml.org,2002:bool"]
                                   for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


def load_text(text: str):
    """Parse YAML or JSON text (JSON is YAML) the way rules are read."""
    return yaml.load(text, Loader=_Loader)  # noqa: S506 — a SafeLoader subclass


def load_rule(text: str) -> Rule:
    """YAML or JSON text -> a checked Rule. Raises RuleError with every problem."""
    try:
        data = load_text(text)
    except yaml.YAMLError as e:
        raise RuleError(f"不是合法的 YAML / JSON：{e}") from None
    if not isinstance(data, dict):
        raise RuleError("规则要写成 key: value 的形式")
    try:
        rule = Rule.model_validate(data)
    except ValidationError as e:
        raise RuleError(_zh(e)) from None
    errors = _check(rule)
    if errors:
        raise RuleError("\n".join(errors))
    return rule


def _exprs(rule: Rule):
    """(place, kind, text) for every expression: kind is at / during / all / any / when / rank."""
    for i, s in enumerate(rule.stages):
        for kind in ("at", "during"):
            for j, c in enumerate(getattr(s, kind)):
                yield f"stages[{i}].{kind}[{j}]（{c.id}）", kind, c.expr
        for kind in ("all", "any", "when"):
            if getattr(s, kind):
                yield f"stages[{i}].{kind}", kind, getattr(s, kind)
    for i, r in enumerate(rule.ranking):
        yield f"ranking[{i}]", "rank", r.expr
    for k in ("fail", "target"):   # read on the signal bar, as a ranking
        if rule.judge and getattr(rule.judge, k):
            yield f"judge.{k}", "rank", getattr(rule.judge, k)
    if rule.plan is not None:
        for k in ("stop", "take"):    # read on the signal bar, as a ranking
            for i, x in enumerate(getattr(rule.plan, k)):
                yield f"plan.{k}[{i}]", "rank", x.line
        for i, x in enumerate(rule.plan.exit):   # each holding day's close, on the stock's own bars
            yield f"plan.exit[{i}]", "exit", x.when


def _walk(n):
    yield n
    for v in vars(n).values():
        if isinstance(v, A.Node):
            yield from _walk(v)
        elif isinstance(v, tuple):
            for x in v:
                if isinstance(x, A.Node):
                    yield from _walk(x)
                elif isinstance(x, tuple):
                    yield from (y for item in x if isinstance(item, A.Node) for y in _walk(item))


def depends(s: Stage) -> str | None:
    """The stage `s` is placed relative to: a structure event's structure, else its before / after /
    within stage. It is bound before `s`."""
    if s.kind == "event" and s.on:
        return s.on.split(".")[0]
    if s.kind == "structure" and isinstance(s.with_.get("of"), str):
        return s.with_["of"]
    ref = s.before or s.after or s.within
    return ref.stage if ref else None


def _relation_errors(at: str, s: Stage, rule: Rule) -> list[str]:
    """Relations that would silently do nothing, and windows / gaps that are empty."""
    errs = []
    if s.before and s.kind != "window":
        errs.append(f"{at}.before：只有窗口能写 before（窗口在某阶段开始之前）")
    if s.within and s.kind != "event":
        errs.append(f"{at}.within：只有事件能写 within（事件在某阶段结束后 N 根以内）")
    if s.after and s.kind == "structure" and s.after.gap is None:
        errs.append(f"{at}.after：结构要写 gap: [a, b]（它的开始距离那个阶段的结束 a 到 b 根）")
    if s.after and s.kind == "event" and s.on and s.after.gap is None:
        errs.append(f"{at}.after：结构事件要写 gap: [a, b]")
    if s.after and s.kind == "event" and s.when and s.after.gap is None and s.after.bars is None:
        errs.append(f"{at}.after：要写 gap: [a, b]（在那个阶段结束后的第 a 到第 b 根里找）")
    pids = {p.id for p in rule.params}

    def num(place: str, v):
        """A number of bars as written, or the whole-number parameter it names; None when it cannot be read."""
        if not isinstance(v, str):
            return v
        pid = v[1:] if v.startswith("$") else v
        if not v.startswith("$") or pid not in pids:
            errs.append(f"{place}：没有叫 {pid} 的参数（根数写数字，或 $参数名）")
            return None
        if not rule.whole(pid):
            errs.append(f"{place}：参数 {pid} 要是整数（它的值、步长和范围都写整数），根数只能是整数")
            return None
        return int(rule.param(pid))

    def pair(place: str, v):
        vals = [num(place, x) for x in (v if isinstance(v, tuple) else (v, v))]
        return None if None in vals else vals

    for rel in ("before", "after"):
        r = getattr(s, rel)
        if r is None:
            continue
        if s.kind == "window" and r.bars is None:
            errs.append(f"{at}.{rel}：窗口要写 bars: [a, b]")
        bars = pair(f"{at}.{rel}.bars", r.bars) if r.bars is not None else None
        if bars and not 1 <= bars[0] <= bars[1]:
            errs.append(f"{at}.{rel}.bars：要写 [a, b]，1 ≤ a ≤ b（第 a 到第 b 根）")
        gap = pair(f"{at}.{rel}.gap", r.gap) if r.gap is not None else None
        if gap and not 0 <= gap[0] <= gap[1]:
            errs.append(f"{at}.{rel}.gap：要写 [a, b]，0 ≤ a ≤ b")
    if s.within is not None:
        w = s.within.bars
        n = None if isinstance(w, tuple) or w is None else num(f"{at}.within.bars", w)
        if isinstance(w, tuple) or w is None or (n is not None and n < 1):
            errs.append(f"{at}.within.bars：要写一个 ≥ 1 的根数")
    return errs


def is_pressure(n) -> bool:
    return isinstance(n, A.Attr) and isinstance(n.obj, A.Name) and n.obj.name == "pressure"


def is_aggregation(n) -> bool:
    return (isinstance(n, A.Call) and n.name.upper() in AGGREGATIONS and len(n.args) == AGGREGATIONS[n.name.upper()])


def _bare_bars(n, stages: set[str]) -> list[str]:
    """Per-bar references in `n` outside any aggregation (fields, L1 calls), for `during`."""
    if is_aggregation(n) or isinstance(n, (A.Num, A.Param)):
        return []
    if isinstance(n, A.Attr) and isinstance(n.obj, A.Name) and (n.obj.name in stages or is_pressure(n)):
        return []
    if isinstance(n, A.Name):
        return [] if n.name in stages else [n.name]
    if isinstance(n, A.Call):
        if n.name.upper() in STRUCTURE_FUNCTIONS:
            return []
        return [n.name]
    if isinstance(n, A.At):
        return []
    out = []
    for v in vars(n).values():
        if isinstance(v, A.Node):
            out += _bare_bars(v, stages)
    return out


def _check(rule: Rule) -> list[str]:
    autoload("wb.engine.l2")
    errs: list[str] = []
    ids = [s.id for s in rule.stages]
    known = set(ids)
    pids = [p.id for p in rule.params]
    for dup in {x for x in ids if ids.count(x) > 1}:
        errs.append(f"stages：阶段 {dup} 重复")
    if "pressure" in known:
        errs.append("stages：pressure 是保留的名字（左侧压力 pressure.space 等），阶段请换一个名字")
    for dup in {x for x in pids if pids.count(x) > 1}:
        errs.append(f"params：参数 {dup} 重复")
    if rule.plan is not None and rule.judge is not None:
        errs.append("plan 和 judge 只写一个（judge 是旧的写法，读进来就是一个交易计划）")

    def ref(place, sid):
        if sid not in known:
            errs.append(f"{place}：没有叫 {sid} 的阶段")

    def params_in(place, v):
        for m in re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", json.dumps(v, ensure_ascii=False)):
            if m not in pids:
                errs.append(f"{place}：没有叫 {m} 的参数")

    u = rule.universe
    for key, names in (("boards", u.boards), ("industries", u.industries), ("tags", u.tags),
                       ("watchlist", [u.watchlist] if u.watchlist is not None else [])):
        for name in names:
            if not name.strip() or '"' in name or "\\" in name:
                errs.append(f"universe.{key}：「{name}」不能是空的，也不能带引号或反斜杠")
    if u.index is not None and not re.fullmatch(r"(CN|JP):IDX:[A-Z0-9]+", u.index):
        errs.append(f"universe.index：「{u.index}」要写成指数编号，例如 CN:IDX:000300、JP:IDX:TOPIX")
    elif u.index is not None and set(rule.markets) != {u.index[:2]}:  # the other market would have no list
        errs.append(f"universe.index：{u.index} 是{' A 股' if u.index[:2] == 'CN' else '日股'}的指数，markets 只能写 {u.index[:2]}")
    for x in rule.universe.exclude:
        if x not in ("st", "s_share", "suspended") and not re.fullmatch(r"(listed|resumed)_lt_(\d+)d", x):
            errs.append(f"universe.exclude：不认识 {x}（可选 st、s_share、suspended、listed_lt_<天数>d、resumed_lt_<根数>d）")
    for i, s in enumerate(rule.stages):
        at = f"stages[{i}]"
        if s.kind == "structure":
            if s.use not in STRUCTURES:
                errs.append(f"{at}.use：不认识的结构 {s.use}（可选 {'、'.join(STRUCTURES)}）")
                continue
            prim, table = STRUCTURES[s.use]
            p = PRIMITIVES[prim]
            names = {x.name for x in p.params}
            changing = set(p.tables[table].evolving) | set(p.tables[table].ignore)
            for k, v in s.with_.items():
                if k == "of":  # 金品 / 箱中箱 of a box another stage binds (its big box)
                    target = next((x for x in rule.stages if x.id == v), None) if isinstance(v, str) else None
                    if s.use not in SMALL_BOX_STAGES:
                        errs.append(f"{at}.with.of：只有金品（GOLD）和箱中箱（NEST）能写 of（它的大箱是哪个阶段）")
                    elif target is None:
                        errs.append(f"{at}.with.of：没有阶段 {v}（写大箱那个结构阶段的 id）")
                    elif target.kind != "structure" or target.use != "BOX":  # a slanted box is never a gold's big box
                        errs.append(f"{at}.with.of：{v} 要是箱体（BOX）结构阶段；金品、箱中箱的大箱是平的箱体，不会是斜箱")
                    elif s.before or s.after:
                        errs.append(f"{at}.with.of：写了 of 就不能再写 before / after（它的位置由那个大箱定）")
                    continue
                if isinstance(v, dict):
                    for kk in v:
                        if (k, kk) not in _ALIASES:
                            errs.append(f"{at}.with.{k}：不认识 {kk}")
                elif k == "status":
                    errs.append(f"{at}.with.status：规则只用已成立的结构，雏形不参与信号（§5.4）")
                elif k in changing and k not in names:
                    errs.append(f"{at}.with.{k}：{k} 在结构成立之后还会变，按它挑结构等于提前知道结果；"
                                f"要判断它请写成条件（例如 at: {s.id}.{k} …），条件按当时看到的样子判断")
                elif k in _PER_SCALE and isinstance(s.scale, list):
                    errs.append(f"{at}.with.{k}：scale 写了几个级别时，{k} 指不到一个级别；写成 {s.scale[0]}.{k} 这样指明级别")
                elif not (k in names or k in p.attributes or k == "scale" or (k in _PER_SCALE and s.scale)):
                    errs.append(f"{at}.with：{s.use} 没有设置 {k}")
            params_in(f"{at}.with", s.with_)
        elif s.kind == "event":
            if bool(s.on) == bool(s.when):
                errs.append(f"{at}：事件要写 on（结构的事件）或 when（公式条件）其中一个")
            if s.on:
                sid, _, ev = s.on.partition(".")
                ref(f"{at}.on", sid)
                if sid in known:
                    src = rule.stage(sid)
                    events = (() if src.kind != "structure" or src.use not in STRUCTURES
                              else SMALL_BOX_EVENTS if src.use in SMALL_BOX_STAGES   # a gold's / a nest's: its small box's
                              else PRIMITIVES[STRUCTURES[src.use][0]].events)
                    if ev not in events:
                        errs.append(f"{at}.on：{sid} 没有事件 {ev}（可选 {'、'.join(events) or '无'}）")
        else:
            if bool(s.before) == bool(s.after):
                errs.append(f"{at}：窗口要写 before 或 after 其中一个")
        for rel in ("before", "after", "within"):
            r = getattr(s, rel)
            if r is not None:
                ref(f"{at}.{rel}.stage", r.stage)
        errs += _relation_errors(at, s, rule)
    left = {s.id: depends(s) for s in rule.stages}
    while left:  # a stage is bound after the one it hangs on: no cycles
        free = [k for k, d in left.items() if d is None or d not in left]
        if not free:
            errs.append("stages：阶段之间的引用成了环：" + "、".join(left))
            break
        for k in free:
            del left[k]
    for place, kind, text in _exprs(rule):
        params_in(place, text)
        try:
            node = parse(text)
        except ParseError as e:
            errs.append(f"{place}：{e}")
            continue
        if kind == "exit" and any(is_pressure(n) or isinstance(n, A.At) or (isinstance(n, A.Name) and n.name in known)
                                  or (isinstance(n, A.Attr) and isinstance(n.obj, A.Name) and n.obj.name in known)
                                  or (isinstance(n, A.Call) and any(k == "of" for k, _ in n.kwargs)) for n in _walk(node)):
            errs.append(f"{place}：卖出条件按这只股票自己的 K 线逐天算，不能用阶段、pressure（{text}）")
            continue
        for n in _walk(node):
            if is_pressure(n):
                if n.name not in PRESSURE:
                    errs.append(f"{place}：pressure 没有 {n.name}（可选 {'、'.join(PRESSURE)}）")
                elif kind not in ("at", "rank"):
                    errs.append(f"{place}：pressure（左侧压力）按某一天计算，只能用在 at 条件和排序里")
            elif isinstance(n, A.Attr) and isinstance(n.obj, A.Name) and n.obj.name.islower() and n.obj.name not in known:
                errs.append(f"{place}：没有叫 {n.obj.name} 的阶段")
            if isinstance(n, A.At) and n.stage not in known:
                errs.append(f"{place}：没有叫 {n.stage} 的阶段")
            if isinstance(n, A.Call):
                for k, v in n.kwargs:
                    if k == "of" and not (isinstance(v, A.Name) and v.name in known):
                        errs.append(f"{place}：of= 要写阶段名")
                if any(k in ("of", "last", "first") for k, _ in n.kwargs) and not is_aggregation(n):
                    errs.append(f"{place}：{n.name} 不能写 of= / last= / first=，只有对阶段的汇总函数可以"
                                f"（{'、'.join(AGGREGATIONS)}）")
        if kind == "during":
            bare = _bare_bars(node, known)
            if bare:
                errs.append(f"{place}：during 是对整段的汇总，{'、'.join(sorted(set(bare)))} 要放进汇总函数"
                            f"（MEAN、COUNT、RATIO……）；逐根判断请用 all / any")
    sid, part = rule.signal
    ref("signal_at", sid)
    if part not in ("start", "end", "at"):
        errs.append("signal_at：要写 <阶段>.end、.start 或 .at")
    if rule.watch_stage:
        ref("watch_stage", rule.watch_stage)
        if rule.watch_stage in known and rule.stage(rule.watch_stage).kind != "structure":
            errs.append(f"watch_stage：{rule.watch_stage} 不是结构阶段（潜伏名单要求结构在 t 时仍然活跃）")
    if rule.plan and rule.plan.entry == "cross":   # the line is the box top the 潜伏 list waits to break (盯盘's line)
        ids = [s.id for s in rule.stages]
        k = ids.index(rule.watch_stage) if rule.watch_stage in ids else None
        nxt = rule.stages[k + 1] if k is not None and k + 1 < len(ids) else None
        if nxt is None or nxt.kind != "event" or not (nxt.on or "").endswith(".break_up"):
            errs.append("plan.entry：越线买（cross）要规则有潜伏阶段（watch_stage），它的下一个阶段是向上突破（on: <结构>.break_up）："
                        "线就是潜伏名单等着突破的那条")
    if not errs:
        seen: dict[tuple, Any] = {}
        for s in rule.stages:
            if s.kind == "structure" and s.use in STRUCTURES:
                for prim, values in _settings(s, rule).items():
                    for k, v in values.items():
                        if seen.setdefault((prim, k), v) != v:
                            errs.append(f"stages：两个阶段给 {prim} 的 {k} 不一样（{seen[(prim, k)]} 和 {v}）")
    # §5.5: nothing written may silently do nothing — a parameter no condition or setting reads would make its
    # slider, its calibration and a clarification card on it change nothing
    used = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)",
                          json.dumps(rule.model_dump(exclude={"params"}, by_alias=True), ensure_ascii=False, default=str)))
    for p in rule.params:
        if p.id not in used:
            errs.append(f"params.{p.id}：参数 {p.id} 在规则里没有用到（写了却不起作用）；用 ${p.id} 引用它，或者删掉")
    return errs


def rule_hash(rule: Rule) -> str:
    """Content hash of the rule (spec §5.9: a published version never changes): the same for YAML and
    JSON, and without the `version:` written in the text — the store numbers versions, so a version
    row and the runs of that version carry the same hash."""
    body = json.dumps(rule.model_dump(mode="json", by_alias=True, exclude={"version"}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def json_schema() -> dict:
    return Rule.model_json_schema(by_alias=True)
