"""yfinance (test profile only, spec §4.2): JP daily bars for a ~300-stock sample, and indices.

Verified 2026-10-02 (yfinance 1.7.0, 7203.T around the 1:5 split of 2021-09-29): with
auto_adjust=False, Open/High/Low/Close are already divided and Volume multiplied by later splits,
so we undo that with `yahoo_to_raw` and record each split as a corp action.
No amount (turnover) and no limit flags: those columns stay null.
"""
from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd
import polars as pl

from wb.data.http import RateLimiter
from wb.data.normalize import yahoo_to_raw
from wb.data.providers.base import Probe, Provider
from wb.listing import from_yahoo, index_id, to_yahoo
from wb.registry import PROVIDERS

INDEX_TICKERS = {
    "JP": {"^N225": index_id("JP", "N225")},
    "CN": {"000001.SS": index_id("CN", "000001"), "399001.SZ": index_id("CN", "399001"),
           "399006.SZ": index_id("CN", "399006"), "000300.SS": index_id("CN", "000300")},
}
POOL_SEGMENTS = ("プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）")
ALWAYS = ("7203", "6758", "9984", "130A")  # well-known names plus a letter code (spec §4.1)


def _yf_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    return yf.Ticker(ticker).history(start=start, end=end + timedelta(days=1), auto_adjust=False, actions=True)


def sample_pool(instruments: pl.DataFrame, n: int = 300) -> list[str]:
    """Deterministic sample of domestic stocks: evenly spaced by code, plus ALWAYS."""
    codes = (instruments.filter(pl.col("board").is_in(POOL_SEGMENTS)).sort("code")["code"].to_list())
    step = max(1, len(codes) // n)
    picked = set(codes[::step][:n]) | {c for c in ALWAYS if c in codes}
    return sorted(picked)


@PROVIDERS.register("yfinance")
class YFinance(Provider):
    name = "yfinance"

    def __init__(self, market: str, history: Callable[[str, date, date], pd.DataFrame] = _yf_history,
                 limiter: RateLimiter | None = None, today: Callable[[], date] = date.today):
        self.market = market
        self._today = today
        self.unit_kinds = {"index_bars_1d": "ticker"} | ({"bars_1d": "ticker"} if market == "JP" else {})
        self._history = history
        self.limiter = limiter or RateLimiter(60)

    def tickers(self, dataset: str, instruments: pl.DataFrame) -> list[str]:
        if dataset == "index_bars_1d":
            return list(INDEX_TICKERS[self.market])
        return [to_yahoo(f"JP:{c}") for c in sample_pool(instruments)]

    def _frame(self, ticker: str, start: date, end: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Bars in [start, end] and every split from `start` up to today.

        Always downloads through today: Yahoo divides old prices by splits that happened after
        `end` too, so undoing the adjustment needs those later splits (found by spotcheck on
        6758.T, whose 1:5 split of 2024 had leaked into a 2021 backfill)."""
        self.limiter.acquire()
        h = self._history(ticker, start, max(end, self._today()))
        schema = {"date": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                  "close": pl.Float64, "volume": pl.Float64}
        if h is None or h.empty:
            return pl.DataFrame(schema=schema), pl.DataFrame(schema={"ex_date": pl.Date, "ratio": pl.Float64})
        df = pl.DataFrame({"date": [ts.date() for ts in h.index],
                           **{k: h[c].astype(float).tolist() for k, c in
                              [("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"),
                               ("volume", "Volume")]}}, schema=schema).fill_nan(None)  # NaN: no price that day
        splits = pl.DataFrame({"ex_date": df["date"], "ratio": h["Stock Splits"].astype(float).tolist()}
                              ).filter(pl.col("ratio") > 0) if "Stock Splits" in h else \
            pl.DataFrame(schema={"ex_date": pl.Date, "ratio": pl.Float64})
        return df, splits

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        df, splits = self._frame(unit, start, end)
        if dataset == "index_bars_1d":
            return {"index_bars_1d": df.filter(pl.col("date") <= end).with_columns(pl.lit(INDEX_TICKERS[self.market][unit]).alias("index_id"),
                                                     pl.lit(self.name).alias("source"))}
        lid = from_yahoo(unit)
        bars = yahoo_to_raw(df, splits).filter(pl.col("date") <= end).with_columns(
            pl.lit(lid).alias("listing_id"), pl.lit(False).alias("suspended"), pl.lit(self.name).alias("source"))
        actions = splits.filter(pl.col("ex_date") <= end).select(pl.lit(lid).alias("listing_id"), "ex_date", pl.lit("split").alias("kind"),
                                "ratio", pl.lit(self.name).alias("source"))
        return {"bars_1d": bars, "corp_actions": actions}

    def probe(self) -> list[Probe]:
        today = date.today()
        out = []
        for ds, ticker in [("bars_1d", "7203.T"), ("index_bars_1d", next(iter(INDEX_TICKERS[self.market])))]:
            if ds not in self.unit_kinds:
                continue
            try:
                df, _ = self._frame(ticker, today - timedelta(days=14), today)
                last = df["date"].max() if df.height else None
                out.append(Probe(ds, df.height > 0, latest_date=last, rate_per_min=self.limiter.per_minute,
                                 detail="yfinance (test only, ~20 min delayed intraday)"))
            except Exception as e:  # noqa: BLE001
                out.append(Probe(ds, False, detail=str(e)))
        return out

    def ready_count(self, day: date, instruments: pl.DataFrame) -> tuple[int, int]:
        df, _ = self._frame("7203.T", day, day)
        return int((df["date"] == day).any()), 1
