"""事件研究的统计 (spec §9.1, §9.5): signals on the same day are one effective sample (their mean), the win rate is the
share of samples with a positive 20-day excess, signals that could not be bought are counted and left out, the split
into 样本内 / 样本外 is fixed by its date, the groups (market, year, industry, the market's state) and the worst cases."""
from datetime import date

import polars as pl
import pytest

from wb.validate.outcomes import HORIZONS
from wb.validate.summary import FEW, summarize


def rows(specs):
    """specs: (market, listing, day, x20, blocked, industry, above200)."""
    out = []
    for m, lid, d, x, blocked, ind, above in specs:
        r = {"market": m, "listing_id": lid, "signal_date": d, "entry_date": d, "blocked": blocked, "mfe": 0.1, "mae": -0.05,
             "gone": False, "industry": ind, "above200": above}
        for h in HORIZONS:
            r[f"r{h}"] = x
            r[f"x{h}"] = x if h <= 20 else None
        out.append(r)
    return pl.DataFrame(out, schema_overrides={"blocked": pl.Utf8, "above200": pl.Boolean})


D1, D2, D3, D4 = date(2023, 3, 1), date(2023, 3, 2), date(2025, 6, 2), date(2025, 6, 3)


def test_same_day_signals_are_one_sample_and_blocked_ones_are_counted_apart():
    r = rows([("CN", "CN:1", D1, 0.10, None, "电子", True), ("CN", "CN:2", D1, -0.04, None, "电子", True),  # D1: +3%
              ("CN", "CN:3", D2, -0.02, None, "银行", False), ("JP", "JP:1", D3, 0.05, None, "電気機器", True),
              ("JP", "JP:2", D4, 0.50, "limit", "電気機器", True)])
    s = summarize(r, oos_start=date(2024, 10, 1))
    assert (s["signals"], s["blocked"], s["effective"]) == (5, {"limit": 1, "halt": 0}, 3)
    assert s["win"] == pytest.approx(2 / 3) and s["median"] == pytest.approx(0.03)
    assert s["split"]["in"]["effective"] == 2 and s["split"]["out"]["effective"] == 1 and s["split"]["out"]["win"] == 1.0
    by = {g["key"]: g for g in s["groups"]["market"]}
    assert (by["CN"]["signals"], by["CN"]["effective"], by["JP"]["signals"], by["JP"]["effective"]) == (3, 2, 2, 1)
    assert {g["key"] for g in s["groups"]["state"]} == {"大盘在 200 日线上", "大盘在 200 日线下"}
    assert [w["listing_id"] for w in s["worst"]][:2] == ["CN:2", "CN:3"]                 # the worst single signals
    assert [h["h"] for h in s["horizons"]] == list(HORIZONS) and s["horizons"][-1]["n"] == 0   # 60 days: none known
    assert s["few"] is True and FEW == 30                                               # under 30: numbers only


def test_the_histogram_counts_every_sample_in_two_percent_steps():
    r = rows([("CN", f"CN:{k}", date(2023, 1, 1 + k), x, None, None, None) for k, x in enumerate([-0.031, -0.01, 0.0, 0.015, 0.25])])
    h = summarize(r, oos_start=date(2024, 1, 1))["hist"]
    assert h["step"] == 0.02 and sum(h["counts"]) == 5 and h["lo"] == pytest.approx(-0.04)


def test_the_next_opening_gap_is_summarised_by_sample_with_the_share_opening_lower():
    rows = pl.DataFrame({"market": ["CN"] * 4, "listing_id": ["a", "b", "c", "d"],
                         "signal_date": [date(2026, 3, 2), date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)],
                         "entry_date": [date(2026, 3, 3), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)],
                         "blocked": [None, "limit", None, None], "gap": [-0.02, 0.04, -0.01, 0.005],
                         **{f"x{h}": [0.01] * 4 for h in (1, 3, 5, 10, 20, 60)}, **{f"r{h}": [0.01] * 4 for h in (1, 3, 5, 10, 20, 60)},
                         "mfe": [0.02] * 4, "mae": [-0.01] * 4, "gone": [False] * 4, "industry": ["x"] * 4, "above200": [True] * 4})
    g = summarize(rows, date(2026, 3, 4))["gap"]
    assert g["n"] == 3 and g["median"] == pytest.approx(0.005) and g["down"] == pytest.approx(1 / 3)   # 3-02 is one sample (+0.01)
