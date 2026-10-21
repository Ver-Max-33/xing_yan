"""Every registered data source is checked the same way (spec §3.5: tests follow the registry): it
declares only catalog datasets and known unit kinds, a ticker source can list its tickers, and a
per-day market source can count a day's rows for readiness (spec §4.5). A new provider fails here
until it is added to FACTORIES, so it cannot join without these checks. Fetch output and probing
are covered per provider against fake servers (test_tushare, test_jquants, test_test_providers)."""
from datetime import date

import pandas as pd
import pytest

from wb.data.http import RateLimiter
from wb.data.providers.base import Provider
from wb.datasets import DATASETS
from wb.registry import PROVIDERS, autoload

autoload("wb.data.providers")

FACTORIES = {  # name -> builds the provider for a market without touching the network
    "tushare": lambda m: PROVIDERS["tushare"](token="t"),
    "jquants": lambda m: PROVIDERS["jquants"](api_key="k"),
    "jpx_list": lambda m: PROVIDERS["jpx_list"](),
    "jpx_topix": lambda m: PROVIDERS["jpx_topix"](),
    "exchange_list_cn": lambda m: PROVIDERS["exchange_list_cn"](),
    "xcals": lambda m: PROVIDERS["xcals"](m),
    "yfinance": lambda m: PROVIDERS["yfinance"](m, history=lambda *a: pd.DataFrame(), limiter=RateLimiter(10_000),
                                                today=lambda: date(2026, 10, 2)),
}
MARKETS_OF = {"tushare": ["CN"], "jquants": ["JP"], "jpx_list": ["JP"], "jpx_topix": ["JP"], "exchange_list_cn": ["CN"],
              "xcals": ["CN", "JP"], "yfinance": ["CN", "JP"]}


def test_every_registered_provider_is_covered():
    assert set(PROVIDERS) == set(FACTORIES) == set(MARKETS_OF)


@pytest.mark.parametrize("name, market", [(n, m) for n in sorted(PROVIDERS) for m in MARKETS_OF.get(n, [])])
def test_provider_declares_what_it_can_serve(name, market):
    p = FACTORIES[name](market)
    assert p.unit_kinds and set(p.unit_kinds) <= set(DATASETS), p.unit_kinds
    assert set(p.unit_kinds.values()) <= {"date", "all", "ticker", "period"}
    if "ticker" in p.unit_kinds.values():
        assert type(p).tickers is not Provider.tickers, f"{name} has ticker units but cannot list tickers"
    if p.unit_kinds.get("bars_1d") == "date":
        assert type(p).ready_count is not Provider.ready_count, f"{name} serves days but cannot count readiness"
