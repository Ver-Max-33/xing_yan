"""yfinance as a realtime source (the test profile, spec §4.8): today's 1-minute bars of stocks and indexes, about 20
minutes behind, in one call a poll. Not an official feed — it carries the intraday path end to end (list, minutes
chart, alerts, Feishu) before a realtime subscription; the prod profile routes to Tushare rt_k and Tachibana."""
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import polars as pl

from wb.data.providers.yfinance_test import INDEX_TICKERS
from wb.listing import parse_id
from wb.markets import load_market
from wb.registry import REALTIME

CN_SUFFIX = {"6": "SS", "9": "SS", "0": "SZ", "2": "SZ", "3": "SZ"}  # 4 / 8: 北交所, which Yahoo does not carry
MINUTES = {"listing_id": pl.Utf8, "at": pl.Datetime, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
           "close": pl.Float64, "volume": pl.Float64}


def yahoo_ticker(listing_id: str) -> str | None:
    market, code = parse_id(listing_id)
    if market == "JP":
        return f"{code}.T"
    suffix = CN_SUFFIX.get(code[0])
    return f"{code}.{suffix}" if suffix else None


def _download(tickers: list[str], day: date) -> pd.DataFrame:
    """The day's minutes only (start/end, not five days), one ticker after another: gentle on Yahoo, whose throttling
    shows as tickers quietly missing."""
    import yfinance as yf
    return yf.download(tickers, start=day, end=day + timedelta(days=1), interval="1m", group_by="ticker", prepost=False,
                       progress=False, threads=False, auto_adjust=False)


@REALTIME.register("yfinance_rt")
class YahooRealtime:
    name = "yfinance_rt"
    kind = "minutes"    # minute bars as they are (the prod sources give the day so far: "snapshot")
    delay = 20          # minutes behind the market
    poll_seconds = 120  # one call fetches every ticker of the poll: kept gentle
    max_listings = 50   # spec §4.2: about 50 stocks a poll
    history_days = 5    # Yahoo keeps about 7 days of 1-minute bars: the multi-day 分时 fills the days before
    # measured 2026-10-02 against the exchanges' daily volume: Shenzhen minutes add up to 1.3–1.5x it, Tokyo's start at
    # 9:05 and miss both auctions (62%), Shanghai's miss the closing auction (96%) — the volume ratio is only a sketch
    approx_volume = True

    def __init__(self, market: str, download: Callable[[list[str], date], pd.DataFrame] = _download):
        self.market, self.download = market, download
        self.tz = ZoneInfo(load_market(market).timezone)

    def minutes(self, listing_ids: Iterable[str], day: date, indexes: Iterable[str] = ()) -> pl.DataFrame:
        """listing_id (a stock or an index id), at (exchange local time), open, high, low, close, volume of each
        minute of `day` so far; empty when the source has not reached the day."""
        by_index = {iid: tk for tk, iid in INDEX_TICKERS[self.market].items()}
        tickers = {tk: lid for lid in listing_ids if (tk := yahoo_ticker(lid))}
        tickers |= {by_index[i]: i for i in indexes if i in by_index}
        if not tickers:
            return pl.DataFrame(schema=MINUTES)
        raw = self.download(list(tickers), day)
        parts = []
        for tk, lid in tickers.items():
            if tk not in raw.columns.get_level_values(0):
                continue
            df = raw[tk].dropna(subset=["Close"])
            at = df.index.tz_convert(self.tz).tz_localize(None).as_unit("us")  # yfinance gives seconds; polars reads us
            keep = at.date == day
            if keep.any():
                parts.append(pl.DataFrame({
                    "listing_id": lid, "at": at[keep].to_numpy(),
                    **{k: df[c].to_numpy()[keep].astype(float) for k, c in
                       (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume"))}},
                    schema=MINUTES))
        return pl.concat(parts) if parts else pl.DataFrame(schema=MINUTES)
