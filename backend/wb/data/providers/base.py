"""Provider contract (spec §3.5 mechanism 3).

A provider turns one *unit* of a dataset into standard frames:
  unit kind "date"    one trading day for the whole market (Tushare daily, J-Quants bars)
  unit kind "all"     the whole dataset in one go (instrument lists, calendars, industry)
  unit kind "ticker"  one security over [start, end] (yfinance)
  unit kind "period"  one report period (a quarter end) of every company (financial reports, spec §4.7)
`fetch` may return several datasets from one call, e.g. J-Quants bars also yield split events.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import polars as pl


class Unavailable(Exception):
    """The account's plan/tier does not include this data (or no credentials). Not retried."""


@dataclass(frozen=True)
class Probe:
    dataset: str
    available: bool
    history_start: date | None = None
    latest_date: date | None = None
    rate_per_min: int | None = None
    detail: str = ""


class Provider(ABC):
    name: str
    market: str
    unit_kinds: dict[str, str]
    complete_list: bool = False  # its instruments list covers every current listing of the market
    index_since: dict = {}       # index_id -> first day the source has rows (later-launched indexes)

    @abstractmethod
    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]: ...

    @abstractmethod
    def probe(self) -> list[Probe]: ...

    def tickers(self, dataset: str, instruments: pl.DataFrame) -> list[str]:
        raise NotImplementedError(f"{self.name} has no ticker units")

    def ready_count(self, day: date, instruments: pl.DataFrame) -> tuple[int, int]:
        """(rows the source has for `day`, rows expected). Used by readiness detection (spec §4.5)."""
        raise NotImplementedError(f"{self.name} cannot report readiness")


def ymd(col: str) -> pl.Expr:
    """'20261002' -> Date; empty strings and nulls -> null."""
    return pl.col(col).cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d", strict=False).alias(col)


def listed_on(instruments: pl.DataFrame, day: date) -> pl.DataFrame:
    """Instruments that should trade on `day` according to their listing dates."""
    return instruments.filter(
        (pl.col("list_date").is_null() | (pl.col("list_date") <= day))
        & (pl.col("delist_date").is_null() | (pl.col("delist_date") > day))
        & (pl.col("status") != "D")
    )
