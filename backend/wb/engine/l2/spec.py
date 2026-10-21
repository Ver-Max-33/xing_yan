"""Structure primitive declarations (spec §5.4 统一契约, §3.5 mechanisms 1–2).

Each primitive declares its parameters (type, default, tunable range, meaning), its output tables
and attributes, lifecycle and events, its known_at rule, how it is drawn (only the generic
geometries rect / polyline / point / band / marker, so the chart needs no change for a new one),
the "我的理解" sentence template, the primitives it builds on (`requires`), and a
`compute(bars, params, deps)` returning its tables, where `deps` holds the required primitives'
tables. The contract tests iterate this registry, so every primitive is checked for prefix
consistency (spec §5.8)."""
import math
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from wb.registry import PRIMITIVES, autoload


@dataclass(frozen=True)
class Param:
    name: str
    default: Any
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    doc: str = ""
    choices: tuple[str, ...] = ()  # for a word parameter


@dataclass(frozen=True)
class Table:
    """How a table's rows become known: rows with `known` <= t are known at t; an `evolving` column is
    filled later and only counts once its own date column is <= t; `ignore` columns are running
    summaries checked through other tables."""
    known: str = "known_at"
    evolving: dict[str, str] = field(default_factory=dict)
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Primitive:
    name: str
    doc: str
    params: tuple[Param, ...]
    attributes: dict[str, str]
    states: tuple[str, ...]
    events: tuple[str, ...]
    known_at: str
    draw: dict
    understanding: dict[str, str]  # table -> "我的理解" template over that table's columns (see describe)
    tables: dict[str, Table]
    compute: Callable[..., dict]
    requires: tuple[str, ...] = ()


def register(p: Primitive) -> Primitive:
    PRIMITIVES.register(p.name)(p)
    return p


# touches_top / touches_bottom are the final counts; as of a day they are the touch rows known by
# then (the L3 matcher recounts them), so they are compared through the touches table
BOX_EVOLVING = Table(evolving={"end": "ended_at", "ended_at": "ended_at", "end_reason": "ended_at", "length": "ended_at",
                               "end_i": "ended_at", "superseded_at": "superseded_at"},
                     ignore=("touches_top", "touches_bottom"))


_NAMES = {"scale": {"short": "小", "mid": "中", "long": "大", "slant": "斜"},
          "status": {"established": "", "proto": "（雏形）"},
          "kind": {"high": "高点", "low": "低点", "bull": "底背离", "bear": "顶背离"},
          "direction": {"up": "上涨", "down": "下跌"}, "seek_direction": {"up": "向上", "down": "向下"}}


class _Text(string.Formatter):
    def format_field(self, value, spec):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "—"  # unknown, never "None" or "nan"
        return super().format_field(value, spec)


def describe(name: str, table: str, row: dict) -> str:
    """A row as the primitive's "我的理解" sentence (spec §5.4), with readable names for codes."""
    r = dict(row)
    for col, names in _NAMES.items():
        if col in r:
            r[f"{col}_name"] = names.get(r[col], r[col])
    r["end_text"] = f"，{r['end']} {r['end_reason']}" if r.get("end_reason") else ""
    r["known_text"] = "出现" if r.get("status") == "proto" else "成立"
    r["variant_text"] = "（变异）" if r.get("variant") else ""
    return _Text().format(PRIMITIVES[name].understanding[table], **r)


def check_settings(name: str, values: dict) -> dict:
    """Settings for primitive `name` checked against its declared parameters (names it has, numbers in
    their range, one of the choices for a word). Raises ValueError with every problem."""
    autoload("wb.engine.l2")
    decl = {x.name: x for x in PRIMITIVES[name].params}
    errs = []
    for k, v in values.items():
        d = decl.get(k)
        if d is None:
            errs.append(f"{name} 没有设置 {k}")
        elif d.choices:
            if v not in d.choices:
                errs.append(f"{k} 要选 {'、'.join(d.choices)} 之一")
        elif isinstance(d.default, bool):
            if not isinstance(v, bool):
                errs.append(f"{k} 要写 true / false")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"{k} 要写数字")
        elif (d.lo is not None and v < d.lo) or (d.hi is not None and v > d.hi):
            errs.append(f"{k} 要在 {d.lo} 到 {d.hi} 之间")
    if errs:
        raise ValueError("；".join(errs))
    return values


def compute(bars: pl.DataFrame, names=None, params: dict | None = None) -> dict[str, dict[str, pl.DataFrame]]:
    """The requested primitives (default: all) and what they require, each computed once.
    `params`: {primitive name: {param: value}}."""
    autoload("wb.engine.l2")
    done: dict[str, dict[str, pl.DataFrame]] = {}
    for name in names or sorted(PRIMITIVES):
        _run(name, bars, params or {}, done)
    return done


def _run(name: str, bars: pl.DataFrame, params: dict, done: dict) -> dict[str, pl.DataFrame]:
    # a plain function, not a closure that calls itself: that closure was a reference cycle holding
    # every result table until a full garbage collection (~0.7 GB per worker over a market)
    if name not in done:
        p = PRIMITIVES[name]
        deps = {r: _run(r, bars, params, done) for r in p.requires}
        done[name] = p.compute(bars, params.get(name), deps)
    return done[name]
