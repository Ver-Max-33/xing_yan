"""Tushare 实时日线 rt_k as the CN realtime source of the prod profile (spec §4.8): each poll asks for the followed
stocks' day so far — open, high, low, the price now, volume (shares) and turnover (yuan) since the open, the previous
close — in one call (up to 6000 rows). It needs Tushare's separate A股日线RT permission; without it the call says so
(PermissionDenied) and the page shows it: nothing switches to another source quietly. Index quotes need yet another
permission, so the index strip stays on the daily closes."""
from collections.abc import Callable, Iterable
from datetime import date, datetime

import polars as pl

from wb.listing import parse_id
from wb.registry import REALTIME

SNAPSHOT = {"listing_id": pl.Utf8, "at": pl.Datetime, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
            "last": pl.Float64, "volume": pl.Float64, "amount": pl.Float64, "pre_close": pl.Float64}
FIELDS = "ts_code,pre_close,open,high,low,close,vol,amount,trade_time"
SUFFIX = {"6": "SH", "9": "SH", "0": "SZ", "2": "SZ", "3": "SZ", "4": "BJ", "8": "BJ"}


def ts_code(listing_id: str) -> str:
    _, code = parse_id(listing_id)
    return f"{code}.{'BJ' if code.startswith('92') else SUFFIX[code[0]]}"


def _when(at, day: date) -> datetime | None:
    """trade_time as "YYYY-MM-DD HH:MM:SS" or "HH:MM:SS" (the documentation does not say which; check once enabled)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            t = datetime.strptime(str(at), fmt)
        except ValueError:
            continue
        return t if fmt.startswith("%Y") else datetime.combine(day, t.time())
    return None


@REALTIME.register("tushare_rt")
class TushareRealtime:
    name = "tushare_rt"
    kind = "snapshot"   # the day so far, not minute bars: the minutes are put together from successive polls
    delay = 0
    poll_seconds = 60
    max_listings = None  # one call answers for the whole market
    approx_volume = False

    def __init__(self, market: str = "CN", call: Callable[[str, str, dict], list[list]] | None = None):
        self.market, self._call = market, call

    def snapshot(self, listing_ids: Iterable[str], day: date) -> pl.DataFrame:
        """listing_id, at (the exchange's time of the last trade), open, high, low, last, volume and amount since the
        open, pre_close — stocks that have traded on `day` only."""
        ids = [x for x in listing_ids if ":IDX:" not in x]
        if not ids:
            return pl.DataFrame(schema=SNAPSHOT)
        if self._call is None:
            from wb.data.providers.tushare import Tushare
            self._call = Tushare()._call
        rows = self._call("rt_k", FIELDS, {"ts_code": ",".join(ts_code(x) for x in ids)})
        out = []
        for code, pre, o, h, low, last, vol, amount, at in rows:
            when = _when(at, day)
            if when is None or when.date() != day or not last:   # not traded yet today, or yesterday's close
                continue
            out.append((f"CN:{code.split('.')[0]}", when, o, h, low, last, vol, amount, pre))
        return pl.DataFrame(out, schema=SNAPSHOT, orient="row")
