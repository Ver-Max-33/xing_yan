"""Data-layer review 2026-10-13: one row whose prices came ×10 (a decimal shift; replayed on the real 10-12 update with
贵州茅台) was flagged vwap_range and limit_range — which make the volume and the limits unknown — while the rules and the
structures went on reading the ×10 prices. Both checks failing at once means the prices are the wrong ones."""
from datetime import date

import polars as pl

from wb.data.quality import AFFECTS, row_flags

D = [date(2026, 10, 9), date(2026, 10, 12)]


def bars(scale: float) -> pl.DataFrame:
    return pl.DataFrame({"listing_id": ["CN:600519"] * 2, "date": D,
                         "open": [1255.0, 1260.0 * scale], "high": [1270.0, 1290.0 * scale], "low": [1250.0, 1255.0 * scale],
                         "close": [1263.0, 1288.0 * scale], "volume": [3.0e6, 3.2e6], "amount": [3.78e9, 4.1e9],
                         "pre_close": [1255.79, 1263.0], "up_limit": [1381.37, 1389.3], "down_limit": [1130.21, 1136.7],
                         "adj_factor": [8.6463, 8.6463]})


def test_a_row_with_decimal_shifted_prices_has_its_prices_read_as_unknown():
    flags = row_flags(bars(10.0), D)
    q = flags.filter(pl.col("date") == D[1])["quality"].to_list()
    hit = [c for c, cols in AFFECTS.items() if q and c in q[0] and "close" in cols]
    assert hit, f"flags {q}: nothing makes the close unknown"
    assert row_flags(bars(1.0), D).is_empty()       # the real row: no flag at all
