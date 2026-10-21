"""Formula syntax tree (spec §5.3). Nodes are frozen dataclasses so equal formulas compare and hash
equal: the compiler deduplicates identical sub-expressions by hashing them."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    pos: int = field(default=0, compare=False, repr=False, kw_only=True)  # character offset, for messages


@dataclass(frozen=True)
class Num(Node):
    value: float | tuple  # a list literal like [5, 10, 20] is a tuple


@dataclass(frozen=True)
class Name(Node):
    name: str  # a field (C), a zero-argument function (VOL_MULT), a structure or stage (box), a word (up)


@dataclass(frozen=True)
class Param(Node):
    name: str  # $box_tol


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple = ()
    kwargs: tuple = ()  # ((key, Node), ...)


@dataclass(frozen=True)
class Attr(Node):
    obj: Node
    name: str  # MACD.DIF, box.top


@dataclass(frozen=True)
class Unary(Node):
    op: str  # "-" | "NOT"
    x: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str  # + - * / > < >= <= == != AND OR
    a: Node
    b: Node


@dataclass(frozen=True)
class At(Node):
    x: Node
    stage: str  # expr@stage: the value on that stage's anchor bar (rules only)
