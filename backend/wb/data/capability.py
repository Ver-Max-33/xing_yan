"""Capability table and routing (spec §3.5 mechanism 3, §4.9).

Business code asks "who provides JP bars_1d?"; the answer comes from the profile's route list
filtered by what probing found available. Prod has exactly one source per dataset: when Tushare
fails we report it and retry, we never silently switch sources (spec §4.2).
"""
from datetime import date, datetime

from wb.data.providers.base import Probe, Provider
from wb.db import tx
from wb.registry import PROVIDERS, autoload

ROUTES: dict[str, dict[str, dict[str, list[str]]]] = {
    "test": {
        "CN": {"calendar": ["xcals"], "instruments": ["exchange_list_cn"], "bars_1d": ["tushare"],
               "corp_actions": ["tushare"], "valuation": ["tushare"], "status_intervals": ["tushare"],
               "industry": ["tushare"], "index_bars_1d": ["tushare", "yfinance"], "index_members": ["tushare"],
               "financials": ["tushare"], "moneyflow": ["tushare"], "unlocks": ["tushare"], "float_holders": ["tushare"]},
        "JP": {"calendar": ["xcals"], "instruments": ["jpx_list"], "bars_1d": ["yfinance"],
               "index_bars_1d": ["yfinance"], "index_members": ["jpx_topix"]},
    },
    "prod": {
        "CN": {ds: ["tushare"] for ds in ("calendar", "instruments", "status_intervals", "industry",
                                          "corp_actions", "bars_1d", "valuation", "index_bars_1d", "index_members", "financials",
                                          "moneyflow", "unlocks", "float_holders")},
        "JP": {**{ds: ["jquants"] for ds in ("calendar", "instruments", "bars_1d", "valuation", "index_bars_1d", "financials")},
               "index_members": ["jpx_topix"]},
    },
}


def save_probes(conn, provider: str, market: str, probes: list[Probe]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with tx(conn):
        for p in probes:
            conn.execute(
                "INSERT OR REPLACE INTO capability VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, market, p.dataset, int(p.available),
                 p.history_start.isoformat() if p.history_start else None,
                 p.latest_date.isoformat() if p.latest_date else None, p.rate_per_min, p.detail, now))


def load_caps(conn, market: str) -> dict[tuple[str, str], Probe]:
    rows = conn.execute("SELECT * FROM capability WHERE market=?", (market,))
    return {(r["provider"], r["dataset"]): Probe(
        r["dataset"], bool(r["available"]),
        date.fromisoformat(r["history_start"]) if r["history_start"] else None,
        date.fromisoformat(r["latest_date"]) if r["latest_date"] else None,
        r["rate_per_min"], r["detail"]) for r in rows}


def record_error(conn, market: str, provider: str, dataset: str, unit: str, error: str) -> None:
    """A fetch that failed (spec §4.9 最近一次错误): the last one per source and dataset is kept."""
    conn.execute("INSERT OR REPLACE INTO fetch_error VALUES (?, ?, ?, ?, ?, ?)",
                 (market, provider, dataset, unit, error[:500], datetime.now().isoformat(timespec="seconds")))


def last_errors(conn, market: str) -> dict[tuple[str, str], dict]:
    return {(r["provider"], r["dataset"]): {"unit": r["unit"], "error": r["error"], "at": r["at"]}
            for r in conn.execute("SELECT * FROM fetch_error WHERE market=?", (market,))}


def route(profile: str, market: str, dataset: str, caps: dict[tuple[str, str], Probe]) -> str | None:
    """First provider in the route list that is not known to be unavailable. Never-probed counts as
    worth trying; a failure at fetch time then marks it unavailable for that run."""
    for name in ROUTES[profile][market].get(dataset, []):
        cap = caps.get((name, dataset))
        if cap is None or cap.available:
            return name
    return None


def make_provider(name: str, market: str, caps: dict[tuple[str, str], Probe] | None = None) -> Provider:
    if name not in PROVIDERS:
        autoload("wb.data.providers")  # imports are cached, so this is cheap after the first call
    prov = PROVIDERS[name](market=market)
    rates = [c.rate_per_min for (n, _), c in (caps or {}).items() if n == name and c.rate_per_min]
    limiter = getattr(getattr(prov, "http", None), "limiter", None) or getattr(prov, "limiter", None)
    if rates and limiter is not None:
        limiter.set_rate(min(rates))
    return prov


def probe_market(conn, profile: str, market: str) -> dict[str, list[Probe]]:
    """Probe every provider routed for this market and store the results."""
    names = sorted({n for lst in ROUTES[profile][market].values() for n in lst})
    out = {}
    for name in names:
        try:
            probes = make_provider(name, market).probe()
        except Exception as e:  # noqa: BLE001 - the source could not be asked (a network blip, say): not "the plan lacks it"
            # — what was known stays (never-probed is still tried) and the failure is its last error; only the source's
            # own answer marks a dataset unavailable
            datasets = [ds for ds, lst in ROUTES[profile][market].items() if name in lst]
            for ds in datasets:
                record_error(conn, market, name, ds, "probe", f"probe failed: {type(e).__name__}: {e}")
            out[name] = [Probe(ds, False, detail=f"probe failed: {e}") for ds in datasets]   # said, not stored
            continue
        save_probes(conn, name, market, probes)
        out[name] = probes
    return out


def probe_if_stale(paths, market: str, log=print) -> None:
    """Spec §4.9: probe on first run and on the first update of each day, so a plan upgrade is
    picked up without code changes."""
    from wb.db import connect
    conn = connect(paths.app_db, "app")
    last = conn.execute("SELECT MAX(probed_at) FROM capability WHERE market=?", (market,)).fetchone()[0]
    if last is None or last[:10] != date.today().isoformat():
        log(f"probing {market} data sources ...")
        probe_market(conn, paths.profile, market)
