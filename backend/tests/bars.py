"""Daily bars along a path of closes, for the structure tests (BOX and what is built on it)."""
from datetime import date, timedelta

import numpy as np
import polars as pl

D0 = date(2024, 1, 1)
SWING = [9.7, 9.4, 9.1, 9.05, 9.3, 9.6, 9.9, 9.95]    # down to the bottom and back to the top


def bars(path, limit_up=(), wick=0.004):
    c = np.array(path, float)
    o = np.concatenate(([c[0]], c[:-1]))
    return pl.DataFrame({"listing_id": "CN:600000", "date": [D0 + timedelta(days=i) for i in range(len(c))],
                         "open": o, "high": np.maximum(o, c) * (1 + wick), "low": np.minimum(o, c) * (1 - wick), "close": c,
                         "volume": 1000.0, "limit_up_close": [i in limit_up for i in range(len(c))]})
