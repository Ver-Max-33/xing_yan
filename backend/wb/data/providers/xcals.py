"""Trading calendars from the exchange_calendars package (test profile, spec §4.2/§4.8).

Its holiday tables are hard-coded and end at a fixed year (XSHG: 2026 in v4.13); days past that
bound are simply not in the calendar, and the pipeline refuses to treat them as trading days.
Upgrade the package to extend it. The prod profile uses Tushare trade_cal / J-Quants instead.
"""
from datetime import date

import exchange_calendars as xc
import polars as pl

from wb.data.providers.base import Probe, Provider
from wb.markets import load_market
from wb.registry import PROVIDERS


@PROVIDERS.register("xcals")
class XCals(Provider):
    name = "xcals"
    unit_kinds = {"calendar": "all"}

    def __init__(self, market: str):
        self.market = market
        self.cal_name = load_market(market).xcals_calendar

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        cal = xc.get_calendar(self.cal_name)
        lo = max(start, cal.first_session.date())
        hi = min(end, cal.last_session.date())
        sessions = {s.date() for s in cal.sessions_in_range(lo, hi)} if lo <= hi else set()
        days = pl.date_range(lo, hi, eager=True) if lo <= hi else pl.Series([], dtype=pl.Date)
        return {"calendar": pl.DataFrame({"date": days}).with_columns(
            pl.col("date").is_in(sorted(sessions)).alias("is_open"), pl.lit(self.name).alias("source"))}

    def probe(self) -> list[Probe]:
        cal = xc.get_calendar(self.cal_name)
        return [Probe("calendar", True, cal.first_session.date(), cal.last_session.date(),
                      detail=f"exchange_calendars {xc.__version__} {self.cal_name}")]
