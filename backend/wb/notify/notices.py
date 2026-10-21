"""How a notice names a stock (spec §7.1). The scans' own notices became the 开盘前简报 (wb.notify.brief, 1.34)."""

MARKET_NAME = {"CN": "A 股", "JP": "日股"}


def listing_title(listing_id: str, names: dict[str, str]) -> str:
    """「name code · market」: how a notice names a stock."""
    market, code = listing_id.split(":", 1)
    name = names.get(listing_id)
    return f"{name + ' ' if name else ''}{code} · {MARKET_NAME.get(market, market)}"
