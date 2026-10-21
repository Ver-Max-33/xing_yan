"""Formula function declarations (spec §3.5 mechanism 1 and 2).

A function is registered once with its signature, unit rule, scope and a Chinese description; the
compiler checks calls against it and the AI catalog (P3) is generated from the same declaration.

Scopes: "ts" = per security over time (evaluated `.over("listing_id")`), "xs" = per date across
securities (`.over("date")`), "el" = element-wise (inlined into the parent expression)."""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from wb.engine.l1.units import U
from wb.registry import FUNCTIONS

REQUIRED = object()


@dataclass(frozen=True)
class Arg:
    name: str
    kind: str  # "series" (number), "cond" (bool series), "int", "float", "word", "list"
    default: Any = REQUIRED
    choices: tuple = ()
    min: float | None = None


@dataclass(frozen=True)
class Fn:
    name: str
    args: tuple[Arg, ...]
    unit: Any  # a U, or callable(list of series-arg units) -> U
    build: Callable[..., pl.Expr | dict[str, pl.Expr]]  # (ctx, *args)
    doc: str
    category: str
    scope: str = "ts"
    outputs: tuple[tuple[str, U], ...] = ()  # multi-output (MACD.DIF ...): names and units
    aliases: dict[str, str] = field(default_factory=dict)  # output aliases, e.g. MACD.MACD -> HIST
    markets: tuple[str, ...] = ("CN", "JP")
    phase: str | None = None  # declared for the catalog but computed by a later phase
    partition: Any = ("date",)  # xs only: columns, or callable(word args) -> columns
    # a function written as a formula of other functions, from its number / word arguments: each step is compiled
    # (and materialized) on its own — a per-listing window inside a per-day one cannot be one polars expression
    expand: Callable[..., str] | None = None


def S(name: str) -> Arg:
    return Arg(name, "series")


def B(name: str) -> Arg:
    return Arg(name, "cond")


def N(name: str, default: Any = REQUIRED, min: float = 1) -> Arg:
    return Arg(name, "int", default, min=min)


def F(name: str, default: Any = REQUIRED) -> Arg:
    return Arg(name, "float", default)


def W(name: str, choices: tuple, default: Any = REQUIRED) -> Arg:
    return Arg(name, "word", default, choices)


def same(units: list[U]) -> U:
    return units[0]


def register(name: str, args: list[Arg], unit: Any, doc: str, category: str, **kw):
    """Decorator: register `build(ctx, *args)` as formula function `name`."""
    def deco(build):
        FUNCTIONS.register(name)(Fn(name, tuple(args), unit, build, doc, category, **kw))
        return build
    return deco
