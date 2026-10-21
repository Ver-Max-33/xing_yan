"""Market profiles: one YAML file per market (spec §3.5 mechanism 2)."""
from dataclasses import dataclass
from datetime import date, time
from functools import cache
from pathlib import Path

import yaml

from wb.listing import parse_id

MARKETS = ("CN", "JP")
_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Market:
    code: str
    timezone: str
    currency: str
    lot_size: int
    sessions: tuple[tuple[time, time], ...]
    ready_start: time
    ready_end: time
    poll_minutes: int
    refetch_at: time
    xcals_calendar: str
    limit_rule: str
    benchmark: dict[str, str]
    price_limits: tuple[tuple[float | None, float], ...] = ()  # JP: (base price bound, width)
    halts: tuple[date, ...] = ()   # whole days the exchange halted all trading (every row without prices is right)
    non_stock_boards: tuple[str, ...] = ()   # boards that are not stocks: left out of the stock pool (spec §7.1)


def _t(s: str) -> time:
    return time.fromisoformat(s)


@cache
def load_market(code: str) -> Market:
    if code not in MARKETS:
        raise ValueError(f"unknown market {code!r}")
    raw = yaml.safe_load((_DIR / f"{code.lower()}.yaml").read_text(encoding="utf-8"))
    rw = raw["ready_window"]
    return Market(
        code=raw["market"],
        timezone=raw["timezone"],
        currency=raw["currency"],
        lot_size=int(raw["lot_size"]),
        sessions=tuple((_t(a), _t(b)) for a, b in raw["sessions"]),
        ready_start=_t(rw["start"]),
        ready_end=_t(rw["end"]),
        poll_minutes=int(rw["poll_minutes"]),
        refetch_at=_t(raw["refetch_at"]),
        xcals_calendar=raw["xcals_calendar"],
        limit_rule=raw["limit_rule"],
        benchmark=dict(raw["benchmark"]),
        price_limits=tuple((b, float(w)) for b, w in raw.get("price_limits", [])),
        halts=tuple(date.fromisoformat(str(d)) for d in raw.get("halts", [])),
        non_stock_boards=tuple(raw.get("non_stock_boards", [])),
    )


def limit_width(market: Market, base_price: float) -> float:
    """TSE limit width in yen for a base price (normally the previous close)."""
    for bound, width in market.price_limits:
        if bound is None or base_price < bound:
            return width
    raise ValueError(f"{market.code} has no price-limit table")


def cn_limit_pct(code: str) -> float:
    """An A share's daily limit by its board, from the code: STAR and ChiNext 20%, Beijing 30%, the main boards 10%
    (an ST stock's 5% is not in the code)."""
    if code.startswith(("688", "689", "30")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def default_index(listing_id: str, profile: str) -> str:
    """The index the 大盘副图 shows for a stock (spec §8.6): the board's own index."""
    market, code = parse_id(listing_id)
    if market == "JP":  # one board-wide index: the market's benchmark for the data profile
        return load_market("JP").benchmark[profile]
    if code.startswith("688"):
        return "CN:IDX:000688"
    if code.startswith(("60", "90")):
        return "CN:IDX:000001"
    if code.startswith("30"):
        return "CN:IDX:399006"
    if code.startswith(("00", "20")):
        return "CN:IDX:399001"
    return "CN:IDX:899050"
