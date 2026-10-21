"""盘中量能的折算 (spec §7.2; design 2026-10-10 §6): the day's volume estimated from what has traded so far on the
market's volume clock — the share of a day's volume that has usually traded by the end of each minute of the session —
instead of linearly (V × total ÷ minutes traded), which the mornings overstate: on a year of A-share minutes the
linear estimate was 3.8× the day at 09:34, 2.4× at 10:00, 1.3× at 11:30, while the clock's was unbiased from 10:30 and
within 2% at 14:45. The clock is the median over the kept days of the market index's minutes (at least MIN_DAYS of
them), else the default measured on 上证指数 over 2025-10—2026-10 (241 days); a market with neither is linear."""
import io
from pathlib import Path

import polars as pl

from wb.config import Paths
from wb.realtime.session import minute_index
from wb.markets import load_market

MIN_DAYS = 5
INDEX = {"CN": "CN:IDX:000001"}
# CN: the share done by the end of minute i (0 = 09:30's, the opening auction in it; 120 = 13:00's; 239 = 14:59's, the
# closing auction in it), 上证指数's minutes from TickFlow, median of 241 days (2025-10-10 — 2026-10-08)
DEFAULT: dict[str, tuple[float, ...]] = {"CN": (
    0.0352, 0.0575, 0.0750, 0.0898, 0.1043, 0.1183, 0.1325, 0.1452, 0.1572, 0.1686, 0.1806, 0.1904,
    0.2013, 0.2112, 0.2203, 0.2290, 0.2382, 0.2459, 0.2535, 0.2606, 0.2698, 0.2768, 0.2845, 0.2918,
    0.2982, 0.3052, 0.3108, 0.3165, 0.3233, 0.3295, 0.3359, 0.3422, 0.3479, 0.3536, 0.3593, 0.3655,
    0.3712, 0.3754, 0.3807, 0.3857, 0.3905, 0.3949, 0.3993, 0.4044, 0.4097, 0.4139, 0.4190, 0.4236,
    0.4274, 0.4314, 0.4351, 0.4396, 0.4439, 0.4479, 0.4520, 0.4556, 0.4596, 0.4630, 0.4670, 0.4705,
    0.4751, 0.4792, 0.4827, 0.4865, 0.4903, 0.4945, 0.4976, 0.5009, 0.5050, 0.5087, 0.5112, 0.5143,
    0.5173, 0.5201, 0.5241, 0.5273, 0.5302, 0.5332, 0.5361, 0.5390, 0.5423, 0.5450, 0.5483, 0.5515,
    0.5544, 0.5569, 0.5596, 0.5626, 0.5650, 0.5683, 0.5707, 0.5737, 0.5767, 0.5790, 0.5819, 0.5846,
    0.5869, 0.5893, 0.5919, 0.5941, 0.5965, 0.5990, 0.6014, 0.6038, 0.6059, 0.6084, 0.6110, 0.6133,
    0.6155, 0.6178, 0.6201, 0.6223, 0.6246, 0.6266, 0.6290, 0.6313, 0.6334, 0.6354, 0.6376, 0.6399,
    0.6491, 0.6540, 0.6566, 0.6599, 0.6625, 0.6654, 0.6681, 0.6709, 0.6735, 0.6766, 0.6798, 0.6824,
    0.6852, 0.6879, 0.6905, 0.6937, 0.6968, 0.6997, 0.7021, 0.7046, 0.7073, 0.7102, 0.7129, 0.7161,
    0.7184, 0.7210, 0.7237, 0.7262, 0.7290, 0.7314, 0.7352, 0.7376, 0.7400, 0.7423, 0.7451, 0.7477,
    0.7507, 0.7530, 0.7557, 0.7580, 0.7606, 0.7632, 0.7657, 0.7681, 0.7703, 0.7730, 0.7753, 0.7775,
    0.7800, 0.7824, 0.7849, 0.7871, 0.7896, 0.7923, 0.7945, 0.7968, 0.7995, 0.8014, 0.8043, 0.8071,
    0.8102, 0.8130, 0.8154, 0.8178, 0.8202, 0.8225, 0.8250, 0.8273, 0.8296, 0.8320, 0.8345, 0.8373,
    0.8395, 0.8417, 0.8441, 0.8469, 0.8492, 0.8521, 0.8548, 0.8568, 0.8598, 0.8620, 0.8640, 0.8661,
    0.8688, 0.8711, 0.8735, 0.8760, 0.8785, 0.8812, 0.8844, 0.8878, 0.8908, 0.8939, 0.8970, 0.8999,
    0.9031, 0.9058, 0.9087, 0.9118, 0.9155, 0.9190, 0.9221, 0.9256, 0.9292, 0.9333, 0.9373, 0.9410,
    0.9451, 0.9494, 0.9544, 0.9592, 0.9640, 0.9692, 0.9748, 0.9813, 0.9886, 0.9890, 0.9890, 1.0000,
)}


def from_minutes(minutes: pl.DataFrame, index_id: str, market: str = "CN") -> tuple[float, ...] | None:
    """The clock of the kept days of `index_id` that cover the whole session (a day the app was closed for a while
    does not count); None with fewer than MIN_DAYS of them."""
    sessions = load_market(market).sessions
    total = sum((b.hour * 60 + b.minute) - (a.hour * 60 + a.minute) for a, b in sessions)
    m = (minutes.filter(pl.col("listing_id") == index_id)
         .with_columns(pl.col("at").dt.date().alias("_d"), minute_index(sessions, pl.col("at")).alias("_i"))
         .filter(pl.col("_i") < total))
    whole = m.group_by("_d").agg(pl.col("_i").n_unique().alias("n")).filter(pl.col("n") == total)["_d"]
    if whole.len() < MIN_DAYS:
        return None
    p = (m.filter(pl.col("_d").is_in(whole.implode())).sort("_d", "_i")
         .with_columns((pl.col("volume").cum_sum().over("_d") / pl.col("volume").sum().over("_d")).alias("_p"))
         .group_by("_i").agg(pl.col("_p").median()).sort("_i"))
    return tuple(float(x) for x in p["_p"].to_list())


def clock_for(paths: Paths, market: str) -> tuple[float, ...] | None:
    """The market's clock from the minutes kept here (盘中 keeps 20 days), else its default, else None (linear)."""
    folder = paths.market_dir(market) / "intraday"
    iid = INDEX.get(market)
    if iid and folder.exists():
        files = sorted(folder.glob("date=*.parquet"))
        if len(files) >= MIN_DAYS:
            kept = pl.concat([pl.read_parquet(io.BytesIO(Path(f).read_bytes())).filter(pl.col("listing_id") == iid) for f in files],
                             how="diagonal_relaxed")
            own = from_minutes(kept, iid, market) if kept.height else None
            if own:
                return own
    return DEFAULT.get(market)


def share(clock: tuple[float, ...] | None, covered: int, total: int) -> float:
    """The share of the day's volume done once `covered` minutes have traded: the clock's, else linear."""
    covered = max(1, min(covered, total))
    return clock[covered - 1] if clock else covered / total
