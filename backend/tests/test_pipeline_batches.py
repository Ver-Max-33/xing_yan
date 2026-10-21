"""Data-layer review 2026-10-13: the day-over-day checks of a daily update must see the day before for every listing,
also on a day another listing has a new corporate action (on the real 10-12 update, 16 ex-dates: a wrong adj_factor on
贵州茅台 that day was not reported; the same day without the ex-dates reported it)."""
from tests.test_pipeline import DAYS, FakeJP, _issues, pipe


def test_the_day_before_is_checked_also_on_a_day_with_another_listings_split(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[0])
    # +80% for 130A with no corporate action, while 7203 splits the same day (its whole history comes into the batch)
    snap = pipe(paths, FakeJP(closes={("JP:130A", DAYS[1]): 900.0}, splits={("JP:7203", DAYS[1]): 2.0})).update(DAYS[1], wait=False)
    assert ("adj_jump", "JP:130A", DAYS[1].isoformat()) in _issues(p, snap)


from datetime import timedelta  # noqa: E402

import polars as pl  # noqa: E402
import pytest  # noqa: E402

from wb.data.pipeline import QualityError  # noqa: E402
from wb.data.providers.base import Provider  # noqa: E402

IDS = [f"JP:{1300 + i}" for i in range(25)]


class Lots(Provider):
    """25 listings; `lots`: the day's volume left in lots of 100 (amount ÷ volume = 100 × the price: a unit error)."""
    name, market, complete_list = "jquants", "JP", True
    unit_kinds = {"calendar": "all", "instruments": "all", "bars_1d": "date"}

    def __init__(self, lots=False):
        self.lots, self.asked = lots, []

    def fetch(self, dataset, unit, start, end):
        self.asked.append(dataset)
        if dataset == "calendar":
            days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            return {"calendar": pl.DataFrame({"date": days, "is_open": [d.weekday() < 5 for d in days], "source": "fake"})}
        if dataset == "instruments":
            return {"instruments": pl.DataFrame({"listing_id": IDS, "market": "JP", "code": [i[3:] for i in IDS], "status": "L",
                                                 "source": "jquants"})}
        shares = 1000.0 + int(unit[-2:])   # a day of its own: a market never repeats yesterday exactly
        return {"bars_1d": pl.DataFrame({"listing_id": IDS, "date": unit, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                                         "volume": shares / 100 if self.lots else shares, "amount": 100.5 * shares,
                                         "suspended": False, "source": "jquants"}
                                        ).with_columns(pl.col("date").str.to_date())}

    def probe(self):
        return []

    def ready_count(self, day, instruments):
        return 25, 25


def test_a_refused_batch_is_set_aside_and_the_next_update_asks_the_source_again(paths):
    pipe(paths, Lots()).backfill(DAYS[0], DAYS[0])
    with pytest.raises(QualityError, match="units"):
        pipe(paths, Lots(lots=True)).update(DAYS[1], wait=False)
    fixed = Lots()                                   # the source corrected its answer meanwhile
    p = pipe(paths, fixed)
    snap = p.update(DAYS[1], wait=False)
    assert "bars_1d" in fixed.asked and snap.as_of == DAYS[1]
    assert p.store.read("bars_1d").filter(pl.col("date") == DAYS[1])["volume"].to_list() == [1000.0 + DAYS[1].day] * 25
    blocked = p.staging.dir / "blocked"
    assert list(blocked.glob("*/bars_1d/*.parquet"))  # the refused batch, kept for inspection
