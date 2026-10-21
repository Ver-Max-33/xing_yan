"""Index members (spec §7.1 股票池: 指数成分): a listing is in an index from the day after the month-end list that has it,
out from the day after a list without it, unknown before the first list; TOPIX100 / TOPIX500 / Core30 come from
TOPIX's size classes. Rules read it with IN_INDEX and universe.index."""
from datetime import date

import polars as pl
import pytest

from tests.test_l3_run import ABOVE_MA
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.compiler import CompileError, Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l3.rule import load_rule
from wb.engine.l3.run import run_rule

D1, D2 = date(2026, 8, 31), date(2026, 9, 15)


def members(paths, rows):
    st = Store(paths, "CN")
    df = pl.DataFrame(rows, schema={"index_id": pl.Utf8, "date": pl.Date, "listing_id": pl.Utf8, "weight": pl.Float64,
                                    "segment": pl.Utf8, "source": pl.Utf8}, orient="row")
    st.publish({"index_members": df}, as_of=st.latest().as_of, note="members")


def test_a_listing_is_in_an_index_from_the_day_after_the_list_that_has_it(paths):
    write(paths, 320)
    members(paths, [("CN:IDX:000300", D1, "CN:600519", 3.2, None, "t"), ("CN:IDX:000300", D2, "CN:000001", 1.0, None, "t")])
    st = Store(paths, "CN")
    assert "in:CN:IDX:000300" in available_columns(st)
    f = build_frame(st, "CN", "test", {"in:CN:IDX:000300"}).filter(pl.col("listing_id") == "CN:600519")
    at = dict(zip(f["date"], f["in:CN:IDX:000300"], strict=True))
    assert at[D1] is None                                                       # the list is out at the close of D1
    assert at[min(d for d in at if d > D1)] is True and at[D2] is True
    assert at[min(d for d in at if d > D2)] is False                            # not in the next list: out


def test_in_index_reads_the_membership_and_a_rule_can_keep_to_one_index(paths):
    write(paths, 320)
    members(paths, [("CN:IDX:000300", D1, "CN:600519", 3.2, None, "t")])
    rule = load_rule(ABOVE_MA.replace("markets: [CN, JP]", "markets: [CN]").replace("signal_at:", "universe: {index: CN:IDX:000300}\nsignal_at:"))
    assert rule.universe.index == "CN:IDX:000300"
    assert run_rule(paths, rule, "CN", record=False).rows["listing_id"].to_list() == ["CN:600519"]
    with pytest.raises(CompileError, match="指数成分"):
        Compiler("CN", available={"close"}).add("x", 'IN_INDEX("CN:IDX:000300")')


def test_topix100_and_500_are_topixs_size_classes(paths):
    from wb.engine.l1.frame import index_membership
    rows = pl.DataFrame({"index_id": ["JP:IDX:TOPIX"] * 3, "date": [D1] * 3, "listing_id": ["JP:7203", "JP:6758", "JP:1301"],
                         "weight": [3.9, 2.0, 0.01], "segment": ["TOPIX Core30", "TOPIX Mid400", "TOPIX Small 1"], "source": "t"})
    m = index_membership(rows)
    def of(iid):
        return sorted(m.filter(pl.col("index_id") == iid)["listing_id"])
    assert of("JP:IDX:TOPIX") == ["JP:1301", "JP:6758", "JP:7203"] and of("JP:IDX:TOPIX500") == ["JP:6758", "JP:7203"]
    assert of("JP:IDX:TOPIX100") == ["JP:7203"] and of("JP:IDX:CORE30") == ["JP:7203"]
