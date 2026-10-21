"""The L3 matcher (spec §5.5 阶段序列, §5.6 匹配结果与解释树).

A binding gives every stage of a rule a place on one listing's bars, as bar numbers (`i`, counted
from the listing's first traded bar): start, end, anchor (the bar `at` conditions read) and known
(the bar from which this stage's part is known). Geometry:

  structure  the bound structure's own bars: start .. end, anchored and known on its confirmation.
             Its end is the bar before an event bound on it (the box ends the bar before its
             breakout); else, if another stage hangs on its end, its own end (and the binding waits
             until that end is known); else the evaluation bar while it is still active.
  event      one bar: a structure's event (`on: box.break_up`), known on the event's known_at (a
             spike in the establishing window is known when the box is confirmed), or the bar where
             a formula holds (`when:`) — every such bar on its own, the first one in the window
             after `within` / `after` a stage otherwise.
  window     bars a..b before a stage's start or after its end.

A binding completes on the latest bar any of its parts becomes known (and never before
`signal_at`; never while a part it needs is still open, such as the end of an active box); it can
only signal then, and it only reads bars up to then (spec §5.8 rule 3).

Conditions: every maximal pure-formula part is compiled once by the L1 compiler into a column of
the frame; the rest (stage attributes `box.top`, aggregations over a stage's bars, `x@stage`,
BAND_VOL_RATIO, `pressure.*`) is evaluated over the bindings. Unknown stays unknown: an aggregation
over a bar that does not exist (a window reaching before the first bar) or an unknown value is
unknown, and a rule with an unknown condition and no failing one is `unknown`, not `fail` — with
its likely reasons (spec §4.6).

At a day t a listing is `signal` (a complete binding finishing on t passes, the nearest structure
first), `watch` (the stages up to `watch_stage` are known by t and pass, and that structure is
still active on t; the next stage's single-threshold conditions are measured as distances),
`unknown`, or `fail` (with the deepest stage reached and the condition closest to passing). Over a
range every structure instance signals at most once."""
import copy
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import polars as pl

from wb.engine.l1 import ast as A
from wb.engine.l1.compiler import Compiler, Program
from wb.engine.l1.functions.base import tie
from wb.engine.l1.parser import ParseError
from wb.engine.l1.run import evaluate
from wb.engine.l2.derived import FalseBottomsIf
from wb.engine.l2.more import left_pressure
from wb.engine.l3.understand import names
from wb.engine.l3.rule import (PRESSURE, SMALL_BOX_STAGES, STRUCTURE_FUNCTIONS, STRUCTURES, Rank, Rule, RuleError, Stage, _exprs,
                               _walk, depends, is_aggregation, is_pressure)
from wb.registry import PRIMITIVES

_CMP = {">", "<", ">=", "<=", "==", "!="}


def _constant(n) -> bool:
    """Numbers and parameters only (a switch, `$sw == 0`): nothing that differs from bar to bar."""
    return all(isinstance(x, (A.Num, A.Param, A.Unary, A.Binary)) for x in _walk(n))


_TRUE, _FALSE = A.Binary("==", A.Num(1), A.Num(1)), A.Binary("==", A.Num(1), A.Num(0))   # a settled condition
_ENDED_BY_BREAK = ("上破离箱", "下破离箱")
PIECE = 300_000   # rows a day a 潜伏 range unrolls at a time (ten years of a template's boxes on 800 stocks: millions)


@dataclass(frozen=True)
class Col(A.Node):
    """A compiled formula column of the frame (read at the context bar)."""
    name: str


@dataclass(frozen=True)
class BCol(A.Node):
    """A column of the bindings table."""
    name: str


def _nodes(n):
    """Every node of a formula's tree."""
    yield n
    for v in vars(n).values():
        for x in (v if isinstance(v, list | tuple) else [v]):
            if isinstance(x, A.Node):
                yield from _nodes(x)
        if isinstance(v, dict):
            for x in v.values():
                if isinstance(x, A.Node):
                    yield from _nodes(x)


def box_tol(rule: Rule) -> float:
    """The band a rule's boxes are crossed by (BOX `tol`): its own setting, else the primitive's default. A watch row's
    break line is the box's top × (1 + tol)."""
    p = rule.primitive_params().get("BOX", {})
    return float(p.get("tol", next(x.default for x in PRIMITIVES["BOX"].params if x.name == "tol")))


class Matcher:
    def __init__(self, rule: Rule, market: str, frame: pl.DataFrame, tables: dict, calendar: list[date] | None = None,
                 available: set[str] | None = None, pool: list[str] | None = None):
        """`pool`: the listings to judge when `frame` is the whole market only for the formulas' sake (a
        cross-sectional value, see `run.frame_for`)."""
        self._compile(rule, market, available)
        self.tables = tables
        prog = self._comp.program()
        f = frame.sort("listing_id", "date")
        out = evaluate(f, prog, calendar) if prog.outputs else f.select("listing_id", "date")
        if pool is not None:
            out, f = out.filter(pl.col("listing_id").is_in(pool)), f.filter(pl.col("listing_id").is_in(pool))
        self.frame = out.with_columns(pl.int_range(pl.len()).over("listing_id").alias("i"))
        # rows with a data problem (spec §4.6: what they affect is unknown, and the reason is named)
        q = (f.select("listing_id", "date", "quality").filter(pl.col("quality").list.len() > 0) if "quality" in f.columns
             else pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, "quality": pl.List(pl.Utf8)}))
        self.quality = q.join(self.frame.select("listing_id", "date", "i"), on=["listing_id", "date"])
        self.tol = self._box_tol()
        self._cache: dict[date, dict] = {}
        self._spans: dict[str, tuple[int, int]] | None = None   # each listing's rows of the frame (see _one)
        self._shared: dict = {}   # what every variant of this frame and these tables can reuse (variant() shares it)
        self._lock = threading.Lock()   # an explanation reads the frame swapped for one listing's rows

    def variant(self, rule: Rule, skip: frozenset = frozenset()) -> "Matcher":
        """The same evaluated frame judged by `rule` — another threshold of a parameter its conditions compare with,
        the formulas' columns unchanged — and without the conditions in `skip` ((stage, id)): the condition
        check-up's variants (spec §9.5) cost no new evaluation."""
        m = copy.copy(self)
        m.rule, m.conds, m._cache = rule, [c for c in self.conds if (c[0], c[1]) not in skip], {}
        return m

    @classmethod
    def program(cls, rule: Rule, market: str, available: set[str] | None = None) -> Program:
        """The rule's formulas compiled: the frame columns they read, whether one reads other listings of the day
        (RANK_XS, INDUSTRY_RANK ...) and which columns those are."""
        m = cls.__new__(cls)
        m._compile(rule, market, available)
        return m._comp.program()

    @classmethod
    def reads(cls, rule: Rule, market: str, available: set[str] | None = None) -> set[str]:
        """The frame columns the rule's own formulas read (not the bars the matcher always reads for itself); a box
        function (BAND_VOL_RATIO) and left pressure read volume too."""
        m = cls.__new__(cls)
        m._compile(rule, market, available)
        return m.read

    @classmethod
    def cross_sectional(cls, rule: Rule, market: str, available: set[str] | None = None) -> bool:
        return cls.program(rule, market, available).xs

    @classmethod
    def columns(cls, rule: Rule, market: str, available: set[str] | None = None) -> set[str]:
        return cls.program(rule, market, available).columns

    def _compile(self, rule: Rule, market: str, available: set[str] | None) -> None:
        if market not in rule.markets:
            raise RuleError(f"规则 {rule.id} 只适用于 {'、'.join(rule.markets)}，不适用于 {market}")
        self.rule, self.market, self._available, self.warnings = rule, market, available, []
        self.ids = [s.id for s in rule.stages]
        self.stages = set(self.ids)
        self._comp, self._cols = Compiler(market, {p.id: p.value for p in rule.params}, available), {}
        self.v, self.h, self.c = self._l1(A.Name("V")), self._l1(A.Name("H")), self._l1(A.Name("C"))
        self.o = self._l1(A.Name("O")) if rule.reads_pressure else None
        # a 潜伏 row's 假底 as if its box broke the next day reads the lows (watches(as_if_break=True))
        self.l = self._l1(A.Name("L")) if any(".false_bottom" in t for _, _, t in _exprs(rule)) else None
        self._comp.touched = set()   # from here on: what the rule's own formulas read
        self.idx = None  # IDX_C, compiled when the rule uses REL_PERF over a stage
        self.conds = []  # (stage, id, kind, lowered node, text)
        for s in rule.stages:
            for c in s.at:
                self.conds.append((s.id, c.id, "at", self._place(f"{s.id}.{c.id}", c.expr, self._lower_cond), c.expr))
            for c in s.during:
                self.conds.append((s.id, c.id, "during", self._place(f"{s.id}.{c.id}", c.expr, self._lower_cond), c.expr))
            for kind in ("all", "any"):
                if getattr(s, kind):
                    text = getattr(s, kind)
                    self.conds.append((s.id, s.id, kind, self._place(f"{s.id}.{kind}", text, self._lower), text))
        self.when = {s.id: self._place(f"{s.id}.when", s.when, self._l1) for s in rule.stages if s.when}
        for sid, col in self.when.items():  # the formula also judges its own bar: unknown stays unknown
            self.conds.append((sid, "when", "at", col, self.rule.stage(sid).when))
        self.ranks = [(self._place(f"ranking[{j}]", r.expr, self._lower), r.dir) for j, r in enumerate(rule.ranking)]
        self.univ = self._universe()
        self.names = names(rule)   # how its parts read in explanations and lists (never its ids)
        texts = [c.expr for s in rule.stages for c in (*s.at, *s.during)] + [t for s in rule.stages for t in (s.all, s.any, s.when) if t]
        boxed = any(isinstance(n, A.Call) and n.name in STRUCTURE_FUNCTIONS for t in texts for n in _nodes(A_parse(t, "reads")))
        self.read = set(self._comp.touched) | ({"volume"} if boxed or rule.reads_pressure else set())

    @staticmethod
    def _place(where: str, text: str, step):
        try:
            return step(A_parse(text, where))
        except RuleError as e:
            raise RuleError(f"{where}（{text}）：{e}") from None

    # ---- compiling -----------------------------------------------------------------------------
    def _l1(self, node) -> Col:
        if node not in self._cols:
            name = f"_l1_{len(self._cols)}"
            try:
                self._comp.add(name, node)
            except ValueError as e:
                raise RuleError(str(e)) from None
            self._cols[node] = name
        return Col(self._cols[node])

    def _pure(self, n) -> bool:
        for x in _walk(n):
            if isinstance(x, A.At) or is_aggregation(x) or is_pressure(x):
                return False
            if isinstance(x, A.Name) and x.name in self.stages:
                return False
            if isinstance(x, A.Attr) and isinstance(x.obj, A.Name) and x.obj.name in self.stages:
                return False
            if isinstance(x, A.Call) and (x.name.upper() in STRUCTURE_FUNCTIONS or any(k in ("of", "last", "first") for k, _ in x.kwargs)):
                return False
        return True

    def _fold(self, n):
        """`$sw == 0 OR …` with the switch off is true, `$sw == 1 AND …` false: a side of numbers and parameters that
        settles an OR or an AND settles the condition, and the rest is never evaluated (形末量尽 / 阳量群 are off by
        default: their aggregations over every bar of every box were computed and thrown away). Read when a condition
        is evaluated, with the parameters of the rule judging then (a check-up's variant may turn the switch on)."""
        if isinstance(n, A.Binary) and n.op in ("OR", "AND"):
            for side in (n.a, n.b):
                if _constant(side):
                    v = bool(pl.select(self._px(side)).item())
                    if v == (n.op == "OR"):
                        return _TRUE if v else _FALSE
        return n

    def _lower_cond(self, n):
        """A condition: a comparison keeps its two sides apart, so its value and threshold can be shown
        (spec §5.6) and measured as a distance on the watch list; anything else is lowered whole."""
        if isinstance(n, A.Binary) and n.op in _CMP:
            if self._pure(n):  # the L1 compiler checks the comparison as a whole (units: C > 5 needs RAW_C) — on the
                try:           # side: the frame needs only its two sides, so another threshold shares it (spec §9.5)
                    Compiler(self.market, {p.id: p.value for p in self.rule.params}, self._available).add("check", n)
                except ValueError as e:
                    raise RuleError(str(e)) from None
            return A.Binary(n.op, self._lower(n.a), self._lower(n.b), pos=n.pos)
        return self._lower(n)

    def _lower(self, n):
        if isinstance(n, (A.Num, A.Param)):
            return n
        if self._pure(n) and not _constant(n):   # numbers and parameters alone ($sw == 0) are read where they stand:
            return self._l1(n)                   # a stage's aggregation has no bar to read a column on
        if isinstance(n, A.Unary):
            return A.Unary(n.op, self._lower(n.x), pos=n.pos)
        if isinstance(n, A.Binary):
            return A.Binary(n.op, self._lower(n.a), self._lower(n.b), pos=n.pos)
        if isinstance(n, A.Call):
            if not is_aggregation(n) and n.name.upper() not in STRUCTURE_FUNCTIONS:   # a formula function reads a column
                raise RuleError(f"{n.name} 是逐根 K 线算的公式函数，里面不能用箱体、阶段或 pressure 的值（它们是阶段那根 K 线上的"
                                f"一个数）：可以直接比较、加减乘除，或用 MEAN、MAX 这样的聚合")
            if is_aggregation(n) and n.name.upper() == "REL_PERF":  # reads the reference index close
                self.idx = self._l1(A.Name("IDX_C"))
            return A.Call(n.name.upper(), tuple(self._lower(a) if not isinstance(a, A.Name) or a.name not in self.stages else a
                                                for a in n.args),
                          tuple((k, v if k == "of" else self._lower(v)) for k, v in n.kwargs), pos=n.pos)
        if isinstance(n, A.At):
            return A.At(self._lower(n.x), n.stage, pos=n.pos)
        return n

    def _universe(self) -> Col | None:
        text, said = universe_formula(self.rule, self.market, self._available)
        self.warnings += said
        return self._l1(A_parse(text, "universe")) if text else None

    def _box_tol(self) -> float:
        return box_tol(self.rule)

    # ---- structures and geometry ---------------------------------------------------------------
    def _index(self, df: pl.DataFrame, col: str, out: str) -> pl.DataFrame:
        idx = self.frame.select("listing_id", pl.col("date").alias(col), pl.col("i").alias(out))
        return df.join(idx, on=["listing_id", col], how="left")

    def _structures(self, s: Stage) -> pl.DataFrame:
        prim, name = STRUCTURES[s.use]
        t = self.tables.get(prim, {}).get(name)
        if t is None or t.is_empty():
            return pl.DataFrame()
        t = t.drop([c for c in ("start_i", "known_i", "end_i") if c in t.columns])  # bar numbers are the frame's
        flt = {"status": "established", **self.rule.filters(s)} if "status" in t.columns else self.rule.filters(s)
        for k, v in flt.items():
            t = t.filter(pl.col(k).is_in(v if isinstance(v, list) else [v]))
        if s.use in ("GOLD", "NEST"):  # the pattern's bars are the small box's
            parts = [x for x in (self.tables.get("BOX", {}).get("boxes"), self.tables.get("SLANT_BOX", {}).get("slant"))
                     if x is not None and x.height]
            if not parts:  # no boxes at all: no small box to stand on
                return pl.DataFrame()
            small = pl.concat(parts, how="diagonal_relaxed")
            t = t.join(small.select(pl.col("box_id").alias("small_box_id"), "start", "end", "ended_at", "end_reason"),
                       on="small_box_id", how="left")
        fb = self.tables.get("BOX_DERIVED", {}).get("false_bottoms")
        if s.use == "BOX" and fb is not None:   # the box's 假底 (交易计划的失效线): its price, known on its break bar
            t = t.join(fb.select("box_id", pl.col("price").alias("false_bottom"), pl.col("known_at").alias("_fbk")),
                       on="box_id", how="left")
            t = self._index(t, "_fbk", "false_bottom_i").drop("_fbk")
        start = next(c for c in ("start", "first", "date", "known_at") if c in t.columns)
        end = next(c for c in ("end", "second", "date", "known_at") if c in t.columns)
        # a 金品 / 箱中箱 is its big and small box (大箱>小箱: the chart draws both; one small box can end two big ones)
        sid = (pl.col("box_id") if "box_id" in t.columns else
               pl.concat_str([pl.col("big_box_id"), pl.col("small_box_id")], separator=">") if s.use in ("GOLD", "NEST") else
               pl.concat_str([pl.col(c).cast(pl.Utf8) for c in ("listing_id", start, "known_at") if c in t.columns], separator=":"))
        broke = pl.col("end_reason").is_in(_ENDED_BY_BREAK) if "end_reason" in t.columns else pl.lit(False)
        t = t.with_columns(sid.alias("_id"), pl.col(start).alias("_s"), pl.col("known_at").alias("_k"), pl.col(end).alias("_e"),
                           (pl.col("ended_at") if "ended_at" in t.columns else pl.col("known_at")).alias("_x"), broke.alias("_b"))
        for col, out in (("_s", "start_i"), ("_k", "known_i"), ("_e", "e_i"), ("_x", "ended_i")):
            t = self._index(t, col, out)
        if "superseded_at" in t.columns:
            t = self._index(t, "superseded_at", "superseded_i")
        t = t.with_columns((pl.col("e_i") - pl.col("_b").cast(pl.Int64)).alias("nat_end_i")).drop("_s", "_k", "_e", "_x", "_b", "e_i")
        keep = [c for c in t.columns if c != "listing_id"]
        return t.select("listing_id", *[pl.col(c).alias(f"{s.id}.{c}") for c in keep]).rename({f"{s.id}._id": f"{s.id}.id"})

    def _order(self, stages: list[Stage]) -> list[Stage]:
        done, out, left = set(), [], list(stages)
        while left:
            for s in left:
                d = depends(s)
                if d is None or d in done:
                    out.append(s)
                    done.add(s.id)
                    left.remove(s)
                    break
            else:
                raise RuleError("阶段之间的引用成了环：" + "、".join(s.id for s in left))
        return out

    def _end(self, b: pl.DataFrame, sid: str) -> pl.Expr:
        """The end bar of a stage that another stage hangs on (a structure's own end)."""
        s = self.rule.stage(sid)
        return pl.col(f"{sid}.nat_end_i") if s.kind == "structure" else pl.col(f"{sid}.end_i")

    def _bind(self, stages: list[Stage], t_i: pl.DataFrame | None, judge_on_t: bool = False) -> pl.DataFrame:
        """All bindings of `stages` (known by t when `t_i`: listing_id, t_i is given). With
        `judge_on_t` they are judged on t itself (the watch list) instead of on their completion."""
        b, uses_end, _ = self._bindings(stages, t_i)
        if b.is_empty():
            return b
        return self._complete(b, stages, uses_end, t_i if judge_on_t else None)

    def _bindings(self, stages: list[Stage], t_i: pl.DataFrame | None, today: bool = False):
        """The bindings of `stages` before their completion, the structures whose end another stage reads, and (with
        `today`, the 潜伏 list over a range) the formula events nothing hangs on, left out: judged on the day itself,
        they are placed on each day once the bindings are unrolled."""
        b: pl.DataFrame | None = None
        uses_end: set[str] = set()
        bound: set[str] = set()
        applied: set[tuple[str, str]] = set()
        later: list[Stage] = []
        for s in self._order(stages):
            if today and s.kind == "event" and not s.on and not (s.within or s.after) and not self._hangs_on(s.id, stages):
                later.append(s)
                continue
            if s.kind == "structure":
                cand = self._structures(s)
                if cand.is_empty():
                    return pl.DataFrame(), uses_end, later
                cand = cand.with_columns(pl.col(f"{s.id}.known_i").alias(f"{s.id}.anchor_i"))
                if t_i is not None:
                    cand = cand.join(t_i, on="listing_id").filter(pl.col(f"{s.id}.known_i") <= pl.col("t_i")).drop("t_i")
                of = s.with_.get("of")
                if b is None:
                    b = cand
                elif s.after and s.after.gap and s.after.stage in bound:
                    b = self._pair(b, cand, s, uses_end)
                    applied.add((s.id, "after"))
                elif of in bound:  # 金品 / 箱中箱 of that stage's box: paired by the box id — never every gold of a listing
                    # with every version of its boxes first (CN: ~900k mid / long versions × ~1.5M gold rows, millions a
                    # listing; the whole-market run was killed for memory)
                    b = b.join(cand.with_columns(pl.col(f"{s.id}.big_box_id").alias("_of")),
                               left_on=["listing_id", f"{of}.id"], right_on=["listing_id", "_of"])
                else:
                    b = b.join(cand, on="listing_id")
            elif s.kind == "event" and s.on:
                x, ev = s.on.split(".")
                use = self.rule.stage(x).use
                if use in SMALL_BOX_STAGES:  # a gold's / a nest's events are its small box's (flat or slanted)
                    parts = [t["events"] for t in (self.tables.get("BOX"), self.tables.get("SLANT_BOX"))
                             if t and t.get("events") is not None and t["events"].height]
                    if not parts:  # no events anywhere: nothing can hang on the gold
                        return pl.DataFrame(), uses_end, later
                    e = pl.concat(parts, how="diagonal_relaxed")
                    key = f"{x}.small_box_id"
                else:
                    e, key = self.tables[STRUCTURES[use][0]]["events"], f"{x}.id"
                if e.is_empty():
                    return pl.DataFrame(), uses_end, later
                e = e.filter(pl.col("event") == ev)
                if "known_at" not in e.columns:
                    e = e.with_columns(pl.col("date").alias("known_at"))
                e = e.join(b.select("listing_id", pl.col(key).alias("box_id")).unique(), on="box_id")
                # an event can be found later than it happened (a spike in the establishing window is
                # known when the box is confirmed): its bar is its date, it is known on known_at
                e = self._index(self._index(e, "date", "anchor_i"), "known_at", "known_i").select(
                    pl.col("box_id").alias(key), pl.col("anchor_i").alias(f"{s.id}.anchor_i"),
                    pl.col("known_i").alias(f"{s.id}.known_i"), pl.col("value").alias(f"{s.id}.value"))
                b = b.join(e, on=key).filter(pl.col(f"{s.id}.known_i") >= pl.col(f"{x}.known_i"))
                # never before a structure bound before it began: 上品's 大箱突破 comes after its small box under the top
                # (10-12: 10 of 42 上品 signals paired an old breakout with a small box drawn weeks after it)
                for y in sorted(bound):
                    if self.rule.stage(y).kind == "structure" and f"{y}.start_i" in b.columns:
                        b = b.filter(pl.col(f"{s.id}.anchor_i") >= pl.col(f"{y}.start_i"))
                a = pl.col(f"{s.id}.anchor_i")
                b = b.with_columns(a.alias(f"{s.id}.start_i"), a.alias(f"{s.id}.end_i"))
            elif s.kind == "event":
                b = self._when(b, s, t_i, uses_end, stages)
            else:
                ref = s.before or s.after
                lo, hi = self._lohi(ref.bars)
                if s.before:
                    base = pl.col(f"{ref.stage}.start_i")
                    b = b.with_columns((base - hi).alias(f"{s.id}.start_i"), (base - lo).alias(f"{s.id}.end_i"))
                else:
                    uses_end.add(ref.stage)
                    base = self._end(b, ref.stage)
                    b = b.with_columns((base + lo).alias(f"{s.id}.start_i"), (base + hi).alias(f"{s.id}.end_i"))
                b = b.with_columns(pl.col(f"{s.id}.end_i").alias(f"{s.id}.anchor_i"), pl.col(f"{s.id}.end_i").alias(f"{s.id}.known_i"))
            bound.add(s.id)
            if b is not None:
                b = self._relations(b, stages, bound, applied, uses_end)
            if b is None or b.is_empty():
                return pl.DataFrame(), uses_end, later
        return b, uses_end, later

    def _relations(self, b: pl.DataFrame, stages: list[Stage], bound: set[str], applied: set, uses_end: set[str]) -> pl.DataFrame:
        """The relations between two stages — `after: {gap}` (starts a..b bars after the other's end) and
        an event's `within` (1..N bars after it) — applied as soon as both stages are bound, so later
        joins carry fewer rows."""
        for s in stages:
            rels = []
            if s.kind != "window" and s.after and s.after.gap:
                rels.append(("after", s.after.stage, pl.col(f"{s.id}.start_i"), self.rule.span(s.after.gap)))
            if s.kind == "event" and s.on and s.within:
                rels.append(("within", s.within.stage, pl.col(f"{s.id}.anchor_i"), (1, self.rule.span(s.within.bars))))
            for kind, ref, col, (lo, hi) in rels:
                if (s.id, kind) not in applied and s.id in bound and ref in bound:
                    uses_end.add(ref)
                    b = b.filter((col - self._end(b, ref)).is_between(lo, hi))
                    applied.add((s.id, kind))
        return b

    def _pair(self, b: pl.DataFrame, cand: pl.DataFrame, s: Stage, uses_end: set[str]) -> pl.DataFrame:
        """Bind structure stage `s` through its `after: {stage, gap}`: only the pairs whose gap fits are
        formed — a sorted search on (listing, start bar) — never every pair of structures of a listing
        (two box stages over ten years of a market would be tens of millions of rows)."""
        x, (lo, hi) = s.after.stage, self.rule.span(s.after.gap)
        uses_end.add(x)
        ids = pl.concat([b.select("listing_id"), cand.select("listing_id")]).unique().with_row_index("_lc")
        big = 1 << 32  # bar numbers stay far below this
        c = (cand.select("listing_id", f"{s.id}.id", f"{s.id}.start_i").join(ids, on="listing_id")
             .with_columns((pl.col("_lc").cast(pl.Int64) * big + pl.col(f"{s.id}.start_i")).alias("_k")).sort("_k")
             .with_row_index("_j"))
        r = (b.with_row_index("_r").select("_r", "listing_id", self._end(b, x).alias("_e")).drop_nulls("_e")
             .join(ids, on="listing_id").with_columns((pl.col("_lc").cast(pl.Int64) * big + pl.col("_e")).alias("_base")))
        first = c["_k"].search_sorted(r["_base"] + lo, side="left")
        last = c["_k"].search_sorted(r["_base"] + hi, side="right")
        pairs = (pl.DataFrame({"_r": r["_r"], "_a": first, "_z": last})
                 .with_columns(pl.int_ranges("_a", "_z").alias("_j")).explode("_j", empty_as_null=False)
                 .join(c.select("_j", f"{s.id}.id"), on="_j").select("_r", f"{s.id}.id"))
        return b.with_row_index("_r").join(pairs, on="_r").drop("_r").join(cand, on=["listing_id", f"{s.id}.id"])

    def _point(self, b, sid):
        a = pl.col(f"{sid}.anchor_i")
        return b.with_columns(a.alias(f"{sid}.start_i"), a.alias(f"{sid}.end_i"), a.alias(f"{sid}.known_i"))

    @staticmethod
    def _hangs_on(sid: str, stages: list[Stage]) -> bool:
        """Whether another of `stages` is placed relative to stage `sid`."""
        return any((x.on and x.on.split(".")[0] == sid) or any(r is not None and r.stage == sid for r in (x.before, x.after, x.within))
                   for x in stages if x.id != sid)

    def _lohi(self, v) -> tuple[int, int]:
        """A window's [a, b] (or a single N) in bars, parameters read."""
        v = self.rule.span(v)
        return v if isinstance(v, tuple) else (v, v)

    def _when(self, b, s: Stage, t_i, uses_end, stages: list[Stage]) -> pl.DataFrame:
        col = self.when[s.id].name
        hits = self.frame.filter(pl.col(col)).select("listing_id", pl.col("i").alias(f"{s.id}.anchor_i"))
        ref = s.within or s.after
        if ref is None:  # every bar where it holds
            if t_i is not None and self._hangs_on(s.id, stages):  # on a given day: its bars up to that day
                hits = hits.join(t_i, on="listing_id").filter(pl.col(f"{s.id}.anchor_i") <= pl.col("t_i")).drop("t_i")
            elif t_i is not None:  # nothing hangs on it: that day, judged by the formula itself (unknown stays unknown)
                hits = t_i.select("listing_id", pl.col("t_i").alias(f"{s.id}.anchor_i"))
            out = hits if b is None else b.join(hits, on="listing_id")
            return self._point(out, s.id)
        uses_end.add(ref.stage)
        base = self._end(b, ref.stage)
        lo, hi = (1, self.rule.span(ref.bars)) if s.within else self._lohi(ref.gap or ref.bars)
        b = b.with_row_index("_w").with_columns((base + lo).alias("_lo"), (base + hi).alias("_hi"))
        first = (b.select("_w", "listing_id", "_lo", "_hi").join(hits, on="listing_id")
                 .filter(pl.col(f"{s.id}.anchor_i").is_between(pl.col("_lo"), pl.col("_hi")))
                 .group_by("_w").agg(pl.col(f"{s.id}.anchor_i").min()))
        return self._point(b.join(first, on="_w").drop("_w", "_lo", "_hi"), s.id)

    def _known(self, stages: list[Stage], uses_end: set[str], completed: bool) -> list[pl.Expr]:
        """The bars a binding's parts are known on: each stage's, the end of a structure another stage reads, and (when
        `completed`, not judged on a given day) the rule's signal part."""
        sig, part = self.rule.signal
        known = [pl.col(f"{s.id}.known_i") for s in stages]
        known += [pl.col(f"{x}.ended_i") for x in uses_end if self.rule.stage(x).kind == "structure"]
        if sig in {s.id for s in stages} and completed:  # a watch is judged on t, not on its signal part
            if part == "end" and self.rule.stage(sig).kind == "structure":
                part = "ended"  # a structure's end is known on the bar it is seen to have ended
            known.append(pl.col(f"{sig}.{'anchor' if part == 'at' else part}_i"))
        return known

    @staticmethod
    def _last_known(known: list[pl.Expr]) -> pl.Expr:
        """The bar the last of them is known on; never while one is still open (null)."""
        return pl.when(pl.any_horizontal([k.is_null() for k in known])).then(None).otherwise(pl.max_horizontal(known))

    def _complete(self, b: pl.DataFrame, stages: list[Stage], uses_end: set[str], fixed: pl.DataFrame | None = None) -> pl.DataFrame:
        """Completion bar T (or the given evaluation bar), the structures' evaluated ends, and every
        structure seen as of T."""
        known = self._known(stages, uses_end, fixed is None)
        if fixed is not None:  # judged on t: every stage must be there by t
            b = b.join(fixed.rename({"t_i": "T"}), on="listing_id").filter(pl.all_horizontal([k <= pl.col("T") for k in known]))
        else:  # complete when the last stage is known
            b = b.with_columns(self._last_known(known).alias("T")).filter(pl.col("T").is_not_null())
        return self._on_T(b, stages, uses_end)

    def _on_T(self, b: pl.DataFrame, stages: list[Stage], uses_end: set[str]) -> pl.DataFrame:
        """Bindings with their bar T: those whose structures still stand on T, the structures' ends, and every
        structure seen as of T."""
        for s in stages:   # a structure replaced before T is not there on T (a 金品 once a later small box in its big box is
            # known); one replaced on T still is: the bar's events (a 撞顶涨停, a 回箱 before an outer box is found) are
            # recorded on the version standing when the bar opened, and the version drawn from T carries none of them
            if s.kind == "structure" and f"{s.id}.superseded_i" in b.columns:
                b = b.filter(pl.col(f"{s.id}.superseded_i").is_null() | (pl.col(f"{s.id}.superseded_i") >= pl.col("T")))
        for s in stages:
            if s.kind != "structure":
                continue
            clips = [pl.col(f"{e.id}.anchor_i") - 1 for e in stages if e.kind == "event" and e.on and e.on.split(".")[0] == s.id]
            if clips:
                end = pl.min_horizontal(clips)
            elif s.id in uses_end:
                end = pl.col(f"{s.id}.nat_end_i")
            else:
                ended = pl.col(f"{s.id}.ended_i").is_not_null() & (pl.col(f"{s.id}.ended_i") <= pl.col("T"))
                end = pl.when(ended).then(pl.col(f"{s.id}.nat_end_i")).otherwise(pl.col("T"))
            b = b.with_columns(end.alias(f"{s.id}.end_i"))
        return self._as_of(b.with_row_index("bid"), stages)

    def _as_of(self, b: pl.DataFrame, stages: list[Stage]) -> pl.DataFrame:
        """What a structure looked like on T (spec §5.8 rule 3): its end is unknown until it ended,
        a 雏形 is superseded only from then, and running counts are recounted from rows known by T."""
        T = pl.col("T")
        for s in stages:
            if s.kind != "structure":
                continue
            x = s.id
            ended = pl.col(f"{x}.ended_i").is_not_null() & (pl.col(f"{x}.ended_i") <= T)
            hide = [pl.when(ended).then(pl.col(f"{x}.{c}")).alias(f"{x}.{c}")
                    for c in ("end", "ended_at", "end_reason", "length") if f"{x}.{c}" in b.columns]
            if f"{x}.superseded_at" in b.columns:
                hide.append(pl.when(pl.col(f"{x}.superseded_i") <= T).then(pl.col(f"{x}.superseded_at")).alias(f"{x}.superseded_at"))
            if f"{x}.false_bottom" in b.columns:   # known on the break bar, none before
                hide.append(pl.when(pl.col(f"{x}.false_bottom_i") <= T).then(pl.col(f"{x}.false_bottom")).alias(f"{x}.false_bottom"))
            b = b.with_columns(hide)
            touches = self.tables.get("BOX", {}).get("touches") if s.use == "BOX" else None
            if touches is not None and not touches.is_empty() and f"{x}.touches_top" in b.columns:
                # a box's touches over all its versions (each is recorded under the version drawn then)
                boxes = self.tables["BOX"]["boxes"]
                lin = (boxes.select("box_id", pl.coalesce("lineage", "box_id").alias("_line")) if "lineage" in boxes.columns
                       else boxes.select("box_id", pl.col("box_id").alias("_line")))
                bound = b.select("bid", "T", "listing_id", pl.col(f"{x}.id").alias("box_id")).join(lin, on="box_id", how="left")
                tt = self._index(touches.rename({"known_at": "_k"}).join(lin, on="box_id").join(
                    bound.select("listing_id", "_line").unique(), on="_line"), "_k", "_ki")
                # each lineage's touches counted up to each bar they are known on, read as of T (a 潜伏 list over a range
                # has a row a day: every row against every touch of its box would be millions of rows)
                cum = (tt.drop_nulls("_ki").group_by("_line", "_ki")
                       .agg((pl.col("side") == "top").sum().alias("_top"), (pl.col("side") == "bottom").sum().alias("_bot"))
                       .sort("_line", "_ki").with_columns(pl.col("_top").cum_sum().over("_line"), pl.col("_bot").cum_sum().over("_line")))
                n = (bound.drop_nulls("_line").sort("T").join_asof(cum.sort("_ki"), left_on="T", right_on="_ki", by="_line",
                                                                   strategy="backward", check_sortedness=False)
                     .select("bid", "_top", "_bot"))
                b = (b.join(n, on="bid", how="left")
                     .with_columns(pl.col("_top").fill_null(0).cast(pl.Int64).alias(f"{x}.touches_top"),
                                   pl.col("_bot").fill_null(0).cast(pl.Int64).alias(f"{x}.touches_bottom")).drop("_top", "_bot"))
        return b

    # ---- evaluation ----------------------------------------------------------------------------
    def _bars(self, b: pl.DataFrame, start: pl.Expr, end: pl.Expr, need: list[pl.Expr]) -> pl.DataFrame:
        """One row per (binding, bar in start..end) with only the frame and binding columns `need`
        reads (a ten-year range has millions of such rows)."""
        names = {n for e in need for n in e.meta.root_names()}
        rows = b.select("bid", "listing_id", pl.int_ranges(start, end + 1).alias("i")).explode("i", empty_as_null=False)
        rows = rows.join(self.frame.select("listing_id", "i", *[c for c in self.frame.columns if c in names - {"listing_id", "i"}]),
                         on=["listing_id", "i"], how="left")
        return rows.join(b.select("bid", *[c for c in b.columns if c in names - {"bid", "listing_id", "i"}]), on="bid", how="left")

    def _over(self, b: pl.DataFrame, start: pl.Expr, end: pl.Expr, need: list[pl.Expr], running: list[pl.Expr]) -> pl.DataFrame:
        """`running` — values over a binding's bars from `start` up to each bar, in bar order — read at each binding's
        `end`: (bid, the running values). Bindings that read the same bars (the listing, the start, the same values of
        the binding columns `need` reads) share one pass up to the last of their ends: the 潜伏 list over a range has a
        row a day for each standing box, its bars from the same start — read once per row, ten years of a box's bars
        would be read once a day."""
        names = {n for e in need for n in e.meta.root_names()}
        own = [c for c in b.columns if c in names - {"bid", "listing_id", "i"} and c not in self.frame.columns]
        iv = b.select("bid", "listing_id", start.alias("_s"), end.alias("_e"), *own)
        keys = ["listing_id", "_s", *own]
        seq = (iv.group_by(keys).agg(pl.col("_e").max()).filter(pl.col("_e") >= pl.col("_s"))
               .with_row_index("_q").with_columns(pl.col("_q").cast(pl.Int64)))
        rows = (self._bars(seq.rename({"_q": "bid"}), pl.col("_s"), pl.col("_e"), need).sort("bid", "i")
                .with_columns([r.over("bid") for r in running]))
        out = [r.meta.output_name() for r in running]
        at = iv.join(seq.select("_q", *keys), on=keys, how="left", nulls_equal=True).select("bid", pl.col("_q"), pl.col("_e").alias("i"))
        return at.join(rows.select(pl.col("bid").alias("_q"), "i", *out), on=["_q", "i"], how="left").select("bid", *out)

    def _interval(self, sid: str, kwargs) -> tuple[pl.Expr, pl.Expr, str]:
        kw = {k: v for k, v in kwargs}
        of = kw.get("of")
        st = of.name if of is not None else sid
        start, end = pl.col(f"{st}.start_i"), pl.col(f"{st}.end_i")
        if "last" in kw:
            start = pl.max_horizontal(start, end - int(kw["last"].value) + 1)
        if "first" in kw:
            end = pl.min_horizontal(end, start + int(kw["first"].value) - 1)
        return start, end, st

    def _pre(self, b: pl.DataFrame, n, sid: str, bar: str):
        """Aggregations, x@stage, structure functions and pressure inside `n` -> binding columns (b grows)."""
        if is_pressure(n):  # read on this bar: above its close; read for a box stage, above the box's top, the line a cross
            name = f"_pr{len(b.columns)}"   # buys at (its room before the break, while the close is still inside it)
            box = sid in self.stages and self.rule.stage(sid).kind == "structure" and f"{sid}.top" in b.columns
            if box:
                b = b.with_columns(pl.col(f"{sid}.top").alias("_pp"))
            pp = ["_pp"] if box else []
            p = self._pressure(b.select("listing_id", pl.col(bar).alias("i"), *[pl.col(c).alias("price") for c in pp])
                               .drop_nulls().unique())
            p = p.select("listing_id", pl.col("i").alias(bar), *[pl.col("price").alias(c) for c in pp],
                         pl.col(PRESSURE[n.name]).alias(name))
            return b.join(p, on=["listing_id", bar, *pp], how="left").drop(pp), BCol(name)
        if isinstance(n, (A.Num, A.Param, Col, BCol, A.Name)) or (isinstance(n, A.Attr)):
            return b, n
        if isinstance(n, A.Unary):
            b, x = self._pre(b, n.x, sid, bar)
            return b, A.Unary(n.op, x)
        if isinstance(n, A.Binary):
            b, x = self._pre(b, n.a, sid, bar)
            b, y = self._pre(b, n.b, sid, bar)
            return b, A.Binary(n.op, x, y)
        if isinstance(n, A.At):
            b, x = self._pre(b, n.x, n.stage, f"{n.stage}.anchor_i")
            name = f"_at{len(b.columns)}"
            return self._at(b, x, f"{n.stage}.anchor_i", name), BCol(name)
        if isinstance(n, A.Call) and n.name in STRUCTURE_FUNCTIONS:  # BAND_VOL_RATIO(box) = V / max V near box top
            x = n.args[0].name
            near = pl.col(self.h.name) >= pl.col(f"{x}.top") * (1 - self.tol)
            name = f"_ref{len(b.columns)}"
            ref = self._over(b, pl.col(f"{x}.start_i"), pl.col(f"{x}.end_i"), [near, pl.col(self.v.name)],
                             [pl.when(near).then(pl.col(self.v.name)).cum_max().forward_fill().alias(name)])
            b = b.join(ref, on="bid", how="left")
            return b, A.Binary("/", self.v, BCol(name))
        if isinstance(n, A.Call) and is_aggregation(n):
            args = [self.c, self.idx] if n.name == "REL_PERF" else []
            for a in n.args:
                b, x = self._pre(b, a, sid, bar)
                args.append(x)
            start, end, _ = self._interval(sid, n.kwargs)
            xs = [self._px(a) for a in args]
            name = f"_agg{len(b.columns)}"
            agg = self._over(b, start, end, xs, [self._running(n.name, xs).alias(name)])
            return b.join(agg, on="bid", how="left"), BCol(name)
        raise RuleError(f"这里不能用 {n}")

    def _pressure(self, pts: pl.DataFrame) -> pl.DataFrame:
        """left_pressure on (listing_id, i) points, over the BOX boxes of the rule's pressure scales."""
        boxes = self.tables.get("BOX", {}).get("boxes")
        if boxes is None:
            raise RuleError("pressure 要用到箱体（BOX），这次运行没有算箱体")
        bars = self.frame.select("listing_id", "date", "i", *[pl.col(c.name).alias(k) for k, c in
                                                              (("open", self.o), ("high", self.h), ("close", self.c), ("volume", self.v))])
        at = pts.join(bars.select("listing_id", "i", "date"), on=["listing_id", "i"])
        by = ["price"] if "price" in at.columns else []
        p = left_pressure(boxes.filter(pl.col("scale").is_in(self.rule.pressure.scales)), bars.drop("i"),
                          at.select("listing_id", "date", *by), self.rule.pressure.lookback)
        return p.join(at, on=["listing_id", "date", *by])

    @staticmethod
    def _running(name: str, xs: list[pl.Expr]) -> pl.Expr:
        """An aggregation over a binding's bars from its start up to each bar (read at its end: the aggregation over
        start..end); unknown from the first bar any of its values is unknown."""
        x = xs[0]
        known = pl.any_horizontal([e.is_null() for e in xs]).cast(pl.Int32).cum_sum() == 0
        i = pl.col("i").cast(pl.Float64)
        n = (pl.int_range(pl.len()) + 1).cast(pl.Float64)
        value = {
            "MEAN": x.cum_sum() / n, "MAX": x.cum_max(), "MIN": x.cum_min(), "SUM": x.cum_sum(),
            "COUNT": x.cast(pl.Int64).cum_sum(), "RATIO": x.cast(pl.Float64).cum_sum() / n,
            "FIRST": x.first(), "LAST": x, "CHANGE": x / x.first() - 1,
            "SLOPE": (n * (i * x).cum_sum() - i.cum_sum() * x.cum_sum()) / (n * (i * i).cum_sum() - i.cum_sum() ** 2),
            "RUNUP": (xs[0] / xs[-1].cum_min() - 1).cum_max() if len(xs) > 1 else None,
            "DRAWDOWN": (1 - xs[-1] / xs[0].cum_max()).cum_max() if len(xs) > 1 else None,
            "REL_PERF": (x / x.first()) / (xs[-1] / xs[-1].first()) - 1 if len(xs) > 1 else None,
        }[name]
        return pl.when(known).then(value)

    def _px(self, n) -> pl.Expr:
        if isinstance(n, A.Num):
            return pl.lit(float(n.value))
        if isinstance(n, A.Param):
            v = self.rule.param(n.name)
            return pl.lit(float(v) if isinstance(v, bool) else v)   # a switch is a number, as in a formula (true = 1)
        if isinstance(n, (Col, BCol)):
            return pl.col(n.name)
        if isinstance(n, A.Attr) and isinstance(n.obj, A.Name):
            return pl.col(f"{n.obj.name}.{n.name}")
        if isinstance(n, A.Unary):
            x = self._px(n.x)
            return ~x if n.op == "NOT" else -x
        if isinstance(n, A.Binary):
            a, b = self._px(n.a), self._px(n.b)
            eq = tie(a, b)   # numbers a rounding apart are equal (EQ), as in a formula: +15.000% is not 0.1499999999999999
            return {"+": lambda: a + b, "-": lambda: pl.when(eq).then(0).otherwise(a - b), "*": lambda: a * b,
                    "/": lambda: pl.when(b != 0).then(a / b),
                    ">": lambda: (a > b) & ~eq, "<": lambda: (a < b) & ~eq, ">=": lambda: (a >= b) | eq, "<=": lambda: (a <= b) | eq,
                    "==": lambda: eq, "!=": lambda: ~eq, "AND": lambda: a & b, "OR": lambda: a | b}[n.op]()
        raise RuleError(f"这里不能用 {n}")

    def _at(self, b: pl.DataFrame, n, bar: str, name: str) -> pl.DataFrame:
        """`n` read on each binding's `bar` (rows stay in the bindings' order)."""
        rows = b.join(self.frame, left_on=["listing_id", bar], right_on=["listing_id", "i"], how="left", maintain_order="left")
        return b.with_columns(rows.with_columns(self._px(n).alias(name))[name])

    def _evaluate(self, b: pl.DataFrame, conds) -> tuple[pl.DataFrame, list]:
        """Each condition's value, threshold and status per binding: columns c{k}.v / .th / .ok."""
        out = []
        for k, (sid, cid, kind, node, text) in enumerate(conds):
            if sid not in {c.split(".")[0] for c in b.columns if "." in c}:
                continue
            node = self._fold(node)
            bar = f"{sid}.anchor_i"
            lhs, rhs, op = (node.a, node.b, node.op) if isinstance(node, A.Binary) and node.op in _CMP else (node, None, None)
            if kind in ("all", "any"):
                b, x = self._pre(b, node, sid, bar)
                e = self._px(x)
                seen = lambda v: e.eq(v).fill_null(False).cast(pl.Int32).cum_sum() > 0   # noqa: E731
                unknown = e.is_null().cast(pl.Int32).cum_sum() > 0
                ok = (pl.when(seen(False)).then(False).when(unknown).then(None).otherwise(True) if kind == "all"
                      else pl.when(seen(True)).then(True).when(unknown).then(None).otherwise(False))
                agg = self._over(b, pl.col(f"{sid}.start_i"), pl.col(f"{sid}.end_i"), [e],
                                 [ok.alias(f"c{k}.ok"), e.cast(pl.Int64).fill_null(0).cum_sum().cast(pl.Float64).alias(f"c{k}.v"),
                                  (pl.int_range(pl.len()) + 1).cast(pl.Float64).alias(f"c{k}.th")])
                b = b.join(agg, on="bid", how="left")
            else:
                b, x = self._pre(b, lhs, sid, bar)
                if rhs is not None:
                    b, y = self._pre(b, rhs, sid, bar)
                    b = self._at(b, x, bar, f"c{k}.v") if kind == "at" else b.with_columns(self._px(x).alias(f"c{k}.v"))
                    b = self._at(b, y, bar, f"c{k}.th") if kind == "at" else b.with_columns(self._px(y).alias(f"c{k}.th"))
                    v, th = pl.col(f"c{k}.v"), pl.col(f"c{k}.th")
                    b = b.with_columns(self._px(A.Binary(op, BCol(f"c{k}.v"), BCol(f"c{k}.th"))).alias(f"c{k}.ok"))
                else:
                    b = self._at(b, x, bar, f"c{k}.ok") if kind == "at" else b.with_columns(self._px(x).alias(f"c{k}.ok"))
                    b = b.with_columns(pl.col(f"c{k}.ok").cast(pl.Float64).alias(f"c{k}.v"), pl.lit(None, pl.Float64).alias(f"c{k}.th"))
            out.append((k, sid, cid, kind, None if kind in ("all", "any") else op, text))
        return b, out

    def _status(self, b: pl.DataFrame, evals, stages: list[str]) -> pl.DataFrame:
        """Per stage pass (Kleene AND of its conditions); overall status; deepest stage passed."""
        cols = []
        for sid in stages:
            oks = [pl.col(f"c{k}.ok") for k, s, *_ in evals if s == sid]
            cols.append((pl.all_horizontal(oks) if oks else pl.lit(True)).alias(f"{sid}.ok"))
        b = b.with_columns(cols)
        ok = pl.all_horizontal([pl.col(f"{s}.ok") for s in stages])
        depth = pl.lit(-1)
        for j, sid in enumerate(stages):  # deepest prefix of stages that all pass
            prefix = pl.all_horizontal([pl.col(f"{s}.ok").fill_null(False) for s in stages[: j + 1]])
            depth = pl.when(prefix).then(j).otherwise(depth)
        return b.with_columns(ok.alias("ok"), depth.alias("depth"))

    def _priority(self, stages: list[Stage]) -> list[pl.Expr]:
        """The binding a listing's day picks: its latest-known structures first, the latest-starting; then their ids
        (two bindings alike so far are picked the same way however their rows are ordered: the 潜伏 list over a range
        equals at(day))."""
        structs = [s for s in stages if s.kind == "structure"]
        keys = [pl.col(f"{s.id}.{c}") for s in structs for c in ("known_i", "start_i")]
        return keys + [pl.col(f"{s.id}.id") for s in structs] if keys else [pl.col("bid")]

    def _latest(self, b: pl.DataFrame) -> list[pl.Expr]:
        """Between bindings alike in their structures, the latest events first: a 回箱 is explained by the break it came
        back from, not by an earlier one the stock had already come back from (both within the window)."""
        return [pl.col(f"{s.id}.anchor_i") for s in self.rule.stages if s.kind == "event" and f"{s.id}.anchor_i" in b.columns]

    # ---- the three modes -----------------------------------------------------------------------
    def _t_index(self, day: date) -> pl.DataFrame:
        today = self.frame.filter(pl.col("date") == day)
        if self.univ is not None:
            today = today.filter(pl.col(self.univ.name).fill_null(False))
        return today.select("listing_id", pl.col("i").alias("t_i"))

    def _day(self, day: date) -> dict:
        if day in self._cache:
            return self._cache[day]
        t_i = self._t_index(day)
        full = self._bind(self.rule.stages, t_i)
        if not full.is_empty():
            full = full.join(t_i, on="listing_id").filter(pl.col("T") == pl.col("t_i"))
        full_ev = []
        if not full.is_empty():
            full, full_ev = self._evaluate(full, self.conds)
            full = self._status(full, full_ev, self.ids)
        watch, watch_ev, nxt = self._watch(t_i)
        state = {"t_i": t_i, "full": full, "full_ev": full_ev, "watch": watch, "watch_ev": watch_ev, "next": nxt}
        self._cache = {day: state}
        return state

    def _watch(self, t_i: pl.DataFrame):
        w = self.rule.watch_stage
        if not w:
            return pl.DataFrame(), [], None
        return self._standing(self.ids.index(w), t_i)

    def _standing(self, k: int, t_i: pl.DataFrame):
        """The bindings of the stages up to structure stage `k` whose structure k still stands on t, judged on t (the
        潜伏 list when k is its stage; the funnel for every k), their conditions, and the stage after k."""
        prefix = self.rule.stages[: k + 1]
        if any(depends(s) not in (None, *(x.id for x in prefix)) for s in prefix):
            return pl.DataFrame(), [], None
        b = self._bind(prefix, t_i, judge_on_t=True)
        if b.is_empty():
            return b, [], None
        w = self.ids[k]
        b = b.filter(pl.col(f"{w}.ended_i").is_null() | (pl.col(f"{w}.ended_i") > pl.col("T"))).with_columns(
            pl.col("T").alias("t_i"))
        if b.is_empty():
            return b, [], None
        ids = [s.id for s in prefix]
        b, ev = self._evaluate(b, [c for c in self.conds if c[0] in ids])
        b = self._status(b, ev, ids)
        nxt = self.rule.stages[k + 1] if k + 1 < len(self.rule.stages) else None
        return b, ev, nxt

    def _refs(self, n) -> set[str]:
        """Stages an expression reads."""
        out = set()
        for x in _walk(n):
            if isinstance(x, A.Attr) and isinstance(x.obj, A.Name) and x.obj.name in self.stages:
                out.add(x.obj.name)
            elif isinstance(x, A.Name) and x.name in self.stages:
                out.add(x.name)
            elif isinstance(x, A.At):
                out.add(x.stage)
        return out

    def _ranked(self, b: pl.DataFrame, label: str) -> pl.DataFrame:
        """The chosen bindings with their status label and ranking values (read on T); a value that
        needs a stage the binding does not have yet (a watch has no breakout) is unranked."""
        b = b.with_columns(pl.lit(label).alias("_st"))
        have = {c.split(".")[0] for c in b.columns if "." in c}
        sig = self.rule.signal[0]
        for j, (node, _) in enumerate(self.ranks):
            if self._refs(node) <= have and b.height:
                b, x = self._pre(b, node, sig if sig in have else next((x for x in self.ids if x in have), sig), "T")
                b = self._at(b, x, "T", f"rank{j}")
            else:
                b = b.with_columns(pl.lit(None, pl.Float64).alias(f"rank{j}"))
        return b

    def at(self, day: date) -> pl.DataFrame:
        """One row per listing in the pool on `day`: status signal / watch / unknown / fail, the
        bound structures, and the ranking values; sorted signal, watch, unknown, fail, then by rank."""
        st = self._day(day)
        if "result" in st:
            return st["result"]
        t_i, full, watch = st["t_i"], st["full"], st["watch"]
        structs = [s for s in self.rule.stages if s.kind == "structure"]
        picks = []
        if not full.is_empty():
            sig = (full.filter(pl.col("ok").fill_null(False)).sort(self._priority(structs) + self._latest(full), descending=True)
                   .unique("listing_id", keep="first", maintain_order=True))
            picks.append(self._ranked(sig, "signal"))
        if not watch.is_empty():
            prefix = [s for s in structs if f"{s.id}.id" in watch.columns]
            wat = (watch.filter(pl.col("ok").fill_null(False)).sort(self._priority(prefix), descending=True)
                   .unique("listing_id", keep="first", maintain_order=True))
            if picks:
                wat = wat.join(picks[0].select("listing_id"), on="listing_id", how="anti")
            picks.append(self._ranked(wat, "watch"))
        ranks = [f"rank{j}" for j in range(len(self.ranks))]
        keep = ["listing_id", "_st", *[f"{s.id}.id" for s in structs], *ranks]
        best = (pl.concat([p.select([c for c in keep if c in p.columns]) for p in picks], how="diagonal_relaxed")
                if picks else pl.DataFrame(schema={"listing_id": pl.Utf8, "_st": pl.Utf8}))
        unknown = [p.filter(pl.col("ok").is_null()).select("listing_id") for p in (full, watch) if not p.is_empty()]
        unk = pl.concat(unknown).unique() if unknown else pl.DataFrame(schema={"listing_id": pl.Utf8})
        result = t_i.select("listing_id").join(best, on="listing_id", how="left")
        for c in [f"{s.id}.id" for s in structs] + ranks:
            if c not in result.columns:
                result = result.with_columns(pl.lit(None, pl.Utf8 if c.endswith(".id") else pl.Float64).alias(c))
        result = result.with_columns(
            pl.when(pl.col("_st").is_not_null()).then(pl.col("_st"))
            .when(pl.col("listing_id").is_in(unk["listing_id"].implode())).then(pl.lit("unknown"))
            .otherwise(pl.lit("fail")).alias("status"),
            pl.when(pl.col("_st") == "signal").then(pl.lit(day)).alias("signal_date")).drop("_st")
        order = pl.col("status").replace_strict({"signal": 0, "watch": 1, "unknown": 2, "fail": 3}, return_dtype=pl.Int8)
        keys = [order] + [pl.col(f"rank{j}") * (-1 if d == "desc" else 1) for j, (_, d) in enumerate(self.ranks)] + [pl.col("listing_id")]
        result = (result.sort(keys, nulls_last=True)
                  .rename({f"{s.id}.id": s.id for s in structs})
                  .select("listing_id", "status", "signal_date", *[s.id for s in structs], *ranks))
        st["result"], st["picks"] = result, picks
        return result

    def depths(self, day: date) -> pl.DataFrame:
        """How far each listing in the pool got on `day`: the deepest prefix of stages one of its bindings passes,
        -1 when none does (the 选股 funnel, spec §7.1) — each structure stage counted while it stands on the day,
        whichever stage the 潜伏 list waits on."""
        st = self._day(day)
        standing = [self._standing(k, st["t_i"])[0] for k, s in enumerate(self.rule.stages)
                    if s.kind == "structure" and s.id != self.rule.watch_stage]
        parts = [p.select("listing_id", pl.col("depth").cast(pl.Int16)) for p in (st["full"], st["watch"], *standing)
                 if not p.is_empty() and "depth" in p.columns]
        out = st["t_i"].select("listing_id")
        if parts:
            out = out.join(pl.concat(parts).group_by("listing_id").agg(pl.col("depth").max()), on="listing_id", how="left")
        else:
            out = out.with_columns(pl.lit(None, pl.Int16).alias("depth"))
        return out.with_columns(pl.col("depth").fill_null(-1))

    def _passing(self, start: date, end: date) -> pl.DataFrame:
        """Every complete binding passing the whole rule on a day in [start, end] of the pool, with its day
        (`signal_date`) — each a day at(day) calls a signal."""
        b = self._bind(self.rule.stages, None)
        if b.is_empty():
            return b
        dates = self.frame.select("listing_id", pl.col("i").alias("T"), pl.col("date").alias("signal_date"),
                                  *([pl.col(self.univ.name).alias("_u")] if self.univ is not None else []))
        b = b.join(dates, on=["listing_id", "T"]).filter(pl.col("signal_date").is_between(start, end))
        if self.univ is not None:
            b = b.filter(pl.col("_u").fill_null(False))
        if b.is_empty():
            return b
        b, ev = self._evaluate(b, self.conds)
        return self._status(b, ev, self.ids).filter(pl.col("ok").fill_null(False))

    def watches(self, start: date, end: date, as_if_break: bool = False, all_days: bool = False) -> pl.DataFrame:
        """Range mode of the 潜伏 list (design 2026-10-11 §9: at(day) costs 1.1 s a day for 800 stocks, ten years of
        lists hours a rule): each binding of the stages up to the watch stage unrolled over the days it stands — from
        the day its last part is known to the day before its watch structure ended, never after one of its structures
        was replaced, the pool's days in [start, end] — and judged on each day as at(day) judges it: one row a listing
        and day, the binding at(day) picks, none on a day the whole rule signals. With the line the next stage breaks,
        as the 潜伏 explanation and 盯盘 read it: `edge` (the box's top, a slanted small box's line on that day: the line
        盯盘 alerts at and a cross buys at) and `rise` (how much it rises a day: the next day's top is edge + rise), `line`
        (the break line, edge × (1 + tol), the explanation's threshold) and `per_bar` (its rise a day), `line_of` (the box
        the line is the top of), and the ranking read on that day (rank0, …).
        With `as_if_break`, on a row whose next bar reaches its top a box's 假底 is the one it would have if it broke
        then (false_bottoms_if): what a cross entry's 失效线 reads, the evening before (design 2026-10-11 §6) — no
        other row can be a cross, and its conditions are judged as at(day). With `all_days`, the rows of days the whole
        rule signals are kept (variants alike up to the watch stage share one replay, each removing its own days)."""
        structs = [s for s in self.rule.stages if s.kind == "structure"]
        ranks = [f"rank{j}" for j in range(len(self.ranks))]
        empty = pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, **{s.id: pl.Utf8 for s in structs}, "edge": pl.Float64,
                                     "rise": pl.Float64, "line": pl.Float64, "per_bar": pl.Float64, "line_of": pl.Utf8,
                                     **{c: pl.Float64 for c in ranks}})
        w = self.rule.watch_stage
        if not w:
            return empty
        k = self.ids.index(w)
        prefix = self.rule.stages[: k + 1]
        if any(depends(s) not in (None, *(x.id for x in prefix)) for s in prefix):
            return empty
        b, uses_end, today = self._bindings(prefix, None, today=True)
        if b.is_empty():
            return empty
        days = (self.frame.filter(pl.col("date").is_between(start, end)).group_by("listing_id")
                .agg(pl.col("i").min().alias("_lo"), pl.col("i").max().alias("_hi")))
        last = [pl.col(f"{w}.ended_i") - 1, pl.col("_hi")]   # min_horizontal skips nulls: an open box stands to the end
        last += [pl.col(f"{s.id}.superseded_i") for s in prefix if s.kind == "structure" and f"{s.id}.superseded_i" in b.columns]
        placed = [s for s in prefix if s not in today]   # a formula event judged on the day is known on each day it is placed
        b = (b.with_columns(self._last_known(self._known(placed, uses_end, False)).alias("_f")).filter(pl.col("_f").is_not_null())
             .join(days, on="listing_id").with_columns(pl.max_horizontal("_f", "_lo").alias("_a"), pl.min_horizontal(last).alias("_z"))
             .filter(pl.col("_a") <= pl.col("_z")).drop("_f", "_lo", "_hi"))
        ids = [s.id for s in prefix]
        conds = [c for c in self.conds if c[0] in ids]
        keys = self._priority(prefix)
        # a listing's bindings are judged together (its day picks one of them), so the days are unrolled a group of
        # listings at a time, PIECE rows or so each
        n = b.group_by("listing_id").agg((pl.col("_z") - pl.col("_a") + 1).sum().alias("_n")).sort("listing_id")
        n = n.with_columns((pl.col("_n").cum_sum() // PIECE).alias("_p"))
        picked = []
        for part in b.join(n.select("listing_id", "_p"), on="listing_id").partition_by("_p", include_key=False):
            u = (part.with_columns(pl.int_ranges("_a", pl.col("_z") + 1).alias("T")).explode("T", empty_as_null=False)
                 .drop("_a", "_z"))
            for s in today:   # a formula event nothing hangs on: judged on the day itself, as at(day) places it
                u = self._point(u.with_columns(pl.col("T").alias(f"{s.id}.anchor_i")), s.id)
            if self.univ is not None:
                u = (u.join(self.frame.select("listing_id", pl.col("i").alias("T"), pl.col(self.univ.name).alias("_u")),
                            on=["listing_id", "T"], how="left").filter(pl.col("_u").fill_null(False)).drop("_u"))
            if u.is_empty():
                continue
            u = self._on_T(u, prefix, uses_end).with_columns(pl.col("T").alias("t_i"))
            u, ev = self._evaluate(u, conds)
            u = self._status(u, ev, ids).filter(pl.col("ok").fill_null(False))
            picked.append(u.sort(["listing_id", "T", *keys], descending=[False, False] + [True] * len(keys))
                          .unique(["listing_id", "T"], keep="first", maintain_order=True))
        if not picked:
            return empty
        b = pl.concat(picked, how="diagonal_relaxed").drop("bid").with_row_index("bid")
        full = self._passing(start, end) if not all_days else pl.DataFrame()
        if not full.is_empty():
            b = b.join(full.select("listing_id", "T").unique(), on=["listing_id", "T"], how="anti")
        if b.is_empty():
            return empty
        b = self._lines(b, self.rule.stages[k + 1] if k + 1 < len(self.rule.stages) else None)
        if as_if_break:
            b = self._as_if_break(b, prefix)
        b = self._ranked(b, "watch")
        dates = self.frame.select("listing_id", pl.col("i").alias("T"), "date")
        return (b.join(dates, on=["listing_id", "T"])
                .select("listing_id", "date", *[pl.col(f"{s.id}.id").alias(s.id) if f"{s.id}.id" in b.columns
                                                  else pl.lit(None, pl.Utf8).alias(s.id) for s in structs],
                        "edge", "rise", "line", "per_bar", "line_of", *[pl.col(c).cast(pl.Float64) for c in ranks])
                .sort("date", "listing_id"))

    def _as_if_break(self, b: pl.DataFrame, stages: list[Stage]) -> pl.DataFrame:
        """Each BOX stage's 假底 on rows of day T as if the box broke on its next bar, from what is known at T's close —
        on the rows whose next bar's high reaches their top, the line a cross buys at (worked out row by row: a check-up's
        every variant over ten years of a market would otherwise wait minutes on rows that never cross)."""
        xs = [s.id for s in stages if s.kind == "structure" and s.use == "BOX" and f"{s.id}.false_bottom" in b.columns]
        if not xs or self.l is None or "edge" not in b.columns:
            return b
        gold = self.tables.get("BOX_DERIVED", {}).get("gold")
        if gold is None:
            gold = pl.DataFrame(schema={"big_box_id": pl.Utf8, "kind": pl.Utf8, "known_at": pl.Date, "small_box_id": pl.Utf8,
                                        "small_bottom": pl.Float64, "superseded_at": pl.Date})
        tol = float(self.rule.primitive_params().get("BOX_DERIVED", {}).get(
            "tol", next(p.default for p in PRIMITIVES["BOX_DERIVED"].params if p.name == "tol")))
        if "as_if" not in self._shared:
            box = self.tables["BOX"]
            self._shared["as_if"] = FalseBottomsIf(box["boxes"], box["touches"], gold, self.frame.select(
                "listing_id", "date", pl.col(self.l.name).alias("low")), tol=tol)
        as_if = self._shared["as_if"]
        nxt = self.frame.select("listing_id", (pl.col("i") - 1).alias("T"), "date", pl.col(self.h.name).alias("_nh"))
        d = (b.select("listing_id", "T", "edge", "rise", *[f"{x}.id" for x in xs]).with_row_index("_r")
             .join(self.frame.select("listing_id", pl.col("i").alias("T"), "date"), on=["listing_id", "T"], how="left")
             .join(nxt.drop("date"), on=["listing_id", "T"], how="left")   # a slack wider than the cross's: a few extra rows
             .filter(pl.col("_nh") >= (pl.col("edge") + pl.col("rise").fill_null(0.0)) * (1 - 1e-6)))
        b = b.with_row_index("_r")
        for x in xs:
            fb = as_if(d.select("listing_id", pl.col(f"{x}.id").alias("box_id"), "date"))
            b = (b.drop(f"{x}.false_bottom").join(d.select("_r").with_columns(fb["false_bottom"].alias(f"{x}.false_bottom")),
                                                   on="_r", how="left", maintain_order="left"))
        return b.drop("_r")

    def _lines(self, b: pl.DataFrame, nxt: Stage | None) -> pl.DataFrame:
        """The line a 潜伏 row's next stage breaks on its day T (`_distances` row by row): a box's top × (1 + tol); a 金品's
        or 箱中箱's small box — flat, its top × (1 + tol); slanted, the line through its two anchor highs extended to T,
        × (1 + the SLANT_BOX tolerance), rising `per_bar` a day."""
        none = pl.lit(None, pl.Float64)
        x, ev = nxt.on.split(".") if nxt is not None and nxt.kind == "event" and nxt.on else (None, None)
        if ev != "break_up":
            return b.with_columns(none.alias("edge"), none.alias("rise"), none.alias("line"), none.alias("per_bar"),
                                  pl.lit(None, pl.Utf8).alias("line_of"))
        if f"{x}.top" in b.columns:
            return b.with_columns(pl.col(f"{x}.top").alias("edge"), pl.lit(0.0).alias("rise"),
                                  (pl.col(f"{x}.top") * (1 + self.tol)).alias("line"), pl.lit(0.0).alias("per_bar"),
                                  pl.col(f"{x}.id").alias("line_of"))
        if f"{x}.small_box_id" not in b.columns:
            return b.with_columns(none.alias("edge"), none.alias("rise"), none.alias("line"), none.alias("per_bar"),
                                  pl.lit(None, pl.Utf8).alias("line_of"))
        b = b.with_columns(pl.col(f"{x}.small_box_id").alias("line_of"))
        slant = self.tables.get("SLANT_BOX", {}).get("slant")
        tol = float(self.rule.primitive_params().get("SLANT_BOX", {}).get(
            "tol", next(p.default for p in PRIMITIVES["SLANT_BOX"].params if p.name == "tol")))
        flat = b.with_columns(pl.col(f"{x}.small_top").alias("edge"), pl.lit(0.0).alias("rise"),
                              (pl.col(f"{x}.small_top") * (1 + self.tol)).alias("line"), pl.lit(0.0).alias("per_bar"))
        if slant is None or slant.is_empty():
            return flat
        s = self._index(self._index(slant.select(pl.col("box_id").alias(f"{x}.small_box_id"), "listing_id", "top_a", "top_b",
                                                 "top_a_price", "top_b_price"), "top_a", "_a"), "top_b", "_z")
        rise = (pl.col("top_b_price") - pl.col("top_a_price")) / (pl.col("_z") - pl.col("_a"))
        edge = pl.col("top_a_price") + rise * (pl.col("T") - pl.col("_a"))
        b = b.join(s.drop("top_a", "top_b"), on=["listing_id", f"{x}.small_box_id"], how="left")
        on = pl.col("_a").is_not_null()
        return b.with_columns(pl.when(on).then(edge).otherwise(pl.col(f"{x}.small_top")).alias("edge"),
                              pl.when(on).then(rise).otherwise(0.0).alias("rise"),
                              pl.when(on).then(edge * (1 + tol)).otherwise(pl.col(f"{x}.small_top") * (1 + self.tol)).alias("line"),
                              pl.when(on).then(rise * (1 + tol)).otherwise(0.0).alias("per_bar")
                              ).drop("top_a_price", "top_b_price", "_a", "_z")

    def signals(self, start: date, end: date, ranks: bool = False) -> pl.DataFrame:
        """Range mode: every complete passing binding finishing in [start, end], one per structure
        instance (its first), one per listing and day; `ranks`: with the rule's ranking values (rank0, …)."""
        structs = [s for s in self.rule.stages if s.kind == "structure"]
        rank_cols = [f"rank{j}" for j in range(len(self.ranks))] if ranks else []
        empty = pl.DataFrame(schema={"listing_id": pl.Utf8, "signal_date": pl.Date, **{s.id: pl.Utf8 for s in structs},
                                     **{c: pl.Float64 for c in rank_cols}})
        b = self._passing(start, end)
        if b.is_empty():
            return empty
        b = b.sort(["T", *self._priority(structs)], descending=[False] + [True] * len(self._priority(structs)))
        for s in structs:
            b = b.unique(f"{s.id}.id", keep="first", maintain_order=True)
        b = b.unique(["listing_id", "T"], keep="first", maintain_order=True)
        if ranks:
            b = self._ranked(b, "signal")
        out = b.select("listing_id", "signal_date", *[pl.col(f"{s.id}.id").alias(s.id) for s in structs],
                       *[pl.col(c).cast(pl.Float64) for c in rank_cols])   # a comparison ranks as 1 / 0, like a batch without one
        return out.sort("signal_date", "listing_id")

    def first(self, lid: str, day: date) -> bool:
        """Whether a signal on `day` is its structures' first completion, as range mode counts it (spec §5.5) — not a box
        broken out of, back in it and broken out of again: the scan signals each, a backtest the first only (you chose,
        10-13: such repeats were 28.7% of 上品's day signals in 60 days; the scan marks them 再次突破). One listing's
        history up to the day, read on its own rows."""
        with self._lock, self._one(lid):
            if self.frame.is_empty():
                return True
            got = self.signals(self.frame["date"].min(), day)
        return got.filter((pl.col("listing_id") == lid) & (pl.col("signal_date") == day)).height > 0

    # ---- the explanation -----------------------------------------------------------------------
    def _date(self, lid: str, i) -> date | None:
        if i is None:
            return None
        r = self.frame.filter((pl.col("listing_id") == lid) & (pl.col("i") == i))
        return r["date"][0] if r.height else None

    def detail(self, lid: str, day: date) -> dict:
        """The explanation tree (spec §5.6) for one listing on `day`: the binding behind its status
        (for a fail or unknown, the candidate that got furthest), every condition with its value,
        threshold and outcome, the deepest stage, the closest miss, and for a watch the distances. The day is
        judged over the whole market first; the listing's tree is then read on its own rows of the frame."""
        with self._lock:
            self.at(day)
            with self._one(lid):
                return self._detail(lid, day)

    @contextmanager
    def _one(self, lid: str):
        """The frame narrowed to one listing's rows — a slice, nothing copied (the frame is in listing, date order, so
        they are one run): each lookup and join of an explanation read every listing's bars, ~1 s a listing,
        ~25 min for the 1,600 潜伏 of one rule on A shares. Rows not in one run (never, as built): the whole frame."""
        if self._spans is None:
            n = self.frame.group_by("listing_id", maintain_order=True).len()
            starts = (n["len"].cum_sum() - n["len"]).to_list()
            self._spans = dict(zip(n["listing_id"].to_list(), zip(starts, n["len"].to_list(), strict=True), strict=True))
        start, n = self._spans.get(lid, (0, 0))
        ids = self.frame["listing_id"]
        if n and not (ids[start] == lid and ids[start + n - 1] == lid and (start + n == len(ids) or ids[start + n] != lid)):
            yield
            return
        whole, quality = self.frame, self.quality
        self.frame, self.quality = whole.slice(start, n), quality.filter(pl.col("listing_id") == lid)
        try:
            yield
        finally:
            self.frame, self.quality = whole, quality

    def _detail(self, lid: str, day: date) -> dict:
        st = self._day(day)
        res = self.at(day).filter(pl.col("listing_id") == lid)
        status = res["status"][0] if res.height else None
        out = {"listing_id": lid, "date": day, "status": status, "bindings": [], "conditions": [], "deepest_stage": None,
               "closest_miss": None, "distances": [], "unknown_reasons": [], "names": self.names}
        if status is None:
            return out
        pick = None
        if status in ("signal", "watch"):
            src, ev = (st["full"], st["full_ev"]) if status == "signal" else (st["watch"], st["watch_ev"])
            chosen = next(p for p in st["picks"] if p.height and p["_st"][0] == status).filter(pl.col("listing_id") == lid)
            row = src.filter(pl.col("bid") == chosen["bid"][0])
            pick = (row, ev)
        else:
            for part, ev in ((st["full"], st["full_ev"]), (st["watch"], st["watch_ev"])):
                if part.is_empty():
                    continue
                p = part.filter(pl.col("listing_id") == lid)
                if p.height == 0:
                    continue
                structs = [s for s in self.rule.stages if s.kind == "structure" and f"{s.id}.id" in p.columns]
                p = p.sort(["depth", *self._priority(structs)], descending=True, nulls_last=True).head(1)
                if pick is None or p["depth"][0] > pick[0]["depth"][0]:
                    pick = (p, ev)
        if pick is None:
            return out
        row, ev = pick[0].row(0, named=True), pick[1]
        ids = [s for s in self.ids if f"{s}.start_i" in pick[0].columns]
        for sid in ids:
            s = self.rule.stage(sid)
            si, ei = row.get(f"{sid}.start_i"), row.get(f"{sid}.end_i")
            out["bindings"].append({"stage": sid, "kind": s.kind, "id": row.get(f"{sid}.id"),
                                    "start": self._date(lid, si), "end": self._date(lid, ei),
                                    "at": self._date(lid, row.get(f"{sid}.anchor_i")),
                                    "bars": ei - si + 1 if si is not None and ei is not None else None})
        misses = []
        for k, sid, cid, kind, op, text in ev:
            ok = row.get(f"c{k}.ok")
            c = {"stage": sid, "id": cid, "kind": kind, "expr": text, "op": op, "value": row.get(f"c{k}.v"),
                 "threshold": row.get(f"c{k}.th"), "status": "unknown" if ok is None else ("pass" if ok else "fail")}
            out["conditions"].append(c)
            if c["status"] == "fail":
                misses.append(c)
        out["deepest_stage"] = ids[row["depth"]] if row.get("depth") is not None and row["depth"] >= 0 else None
        if status == "unknown" or any(c["status"] == "unknown" for c in out["conditions"]):
            out["unknown_reasons"] = self._unknown_reasons(lid, row, ids)
        if misses:
            def gap(c):
                v, th = c["value"], c["threshold"]
                return abs(th - v) / abs(th) if isinstance(v, float) and isinstance(th, float) and th else float("inf")
            first_stage = min(self.ids.index(c["stage"]) for c in misses)
            out["closest_miss"] = min((c for c in misses if self.ids.index(c["stage"]) == first_stage), key=gap)
        if status == "watch" and st["next"] is not None:
            out["distances"] = self._distances(pick[0], st["next"])
        return out

    def _unknown_reasons(self, lid: str, row: dict, ids: list[str]) -> list[dict]:
        """Why conditions may be unknown: a stage reaching before the listing's first bar, and the
        listing's days with a data problem up to T."""
        out = []
        first = min((row[f"{s}.start_i"] for s in ids if row.get(f"{s}.start_i") is not None), default=0)
        if first < 0:
            out.append({"kind": "history", "bars": -first})
        q = self.quality.filter((pl.col("listing_id") == lid) & (pl.col("i") <= row["T"])).sort("date")
        out += [{"kind": "quality", "date": d, "issues": list(x)} for d, x in q.select("date", "quality").iter_rows()]
        # ponytail: formula windows are not tracked, so a listing younger than a year (MA250, the
        # one-year high) is named as a likely cause; track each column's warm-up if this misleads
        if row["T"] + 1 < 250:
            out.append({"kind": "young", "bars": row["T"] + 1})
        return out

    def _distances(self, row: pl.DataFrame, nxt: Stage) -> list[dict]:
        """How far the next stage (an event, as if it happened on the evaluation bar) is: a structure's
        break from its edge, and its single-threshold `at` conditions."""
        if nxt.kind != "event":
            return []
        b = row.with_columns(pl.col("t_i").alias(f"{nxt.id}.anchor_i"))
        x, ev = nxt.on.split(".") if nxt.on else (None, None)
        if x is not None:
            b = b.with_columns((pl.col("t_i") - 1).alias(f"{x}.end_i"))
        b = self._point(b, nxt.id)
        out = []
        b = b.join(self.frame.select("listing_id", "i", pl.col(self.c.name).alias("_c")),
                   left_on=["listing_id", "t_i"], right_on=["listing_id", "i"], how="left")
        r = b.row(0, named=True)
        if ev == "break_up" and r.get(f"{x}.top") is not None:
            out.append({"id": "break_up", "value": r["_c"], "threshold": r[f"{x}.top"] * (1 + self.tol), "edge": r[f"{x}.top"]})
        elif ev == "break_up" and r.get(f"{x}.small_box_id") is not None:   # a 金品 / 箱中箱 breaks with its small box
            out.append(self._small_break(r, x))
        if ev == "break_down" and r.get(f"{x}.bottom") is not None:
            out.append({"id": "break_down", "value": r["_c"], "threshold": r[f"{x}.bottom"] * (1 - self.tol), "edge": r[f"{x}.bottom"]})
        conds = [c for c in self.conds if c[0] == nxt.id and c[2] == "at"]
        b, ev_ = self._evaluate(b.drop("bid").with_row_index("bid"), conds)
        r = b.row(0, named=True)
        for k, sid, cid, kind, op, text in ev_:
            th = r.get(f"c{k}.th")
            if op in _CMP and isinstance(th, float):
                out.append({"id": cid, "stage": sid, "value": r.get(f"c{k}.v"), "threshold": th})
        return out


    def _small_break(self, r: dict, x: str) -> dict:
        """The break line of the small box a 金品 (or 箱中箱) `x` stands on, on the evaluation bar: a flat box's top × (1 +
        the BOX tolerance), as the box primitive breaks it; a slanted one's top line (through its two anchor highs, bar
        by bar) extended to that bar × (1 + the SLANT_BOX tolerance), with `per_bar`, how much that line rises a bar —
        a day after the scan is a bar further along."""
        sid, lid = r[f"{x}.small_box_id"], r["listing_id"]
        slant = self.tables.get("SLANT_BOX", {}).get("slant")
        row = slant.filter(pl.col("box_id") == sid) if slant is not None and slant.height else None
        if row is None or row.is_empty():
            top = r.get(f"{x}.small_top")
            return {"id": "break_up", "value": r["_c"], "threshold": top * (1 + self.tol) if top is not None else None, "edge": top}
        s = row.row(0, named=True)
        at = dict(self.frame.filter(pl.col("listing_id") == lid).select("date", "i").iter_rows())
        a, z, t = at[s["top_a"]], at[s["top_b"]], r["t_i"]
        rise = (s["top_b_price"] - s["top_a_price"]) / (z - a)
        tol = float(self.rule.primitive_params().get("SLANT_BOX", {}).get(
            "tol", next(p.default for p in PRIMITIVES["SLANT_BOX"].params if p.name == "tol")))
        line = s["top_a_price"] + rise * (t - a)
        return {"id": "break_up", "value": r["_c"], "threshold": line * (1 + tol), "edge": line, "per_bar": rise * (1 + tol)}


def A_parse(text: str, where: str):
    from wb.engine.l1.parser import ParseError, parse

    try:
        return parse(text)
    except ParseError as e:
        raise RuleError(f"{where}：{e}") from None


def universe_formula(rule: Rule, market: str, available: set[str] | None) -> tuple[str | None, list[str]]:
    """The rule's stock pool (spec §7.1) as one condition (None: every stock), and what this market's data cannot apply
    (said, never skipped silently)."""
    u, parts, said = rule.universe, [], []
    if "st" in u.exclude and market == "CN":
        if available is None or "is_st" in available:
            parts.append("NOT IS_ST")
        else:
            said.append("当前数据档没有 ST 信息，股票池没有排除 ST")
    if "s_share" in u.exclude and market == "CN":
        parts.append("NOT S_SHARE")
    if u.listed_days:
        parts.append(f"BARSCOUNT(C) >= {u.listed_days}")
    if u.resumed_bars:   # back from a long suspension: its first bars are not a breakout of the box before it (10-13)
        parts.append(f"BARSRESUMED >= {u.resumed_bars}")
    if u.min_avg_amount_20d and market in u.min_avg_amount_20d:
        parts.append(f"MA(AMOUNT, 20) >= {u.min_avg_amount_20d[market]}")
    for fn, names in (("IN_BOARD", u.boards), ("IN_INDUSTRY", u.industries), ("TAGGED", u.tags),
                      ("IN_GROUP", [u.watchlist] if u.watchlist else []), ("IN_INDEX", [u.index] if u.index else [])):
        if names:  # any of the names; each option narrows the pool
            parts.append("(" + " OR ".join(f'{fn}("{n}")' for n in names) + ")")
    return (" AND ".join(parts) if parts else None), said


def compile_errors(rule: Rule) -> list[str]:
    """What stops a rule from running at all, known before any data (spec §6.1: what you confirm is what runs):
    every formula compiled, for each market the rule is for, by the run's own compiler — a misspelled function, a
    field that does not exist, a fixed price against adjusted prices. Empty: it runs. The same mistake reads once."""
    out: list[str] = []
    plan = rule.plan
    lines = [x.line for x in (plan.stop + plan.take)] if plan else []
    for market in rule.markets:
        try:
            Matcher.program(rule, market)
            if lines:   # read on the signal bar as a ranking is
                Matcher.program(rule.model_copy(update={"ranking": [Rank(expr=x) for x in lines]}), market)
            for i, x in enumerate(plan.exit if plan else []):   # on the stock's own bars, each holding day
                try:
                    Compiler(market, {p.id: p.value for p in rule.params}).add(f"exit{i}", x.when, want="cond")
                except (ValueError, ParseError) as e:
                    raise RuleError(f"plan.exit[{i}]（{x.when}）：{e}") from None
        except RuleError as e:
            if str(e) not in out:
                out.append(str(e))
    return out
