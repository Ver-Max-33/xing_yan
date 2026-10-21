"""Formula compiler (spec §5.3, §5.7): syntax tree -> checked Polars program.

* Every call to a window function ("ts"/"xs" scope) becomes one column, named by a hash of its
  canonical text, so the same sub-expression is computed once however often it appears.
* Element-wise arithmetic, comparisons and logic are inlined into their parent, so they cost no
  extra memory.
* Columns are grouped in levels: a level only reads columns of earlier levels, so cross-sectional
  ranks (`.over("date")`) never nest inside per-security windows (`.over("listing_id")`).
* Unknown stays unknown: windows short of N values give null; comparisons with null give null;
  AND/OR use three-valued logic; division by zero gives null.
* Future-looking 通达信 functions and negative REF are rejected here (spec §5.8 rule 1).
* `TF("W", expr)` compiles `expr` for completed weekly (or monthly) bars; its value becomes
  visible on the period's last trading day (spec §5.3 多周期).
* `lenient` (指标库, the 通达信 way): a condition is 1 / 0 in arithmetic and comparisons, a number is a condition when
  it is not 0, a price may be compared with a fixed number (an indicator is drawn on the prices shown), and mixed units
  make a plain number. Future-looking functions stay rejected. `env`: an indicator's variables, by name."""
import hashlib
from dataclasses import dataclass, field
from functools import cache

import polars as pl

from wb.engine.l1 import ast as A
from wb.engine.l1 import units as UN
from wb.engine.l1.fields import lookup
from wb.engine.l1.functions.base import tie
from wb.engine.l1.parser import parse
from wb.engine.l1.spec import REQUIRED, Fn
from wb.engine.l1.units import U
from wb.registry import FUNCTIONS, autoload

NEEDS = {"idx_state": "指数日线"}  # internal columns some data profiles cannot provide: what is missing

FUTURE = {
    "ZIG": "之字转向", "PEAK": "波峰", "TROUGH": "波谷", "PEAKBARS": "波峰位置", "TROUGHBARS": "波谷位置",
    "BACKSET": "向前赋值", "REFX": "引用未来数据", "REFXV": "引用未来数据", "XMA": "居中移动平均",
    "BARSNEXT": "下一次满足条件",
}
TF_FIELDS = {"open", "high", "low", "close", "volume", "amount", "raw_open", "raw_high", "raw_low",
             "raw_close", "raw_volume"}


@cache
def _load_functions() -> None:
    """Import every function module once (some may already be imported directly, e.g. by charts)."""
    autoload("wb.engine.l1.functions")


def _num_key(v) -> str:
    """Equal numbers written differently (2, 2.0) give the same key, so they share a column."""
    if isinstance(v, tuple):
        return repr(tuple(float(x) for x in v))
    return repr(float(v))


class CompileError(ValueError):
    def __init__(self, msg: str, pos: int | None = None):
        super().__init__(msg if pos is None else f"第 {pos + 1} 个字符：{msg}")
        self.pos = pos


@dataclass
class Val:
    kind: str  # "expr" | "num" | "word" | "multi"
    unit: U | None = None
    expr: pl.Expr | None = None
    num: float | int | tuple | None = None
    word: str | None = None
    level: int = 0  # the first step at which `expr` can be evaluated
    key: str = ""
    outputs: dict = field(default_factory=dict)  # multi: name -> Val
    fn: Fn | None = None


@dataclass
class TfProgram:
    period: str  # "W" | "M"
    program: "Program"
    column: str  # the daily column the result is joined into


@dataclass
class Program:
    steps: list[dict[str, pl.Expr]]
    tf: list[TfProgram]
    outputs: dict[str, tuple[str, U]]  # output name -> (column, unit)
    columns: set[str]  # frame columns the program reads
    xs: bool = False   # a cross-sectional function reads other listings: evaluate on the whole market
    xs_cols: frozenset[str] = frozenset()  # the columns computed across listings (`.over("date")`)


class Ctx:
    """What a function builder may use besides its arguments."""

    def __init__(self, comp: "Compiler"):
        self._c = comp
        self.market = comp.market

    def field(self, name: str) -> pl.Expr:
        """A formula field (C, V ...), checked for this market like a field written in the formula."""
        return self._c.field_expr(name, None).expr

    def column(self, name: str) -> pl.Expr:
        """An internal frame column (limit prices, index close ...) that is not a formula field."""
        if self._c.available is not None and name in NEEDS and name not in self._c.available:
            raise ValueError(f"当前数据档没有{NEEDS[name]}")  # said when the rule is checked, not when it runs
        if self._c.available is not None and name.startswith("in:") and name not in self._c.available:
            raise ValueError(f"当前数据档没有 {name[3:]} 的指数成分（A 股正式档每月月末的成分表；日股 TOPIX 一览）")
        self._c.columns.add(name)
        return pl.col(name)


class Compiler:
    def __init__(self, market: str, params: dict | None = None, available: set[str] | None = None,
                 period: str = "D", lenient: bool = False, env: dict[str, "Val"] | None = None):
        _load_functions()
        self.market, self.params, self.available, self.period = market, params or {}, available, period
        self.lenient = lenient
        self.env: dict[str, Val] = env if env is not None else {}   # NAME (upper case) -> its compiled value
        self.cols: dict[str, tuple[str, pl.Expr, int]] = {}  # key -> (column, expr, level)
        self.xs = False
        self.xs_cols: set[str] = set()
        self.columns: set[str] = set()
        self.touched: set[str] = set()   # fields read since the caller last cleared it (a rule's own formulas)
        self.tfs: dict[str, TfProgram] = {}
        self.outputs: dict[str, tuple[str, U]] = {}

    # ---- public ----------------------------------------------------------------------------
    def add(self, name: str, formula: str | A.Node, want: str | None = None) -> U:
        """Compile `formula` as output `name`. want="cond" requires a condition, "value" a number."""
        node = parse(formula) if isinstance(formula, str) else formula
        v = self.value(self.visit(node), node)
        if want == "cond" and v.unit != U.BOOL:
            raise CompileError(f"这里要的是条件（真/假），而公式的结果是{v.unit.value}")
        if want == "value" and v.unit == U.BOOL:
            raise CompileError("这里要的是数值，而公式的结果是条件")
        if name in self.outputs:
            raise CompileError(f"输出名 {name} 已经用过")
        col = f"out_{name}"
        self._put(col, v.expr if v.kind == "expr" else pl.lit(v.num), v.level)
        self.outputs[name] = (col, v.unit)
        return v.unit

    def compile(self, node: A.Node) -> Val:
        """One formula's value (a statement of an indicator), checked like an output but not made one."""
        return self.value(self.visit(node), node)

    def add_val(self, name: str, v: Val) -> None:
        """Make an already compiled value output `name` (an indicator's line or a drawing's part)."""
        if name in self.outputs:
            raise CompileError(f"输出名 {name} 已经用过")
        col = f"out_{name}"
        self._put(col, v.expr if v.kind == "expr" else pl.lit(v.num, dtype=pl.Float64), v.level)
        self.outputs[name] = (col, v.unit)

    # ---- the 通达信 way (lenient) ----------------------------------------------------------------
    def _num(self, v: Val) -> Val:
        """A condition as 1 / 0 (通达信); anything else as it is."""
        if v.unit != U.BOOL:
            return v
        return Val("expr", U.NUMBER, v.expr.cast(pl.Float64), level=v.level, key=f"NUM({v.key})")

    def _cond(self, v: Val) -> Val:
        """A number as a condition: true when it is not 0 (通达信)."""
        if v.unit == U.BOOL:
            return v
        if v.kind == "num":
            return Val("expr", U.BOOL, pl.lit(bool(v.num)), level=0, key=f"COND({v.key})")
        return Val("expr", U.BOOL, v.expr != 0, level=v.level, key=f"COND({v.key})")

    def program(self) -> Program:
        steps: list[dict[str, pl.Expr]] = []
        for col, expr, level in sorted(self.cols.values(), key=lambda c: c[2]):
            while len(steps) <= level:
                steps.append({})
            steps[level][col] = expr
        return Program([s for s in steps if s], list(self.tfs.values()), dict(self.outputs), set(self.columns), self.xs,
                       frozenset(self.xs_cols))

    # ---- helpers ---------------------------------------------------------------------------
    def _put(self, col: str, expr: pl.Expr, level: int) -> None:
        self.cols.setdefault(col, (col, expr, level))

    def _materialize(self, key: str, expr: pl.Expr, level: int, over: tuple[str, ...] | None) -> Val:
        col = "l1_" + hashlib.sha1(key.encode()).hexdigest()[:12]
        if col not in self.cols:
            self._put(col, expr.over(*over) if over else expr, level)
        if over and "date" in over:
            self.xs_cols.add(col)
        return Val("expr", expr=pl.col(col), level=self.cols[col][2] + 1, key=key)

    def field_expr(self, name: str, pos: int | None) -> Val:
        f = lookup(name)
        if f is None:
            raise CompileError(f"不认识的名字 {name}", pos)
        if self.market not in f.markets:
            raise CompileError(f"{f.name}（{f.doc}）在 {self.market} 没有数据", pos)
        if self.period != "D" and f.column not in TF_FIELDS:
            raise CompileError(f"周线/月线里没有 {f.name}", pos)
        if self.available is not None and f.column not in self.available:
            raise CompileError(f"{f.name}（{f.doc}）在当前数据档里没有", pos)
        self.columns.add(f.column)
        self.touched.add(f.column)
        return Val("expr", f.unit, pl.col(f.column), key=f.name)

    def value(self, v: Val, node: A.Node) -> Val:
        if v.kind == "word":
            raise CompileError(f"不认识的名字 {v.word}", node.pos)
        if v.kind == "multi":
            names = "、".join(f"{v.fn.name}.{o}" for o in v.outputs)
            raise CompileError(f"{v.fn.name} 有多个输出，请写明要哪一个：{names}", node.pos)
        return v

    # ---- visitors ----------------------------------------------------------------------------
    def visit(self, n: A.Node) -> Val:
        return getattr(self, f"v_{type(n).__name__}")(n)

    def v_Num(self, n: A.Num) -> Val:
        return Val("num", U.NUMBER, num=n.value, key=_num_key(n.value))

    def v_Param(self, n: A.Param) -> Val:
        if n.name not in self.params:
            raise CompileError(f"参数 ${n.name} 没有定义", n.pos)
        value = self.params[n.name]
        return Val("num", U.NUMBER, num=value, key=_num_key(value))

    def v_Name(self, n: A.Name) -> Val:
        if (var := self.env.get(n.name.upper())) is not None:   # an indicator's own variable comes first
            return var
        if lookup(n.name):
            return self.field_expr(n.name, n.pos)
        fn = FUNCTIONS.get(n.name.upper())
        if fn is not None:
            return self.call(fn, (), (), n)
        if n.name.upper() in FUTURE:
            self.call_future(n.name, n.pos)
        return Val("word", word=n.name, key=n.name)

    def v_Attr(self, n: A.Attr) -> Val:
        base = self.visit(n.obj)
        if base.kind != "multi":
            if base.kind == "word":
                raise CompileError(f"{base.word}.{n.name} 是结构属性，只能在规则的阶段条件里引用", n.pos)
            raise CompileError(f"{n.name} 不是这个值的属性", n.pos)
        name = base.fn.aliases.get(n.name.upper(), n.name.upper())
        if name not in base.outputs:
            raise CompileError(f"{base.fn.name} 没有 {n.name}，可选：{'、'.join(base.outputs)}", n.pos)
        return base.outputs[name]

    def v_At(self, n: A.At) -> Val:
        raise CompileError("expr@阶段 只能在规则的阶段条件里使用", n.pos)

    def v_Unary(self, n: A.Unary) -> Val:
        x = self.value(self.visit(n.x), n.x)
        if self.lenient:
            x = self._cond(x) if n.op == "NOT" else self._num(x)
        if n.op == "NOT":
            if x.unit != U.BOOL:
                raise CompileError(f"NOT 后面要接条件，而这里是{x.unit.value}", n.pos)
            return Val("expr", U.BOOL, ~x.expr, level=x.level, key=f"NOT({x.key})")
        if x.unit == U.BOOL:
            raise CompileError("条件前面不能加负号", n.pos)
        if x.kind == "num":
            return Val("num", U.NUMBER, num=-x.num, key=_num_key(-x.num))
        return Val("expr", x.unit, -x.expr, level=x.level, key=f"-({x.key})")

    def v_Binary(self, n: A.Binary) -> Val:
        a = self.value(self.visit(n.a), n.a)
        b = self.value(self.visit(n.b), n.b)
        if self.lenient:
            if n.op in ("AND", "OR"):
                a, b = self._cond(a), self._cond(b)
            elif not (n.op in (">", "<", ">=", "<=", "==", "!=") and a.unit == b.unit == U.BOOL):
                a, b = self._num(a), self._num(b)
        key = f"({a.key} {n.op} {b.key})"
        try:
            if n.op in ("AND", "OR"):
                for side, node in ((a, n.a), (b, n.b)):
                    if side.unit != U.BOOL:
                        raise CompileError(f"{n.op} 两边都要是条件，而这里是{side.unit.value}", node.pos)
                ea, eb = self._e(a), self._e(b)
                return Val("expr", U.BOOL, ea & eb if n.op == "AND" else ea | eb, level=max(a.level, b.level), key=key)
            if n.op in (">", "<", ">=", "<=", "==", "!="):
                if not self.lenient:
                    UN.compare(a.unit, b.unit, n.op)
                for x, lit in ((a, b), (b, a)):
                    if not self.lenient and x.unit == U.PRICE and lit.kind == "num" and lit.num != 0:
                        raise CompileError("后复权价格不能和固定数字比较（历史价格会被复权改变）；"
                                           "绝对价格门槛请用原始价 RAW_C / RAW_O / RAW_H / RAW_L", n.pos)
                ea, eb = self._e(a), self._e(b)
                if U.BOOL in (a.unit, b.unit):
                    ops = {">": ea > eb, "<": ea < eb, ">=": ea >= eb, "<=": ea <= eb, "==": ea == eb, "!=": ea != eb}
                else:   # numbers a rounding apart are equal (EQ): an exact tie must not fall on either side at random
                    eq = tie(ea, eb)
                    ops = {">": (ea > eb) & ~eq, "<": (ea < eb) & ~eq, ">=": (ea >= eb) | eq, "<=": (ea <= eb) | eq,
                           "==": eq, "!=": ~eq}
                return Val("expr", U.BOOL, ops[n.op], level=max(a.level, b.level), key=key)
            try:
                unit = {"+": lambda: UN.add(a.unit, b.unit, "+"), "-": lambda: UN.add(a.unit, b.unit, "-"),
                        "*": lambda: UN.mul(a.unit, b.unit), "/": lambda: UN.div(a.unit, b.unit)}[n.op]()
            except UN.UnitError:
                if not self.lenient:
                    raise
                unit = U.NUMBER   # 通达信 adds what you write: the value is a plain number
        except UN.UnitError as e:
            raise CompileError(str(e), n.pos) from None
        if a.kind == "num" and b.kind == "num" and not isinstance(a.num, tuple) and not isinstance(b.num, tuple):
            if n.op == "/" and b.num == 0:
                raise CompileError("除以 0", n.pos)
            val = {"+": a.num + b.num, "-": a.num - b.num, "*": a.num * b.num, "/": a.num / b.num if b.num else None}[n.op]
            return Val("num", U.NUMBER, num=val, key=_num_key(val))
        ea, eb = self._e(a), self._e(b)
        # x / 0 is unknown, not infinity — in an indicator (lenient, the 通达信 way) 0, as 通达信 draws it: a one-price day's
        # (C−O)/(H−L) does not blank the N bars of a SUM over it
        div = pl.when(eb != 0).then(ea / eb)
        expr = {"+": ea + eb, "-": pl.when(tie(ea, eb)).then(0).otherwise(ea - eb),  # equal within EQ: no change
                "*": ea * eb, "/": div.when(eb == 0).then(0.0) if self.lenient else div}[n.op]
        return Val("expr", unit, expr, level=max(a.level, b.level), key=key)

    def _e(self, v: Val) -> pl.Expr:
        if v.kind == "num":
            if isinstance(v.num, tuple):
                raise CompileError("列表只能作为函数参数")
            return pl.lit(v.num, dtype=pl.Float64)
        return v.expr

    def v_Call(self, n: A.Call) -> Val:
        name = n.name.upper()
        if name in FUTURE:
            self.call_future(name, n.pos)
        if name == "TF":
            return self.tf(n)
        from wb.engine.l3.rule import AGGREGATIONS, STRUCTURE_FUNCTIONS  # the rule-only words, for a hint
        if any(k in ("of", "last", "first") for k, _ in n.kwargs) or (name in AGGREGATIONS and name not in FUNCTIONS):
            raise CompileError(f"{n.name}(…) 是对规则里一个阶段的汇总（of= / last= / first=），只能写在规则（L3）的条件里", n.pos)
        if name in STRUCTURE_FUNCTIONS:
            raise CompileError(f"{n.name} 要读规则里绑定的结构，只能写在规则（L3）的条件里", n.pos)
        fn = FUNCTIONS.get(name)
        if fn is None:
            raise CompileError(f"不认识的函数 {n.name}", n.pos)
        if name == "REF" and len(n.args) == 2 and not n.kwargs:   # REF(X, N) with N a series: N bars back on each bar
            k = self.value(self.visit(n.args[1]), n.args[1])
            if k.kind == "expr":
                return self.ref_by(fn, n, k)
        return self.call(fn, n.args, n.kwargs, n)

    def ref_by(self, fn: Fn, n: A.Call, k: Val) -> Val:
        """REF(X, N) whose N differs from bar to bar (通达信: REF(C, BARSLAST(C<O))): the value N bars back, unknown where
        N is unknown, negative (the future) or reaches before the first bar."""
        if self.lenient:
            k = self._num(k)
        if k.unit == U.BOOL:
            raise CompileError("REF 的位移要是数值", n.args[1].pos)
        x = self.check_arg(fn, fn.args[0], self.visit(n.args[0]), n.args[0])
        xe = self._arg_value(fn.args[0], x, True)
        idx = pl.int_range(pl.len(), dtype=pl.Int64)
        target = idx - k.expr.round(0).cast(pl.Int64, strict=False)
        ok = k.expr.is_not_null() & (k.expr >= 0) & (target >= 0)
        expr = pl.when(ok).then(xe.gather(pl.when(ok).then(target).otherwise(0)))
        v = self._materialize(f"REF({x.key}, {k.key})", expr, max(x.level, k.level), ("listing_id",))
        v.unit = x.unit if x.kind == "expr" else U.NUMBER
        return v

    def call_future(self, name: str, pos: int):
        raise CompileError(f"{name.upper()}（{FUTURE[name.upper()]}）会用到未来的数据，不能用于选股；"
                           "找高低点请用结构原语 PIVOTS", pos)

    def call(self, fn: Fn, args: tuple, kwargs: tuple, n: A.Node) -> Val:
        if fn.phase:
            raise CompileError(f"{fn.name} 需要 {fn.phase} 阶段的数据，现在还不能用", n.pos)
        if self.market not in fn.markets:
            raise CompileError(f"{fn.name} 不适用于 {self.market}", n.pos)
        if self.period != "D" and fn.scope == "xs":
            raise CompileError(f"周线/月线里不能用截面函数 {fn.name}", n.pos)
        bound = self.bind(fn, args, kwargs, n)
        if fn.expand:
            return self.visit(parse(fn.expand(*[v.num if v.kind == "num" else v.word for v in bound])))
        units = [v.unit for spec, v in zip(fn.args, bound, strict=True) if spec.kind == "series"]
        try:
            unit = fn.unit(units) if callable(fn.unit) else fn.unit
        except UN.UnitError as e:
            if not self.lenient:
                raise CompileError(f"{fn.name}：{e}", n.pos) from None
            unit = U.NUMBER
        values = [self._arg_value(spec, v, fn.scope != "el") for spec, v in zip(fn.args, bound, strict=True)]
        level = max([v.level for v in bound if v.kind == "expr"], default=0)
        key = f"{fn.name}({', '.join(v.key for v in bound)})"
        try:
            built = fn.build(Ctx(self), *values)
        except ValueError as e:
            raise CompileError(f"{fn.name}：{e}", n.pos) from None
        if fn.scope == "xs":
            self.xs = True
            words = [v.word for spec, v in zip(fn.args, bound, strict=True) if spec.kind == "word"]
            over = fn.partition(*words) if callable(fn.partition) else fn.partition
            self.columns |= set(over) - {"date"}
        else:
            over = ("listing_id",) if fn.scope == "ts" else None
        if fn.outputs:
            outs = {}
            for out, out_unit in fn.outputs:
                v = self._materialize(f"{key}.{out}", built[out], level, over)
                v.unit = out_unit
                outs[out] = v
            return Val("multi", outputs=outs, fn=fn, key=key)
        if fn.scope == "el":
            return Val("expr", unit, built, level=level, key=key)
        v = self._materialize(key, built, level, over)
        v.unit = unit
        return v

    def bind(self, fn: Fn, args: tuple, kwargs: tuple, n: A.Node) -> list[Val]:
        if len(args) > len(fn.args):
            raise CompileError(f"{fn.name} 最多 {len(fn.args)} 个参数，这里有 {len(args)} 个", n.pos)
        given: dict[str, A.Node] = {spec.name: a for spec, a in zip(fn.args, args, strict=False)}
        for k, v in kwargs:
            if k not in {a.name for a in fn.args}:
                raise CompileError(f"{fn.name} 没有参数 {k}", v.pos)
            given[k] = v
        out = []
        for spec in fn.args:
            node = given.get(spec.name)
            if node is None:
                if spec.default is REQUIRED:
                    raise CompileError(f"{fn.name} 缺少参数 {spec.name}", n.pos)
                out.append(Val("num", U.NUMBER, num=spec.default, key=_num_key(spec.default)) if spec.kind != "word"
                           else Val("word", word=spec.default, key=spec.default))
                continue
            out.append(self.check_arg(fn, spec, self.visit(node), node))
        return out

    def check_arg(self, fn: Fn, spec, v: Val, node: A.Node) -> Val:
        where = f"{fn.name} 的参数 {spec.name}"
        if spec.kind in ("series", "cond"):
            v = self.value(v, node)
            if v.kind == "num" and isinstance(v.num, tuple):
                raise CompileError(f"{where} 不能是列表", node.pos)
            if self.lenient:
                v = self._cond(v) if spec.kind == "cond" else self._num(v)
            if spec.kind == "cond" and v.unit != U.BOOL:
                raise CompileError(f"{where} 要是条件", node.pos)
            if spec.kind == "series" and v.unit == U.BOOL:
                raise CompileError(f"{where} 要是数值，不能是条件", node.pos)
            return v
        if spec.kind == "word":
            if v.kind != "word" or (spec.choices and v.word.lower() not in spec.choices):
                raise CompileError(f"{where} 只能是 {' / '.join(spec.choices)}", node.pos)
            return Val("word", word=v.word.lower(), key=v.word.lower())
        if v.kind == "word":   # MA(C, N) with no N defined: the name is what is wrong, not MA's argument
            raise CompileError(f"不认识的名字 {v.word}", node.pos)
        if v.kind != "num":
            raise CompileError(f"{where} 要写成数字", node.pos)
        if spec.kind == "list":
            if not isinstance(v.num, tuple) or not v.num:
                raise CompileError(f"{where} 要写成列表，例如 [5, 10, 20]", node.pos)
            return v
        if isinstance(v.num, tuple):
            raise CompileError(f"{where} 不能是列表", node.pos)
        if spec.kind == "int":
            if float(v.num) != int(v.num):
                raise CompileError(f"{where} 要是整数", node.pos)
            if spec.name == "N" and fn.name == "REF" and v.num < 0:
                raise CompileError("REF 的位移不能是负数（负数会引用未来的数据）", node.pos)
            if spec.min is not None and v.num < spec.min:
                raise CompileError(f"{where} 至少是 {spec.min:g}", node.pos)
            return Val("num", U.NUMBER, num=int(v.num), key=_num_key(int(v.num)))
        return v

    def _arg_value(self, spec, v: Val, windowed: bool = False):
        if spec.kind in ("series", "cond"):
            if windowed and v.kind == "num":  # a literal shifts to nothing: CROSS(RSI(6), 30) needs 30 on every bar
                return pl.repeat(float(v.num), pl.len(), dtype=pl.Float64)
            return self._e(v)
        if spec.kind == "word":
            return v.word
        return v.num

    def tf(self, n: A.Call) -> Val:
        if self.period != "D":
            raise CompileError("TF 不能嵌套", n.pos)
        if len(n.args) != 2 or n.kwargs:
            raise CompileError('TF 的写法是 TF("W", 公式) 或 TF("M", 公式)', n.pos)
        period = n.args[0].name.upper() if isinstance(n.args[0], A.Name) else None
        if period not in ("W", "M"):
            raise CompileError('TF 的周期只能是 "W"（周线）或 "M"（月线）', n.args[0].pos)
        sub = Compiler(self.market, self.params, self.available, period, self.lenient)
        unit = sub.add("v", n.args[1])
        prog = sub.program()
        key = f"TF({period}, {n.args[1]!r}, {sorted(self.params.items())!r})"  # named by the values its $params take
        col = "tf_" + hashlib.sha1(key.encode()).hexdigest()[:12]
        self.tfs.setdefault(col, TfProgram(period, prog, col))
        self.columns |= prog.columns
        return Val("expr", unit, pl.col(col), level=0, key=key)
