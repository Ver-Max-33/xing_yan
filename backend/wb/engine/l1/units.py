"""Units of formula values (spec §3.5 mechanism 2: the compiler checks units).

Prices are backward-adjusted (hfq) for computation; RAW_* fields are exchange prices for absolute
thresholds (spec §5.8 rule 4). NUMBER is a bare literal and fits anywhere a quantity is expected."""
from enum import Enum


class U(str, Enum):
    PRICE = "价格"
    RAW_PRICE = "原始价"
    VOLUME = "成交量"
    AMOUNT = "成交额"
    RATIO = "比率"
    COUNT = "根数"
    NUMBER = "数值"
    BOOL = "条件"


QUANTITIES = {U.PRICE, U.RAW_PRICE, U.VOLUME, U.AMOUNT}


class UnitError(ValueError):
    pass


def add(a: U, b: U, op: str) -> U:
    _numeric(a, op), _numeric(b, op)
    if a == b:
        return a
    if a == U.NUMBER:
        return b
    if b == U.NUMBER:
        return a
    if {a, b} <= {U.RATIO, U.COUNT}:
        return U.NUMBER
    raise UnitError(f"单位不一致：{a.value} {op} {b.value}")


def mul(a: U, b: U) -> U:
    _numeric(a, "*"), _numeric(b, "*")
    if a == U.NUMBER or a == U.RATIO:
        return b if b != U.NUMBER else a
    if b == U.NUMBER or b == U.RATIO:
        return a
    if {a, b} in ({U.PRICE, U.VOLUME}, {U.RAW_PRICE, U.VOLUME}):
        return U.AMOUNT
    return U.NUMBER


def div(a: U, b: U) -> U:
    _numeric(a, "/"), _numeric(b, "/")
    if a == b:
        return U.RATIO if a != U.NUMBER else U.NUMBER
    if b in (U.NUMBER, U.RATIO, U.COUNT):
        return a
    if a == U.AMOUNT and b == U.VOLUME:
        return U.RAW_PRICE  # amount / volume is an average traded (raw) price
    if a == U.AMOUNT and b in (U.PRICE, U.RAW_PRICE):
        return U.VOLUME
    return U.NUMBER


def compare(a: U, b: U, op: str) -> None:
    _numeric(a, op), _numeric(b, op)
    if U.NUMBER in (a, b) or a == b:
        return
    if {a, b} <= {U.RATIO, U.COUNT, U.NUMBER}:
        return
    raise UnitError(f"不能比较 {a.value} 和 {b.value}")


def unify(a: U, b: U) -> U:
    if a == b:
        return a
    if U.NUMBER in (a, b):
        return b if a == U.NUMBER else a
    if U.BOOL in (a, b):
        raise UnitError(f"IF 的两个分支类型不同：{a.value} 与 {b.value}")
    raise UnitError(f"IF 的两个分支单位不同：{a.value} 与 {b.value}")


def _numeric(u: U, op: str) -> None:
    if u == U.BOOL:
        raise UnitError(f"条件不能参与运算 {op}（用 IF 或 COUNT）")
