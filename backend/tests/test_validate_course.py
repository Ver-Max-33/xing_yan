"""能拿你自己的数据检验的选股说法: each a single-condition test ready to run — its formula
compiles in both markets, and what to look at in the result is said."""
import pytest

from wb.data.store import Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns
from wb.validate.course import CHECKS


@pytest.mark.parametrize("market", ["CN", "JP"])
def test_every_course_claim_compiles_as_a_single_condition(paths, market):
    comp = Compiler(market, available=available_columns(Store(paths, market)))
    for c in CHECKS:
        comp.add(c["id"], c["expr"], want="cond")   # a condition, as the single test reads it
    assert comp.program().columns >= {"close"}
    assert len({c["id"] for c in CHECKS}) == len(CHECKS) and all(c["label"] and c["look"] for c in CHECKS)
    assert next(c for c in CHECKS if c["id"] == "vwap5")["expr"] == "(C - VWAP) / REF(C, 1) >= 0.05"   # in points


def test_the_setup_lists_them(paths):
    from wb.validate.jobs import setup
    assert setup(lambda m: paths, ["CN"])["checks"] == CHECKS
