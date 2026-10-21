"""事件研究的每个信号 (spec §9.1, §9.4): bought at the next trading day's open, its return and its excess over the
market's benchmark h trading days on (adjusted prices), the best and worst move over 20 days; a next-day open at the
limit up, or a day it did not trade, cannot be bought; a listing that stopped trading before the exit is listed apart,
never dropped; a window past the data is unknown."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.validate.outcomes import HOLD, outcomes

DAYS = [d for d in (date(2026, 1, 5) + timedelta(days=i) for i in range(140)) if d.weekday() < 5][:90]


def publish(paths, bars: dict[str, list[tuple]], index=None):
    """bars: listing -> [(day index, open, close, adj_factor, up_limit)]; high / low are 2% around the bar."""
    rows = [{"listing_id": lid, "date": DAYS[k], "open": o, "high": max(o, c) * 1.02, "low": min(o, c) * 0.98, "close": c,
             "volume": 1e6, "adj_factor": adj, "up_limit": up, "suspended": False, "source": "t"}
            for lid, bs in bars.items() for k, o, c, adj, up in bs]
    idx = index or [(k, 3000.0, 3000.0) for k in range(len(DAYS))]
    Store(paths, "CN").publish({
        "bars_1d": conform(pl.DataFrame(rows), "bars_1d"),
        "instruments": conform(pl.DataFrame({"listing_id": list(bars), "market": "CN", "code": [x[3:] for x in bars],
                                             "name": list(bars), "industry": "电子", "status": "L", "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": DAYS, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "CN:IDX:000300", "date": [DAYS[k] for k, _, _ in idx],
                                               "open": [o for _, o, _ in idx], "high": [max(o, c) for _, o, c in idx],
                                               "low": [min(o, c) for _, o, c in idx], "close": [c for _, _, c in idx],
                                               "source": "t"}), "index_bars_1d"),
    }, as_of=DAYS[-1], note="t")


def rising(n=len(DAYS), start=10.0, step=0.01, **over):
    """Opens at the previous close, closes 1% higher each day."""
    out, prev = [], start
    for k in range(n):
        c = prev * (1 + step)
        out.append(over.get(k, (k, prev, c, 1.0, prev * 1.1)))
        prev = c
    return out


def one(df, lid, day):
    return df.filter((pl.col("listing_id") == lid) & (pl.col("signal_date") == day)).row(0, named=True)


def test_bought_at_the_next_open_its_returns_excess_and_moves(paths):
    publish(paths, {"CN:600519": rising()}, index=[(k, 3000.0 * 1.005 ** k, 3000.0 * 1.005 ** (k + 1)) for k in range(len(DAYS))])
    o = one(outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519"], "signal_date": [DAYS[40]]})), "CN:600519", DAYS[40])
    assert o["entry_date"] == DAYS[41] and o["blocked"] is None
    for h in (1, 5, 20):
        assert o[f"r{h}"] == pytest.approx(1.01 ** h - 1)                        # entry = the 40th close, + h closes
        assert o[f"x{h}"] == pytest.approx(1.01 ** h - 1.005 ** h)               # the index from its entry-day open
    assert o["mfe"] == pytest.approx(1.01 ** HOLD * 1.02 - 1) and o["mae"] == pytest.approx(0.98 - 1)
    assert o["r60"] is None and o["gone"] is False                              # 60 days on is past the data


def test_a_next_day_at_the_limit_up_or_without_trading_cannot_be_bought(paths):
    lim = rising()
    k = 21
    lim[k] = (k, lim[k][4], lim[k][2], 1.0, lim[k][4])                          # opens at the limit price
    halted = [b for b in rising() if b[0] != 31]                                # no bar on day 31
    publish(paths, {"CN:600519": lim, "CN:000001": halted})
    out = outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519", "CN:000001"], "signal_date": [DAYS[20], DAYS[30]]}))
    assert one(out, "CN:600519", DAYS[20])["blocked"] == "limit" and one(out, "CN:000001", DAYS[30])["blocked"] == "halt"


def test_a_listing_that_stopped_trading_is_listed_apart_and_a_split_does_not_move_the_return(paths):
    split = [(k, o * (2 if k < 40 else 1), c * (2 if k < 40 else 1), 0.5 if k < 40 else 1.0, up * (2 if k < 40 else 1))
             for k, o, c, _, up in rising()]
    publish(paths, {"CN:600519": split, "CN:000001": rising(n=45)})              # 000001's bars end on day 44
    out = outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519", "CN:000001"], "signal_date": [DAYS[30], DAYS[30]]}))
    s = one(out, "CN:600519", DAYS[30])
    assert s["r20"] == pytest.approx(1.01 ** 20 - 1)                             # adjusted across the 2:1 split
    g = one(out, "CN:000001", DAYS[30])
    assert g["gone"] is True and g["r20"] is None and g["r10"] == pytest.approx(1.01 ** 10 - 1)


def test_a_day_without_its_price_factor_carries_the_last_one(paths):
    # Tushare's factor call is optional: a day without it is not a 1:1 price
    rows = [(k, o * 3, c * 3, 1 / 3, up * 3) for k, o, c, _, up in rising()]
    rows[50] = (*rows[50][:3], None, rows[50][4])
    publish(paths, {"CN:600519": rows})
    o = one(outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519"], "signal_date": [DAYS[40]]})), "CN:600519", DAYS[40])
    assert o["r10"] == pytest.approx(1.01 ** 10 - 1)


def test_a_signal_on_the_datas_last_day_is_not_yet_bought_not_halted(paths):
    publish(paths, {"CN:600519": rising()})
    o = one(outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519"], "signal_date": [DAYS[-1]]})), "CN:600519", DAYS[-1])
    assert o["entry_date"] is None and o["blocked"] is None and o["r1"] is None          # no next day yet: unknown


def test_a_benchmark_without_bars_stops_the_study_with_the_reason(paths):
    from wb.engine.l3.rule import RuleError
    publish(paths, {"CN:600519": rising()}, index=[(0, 3000.0, 3000.0)])                 # the index has one day only
    with pytest.raises(RuleError, match="基准 CN:IDX:000300"):
        outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519"], "signal_date": [DAYS[40]]}))


def test_the_next_days_opening_gap_against_the_signal_close(paths):
    # a close far above the day's average tends to open lower the next day — what opened next is recorded
    # (adjusted, so an ex-date in between is no gap), also on a day opening at the limit (the gap is there, the buy is not)
    bars = rising()
    bars[41] = (41, bars[40][2] * 0.97, bars[40][2] * 0.98, 1.0, bars[40][2] * 1.1)        # opens 3% below day 40's close
    top = rising(start=20.0)
    top[41] = (41, 22.0, 22.0, 1.0, 22.0)                                                # opens at its limit up
    publish(paths, {"CN:600519": bars, "CN:000001": top})
    got = outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519", "CN:000001"], "signal_date": [DAYS[40], DAYS[40]]}))
    assert one(got, "CN:600519", DAYS[40])["gap"] == pytest.approx(-0.03)
    limit = one(got, "CN:000001", DAYS[40])
    assert limit["blocked"] == "limit" and limit["gap"] == pytest.approx(22.0 / top[40][2] - 1)


def test_an_ex_date_the_next_morning_is_no_opening_gap(paths):
    # adjusted prices: a 10-for-10 bonus the next morning halves the exchange's open, and the stock opened flat
    bars = rising()
    bars = bars[:41] + [(k, o / 2, c / 2, 2.0, up / 2) for k, o, c, _, up in bars[41:]]
    publish(paths, {"CN:600519": bars})
    got = outcomes(paths, "CN", pl.DataFrame({"listing_id": ["CN:600519"], "signal_date": [DAYS[40]]}))
    assert one(got, "CN:600519", DAYS[40])["gap"] == pytest.approx(0.0, abs=1e-9)
