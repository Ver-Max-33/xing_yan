"""Shadow verification (spec §4.6): compare a sample of stored raw bars with an independent source.

The reference is only a witness. It never fills gaps or replaces values (spec §4.2: no silent
source switching); disagreements are recorded and reported for a human to judge.

References, verified 2026-10-02:
  CN  SSE  https://yunhq.sse.com.cn:32042/v1/sh1/dayk/<code>   full history; volume in shares
      SZSE https://www.szse.cn/api/market/ssjjhq/getHistoryData  last ~200 days; volume in lots
      (official exchange website data, unadjusted; BSE has no usable endpoint)
      TickFlow daily K-lines (prod profile, once its key is set): every board, the Beijing exchange too; as traded,
      volume in lots. Verified 2026-10-11 against the Tushare daily bars: 25,228 BSE stock-days agree on every price,
      volume and amount on 99.45% (ledger 2026-10-11-tickflow-audit)
  JP  yfinance (raw reconstructed, for the prod profile) or J-Quants (for the test profile,
      dates older than the free plan's 12-week delay)
"""
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from wb.data.http import HttpClient
from wb.data.pipeline import Pipeline
from wb.data.providers.base import Unavailable
from wb.data.store import Snapshot
from wb.db import tx
from wb.listing import parse_id, to_jquants, to_yahoo
from wb.secrets import secret_source

FIELDS = ["open", "high", "low", "close", "volume", "amount"]
SCHEMA = {"listing_id": pl.Utf8, "date": pl.Date, **{f: pl.Float64 for f in FIELDS}}
ReferenceFn = Callable[[list[str], date, date], pl.DataFrame]


def sse_szse_reference(http: HttpClient | None = None) -> ReferenceFn:
    http = http or HttpClient(per_minute=30)

    def fetch(listing_ids: list[str], start: date, end: date) -> pl.DataFrame:
        rows = []
        for lid in listing_ids:
            code = parse_id(lid)[1]
            if code.startswith(("6", "9")):  # Shanghai
                n = (date.today() - start).days + 10  # calendar days >= trading days: enough bars
                r = http.request("GET", f"https://yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}",
                                 params={"begin": -n, "end": -1, "select": "date,open,high,low,close,volume,amount"},
                                 headers={"Referer": "https://www.sse.com.cn/"})
                if r.status_code == 200:
                    for d, o, h, lo, c, v, a in r.json().get("kline", []):
                        rows.append((lid, datetime.strptime(str(d), "%Y%m%d").date(), o, h, lo, c, float(v), float(a)))
            elif code.startswith(("0", "2", "3")):  # Shenzhen
                r = http.request("GET", "https://www.szse.cn/api/market/ssjjhq/getHistoryData",
                                 params={"cycleType": 32, "marketId": 1, "code": code},
                                 headers={"Referer": "https://www.szse.cn/"})
                if r.status_code == 200:
                    for d, o, c, lo, h, *_, v, a in r.json().get("data", {}).get("picupdata", []):
                        rows.append((lid, date.fromisoformat(d), float(o), float(h), float(lo), float(c),
                                     float(v) * 100, float(a)))  # lots -> shares
        df = pl.DataFrame(rows, schema=SCHEMA, orient="row")
        return df.filter(pl.col("date").is_between(start, end))

    return fetch


def yfinance_reference(provider=None) -> ReferenceFn:
    from wb.data.providers.yfinance_test import YFinance

    yf = provider or YFinance("JP")

    def fetch(listing_ids: list[str], start: date, end: date) -> pl.DataFrame:
        frames = [yf.fetch("bars_1d", to_yahoo(lid), start, end)["bars_1d"] for lid in listing_ids]
        # Yahoo gives no 成交额: it is left unknown, and compare() leaves out a field the witness does not have
        return pl.concat([f.with_columns(pl.lit(None, t).alias(c) for c, t in SCHEMA.items() if c not in f.columns)
                          .select(pl.col(c).cast(t) for c, t in SCHEMA.items()) for f in frames]) if frames else pl.DataFrame(schema=SCHEMA)

    return fetch


def jquants_reference(provider=None) -> ReferenceFn:
    from wb.data.providers.jquants import JQuants

    jq = provider or JQuants()

    def fetch(listing_ids: list[str], start: date, end: date) -> pl.DataFrame:
        rows = []
        for lid in listing_ids:
            for r in jq.get("/equities/bars/daily", code=to_jquants(lid), **{"from": start.isoformat(), "to": end.isoformat()}):
                rows.append((lid, date.fromisoformat(r["Date"]), r.get("O"), r.get("H"), r.get("L"), r.get("C"),
                             r.get("Vo"), r.get("Va")))
        return pl.DataFrame(rows, schema=SCHEMA, orient="row")

    # the free plan lags 12 weeks (spec §4.6 测试档用 J-Quants 免费档核对 12 周以前的数据); with a paid
    # plan the older days are still a fair sample
    fetch.latest = date.today() - timedelta(weeks=12)
    return fetch


def tickflow_reference(source=None) -> ReferenceFn:
    """TickFlow's daily bars as a witness of every board — a sample with as many listings of each (上海主板, 深圳主板,
    创业板, 科创板, 北交所), not a random one, so the Beijing exchange is always in it."""
    if source is None:
        from wb.realtime.providers.tickflow_rt import TickFlowRealtime
        source = TickFlowRealtime()

    def fetch(listing_ids: list[str], start: date, end: date) -> pl.DataFrame:
        return source.days(listing_ids, start, end).select(list(SCHEMA))

    def pick(pool: pl.DataFrame, n: int, seed: int) -> list[str]:
        ids = pool["listing_id"].unique().sort()
        boards = ids.str.slice(3, 2).replace_strict({"00": "00", "60": "60", "68": "68", "30": "30", "92": "92"}, default="other")
        per = max(1, n // 5)
        return pl.DataFrame({"listing_id": ids, "board": boards}).filter(pl.col("board") != "other").group_by("board").map_groups(
            lambda g: g.sample(n=min(per, g.height), seed=seed))["listing_id"].to_list()

    fetch.pick = pick
    return fetch


def references(profile: str, market: str) -> dict[str, ReferenceFn]:
    if market == "CN":
        refs = {"sse_szse": sse_szse_reference()}
        if profile == "prod" and secret_source("tickflow_api_key"):
            refs["tickflow"] = tickflow_reference()
        return refs
    return {"jquants": jquants_reference()} if profile == "test" else {"yfinance": yfinance_reference()}


def compare(stored: pl.DataFrame, ref: pl.DataFrame) -> pl.DataFrame:
    """One row per (listing, date, field) present in both. Prices must match to 0.005 (or 1e-6
    relative), volume to 0.1%, amount to 0.5% (sources round turnover differently)."""
    j = stored.select(list(SCHEMA)).join(ref.select(list(SCHEMA)), on=["listing_id", "date"], suffix="_ref")
    out = []
    for f in FIELDS:
        tol = {"volume": 1e-3, "amount": 5e-3}.get(f)
        a, b = pl.col(f), pl.col(f"{f}_ref")
        ok = (((a - b).abs() <= b.abs() * tol) if tol else
              (((a - b).abs() <= 0.005) | ((a - b).abs() <= b.abs() * 1e-6)))
        out.append(j.filter(a.is_not_null() & b.is_not_null()).select(
            "listing_id", "date", pl.lit(f).alias("field"), a.alias("stored"), b.alias("ref_value"), ok.alias("ok")))
    return pl.concat(out)


def crosscheck(pipe: Pipeline, n: int = 30, days: int = 5, seed: int = 0,
               refs: dict[str, ReferenceFn] | None = None) -> pl.DataFrame:
    """Sample `n` listings traded on the last `days` published trading days, compare with every
    reference for this market, store results with the latest snapshot and return them."""
    snap: Snapshot | None = pipe.store.latest()
    if snap is None:
        return pl.DataFrame()
    bars = pipe.store.scan("bars_1d")  # a few days, never the whole table
    published = sorted(bars.select(pl.col("date").unique()).collect()["date"].to_list())
    results = []
    for name, fetch in (refs or references(pipe.paths.profile, pipe.market)).items():
        # a reference that lags (J-Quants free: 12 weeks) is compared on the last days it has (spec §4.6)
        latest = getattr(fetch, "latest", None)
        recent = [d for d in published if latest is None or d <= latest][-days:]
        if not recent:
            pipe.log(f"crosscheck: {name} has no data for the published days")
            continue
        pool = bars.filter(pl.col("date").is_in(recent) & ~pl.col("suspended").fill_null(False)).collect()
        pick = getattr(fetch, "pick", None)   # a witness may choose its sample (TickFlow: every board)
        ids = (pl.DataFrame({"listing_id": pick(pool.select("listing_id").unique(), n, seed)}, schema={"listing_id": pl.Utf8}) if pick
               else pool.select("listing_id").unique().sample(n=min(n, pool["listing_id"].n_unique()), seed=seed))
        sample = pool.join(ids, on="listing_id")
        try:
            ref = fetch(ids["listing_id"].to_list(), recent[0], recent[-1])
        except Unavailable as e:  # reported, never fatal: the data were already published
            pipe.log(f"crosscheck: {name} refused ({e})")
            continue
        got = compare(sample, ref)
        missing = ids.join(got.select("listing_id").unique(), on="listing_id", how="anti")["listing_id"].to_list()
        if missing:
            pipe.log(f"crosscheck: {name} has no data for {len(missing)} listings (e.g. Beijing exchange): {missing[:10]}")
        results.append(got.with_columns(pl.lit(name).alias("reference")))
    res = pl.concat(results) if results else pl.DataFrame()
    if res.height:
        now = datetime.now().isoformat(timespec="seconds")
        with tx(pipe.store.conn):
            pipe.store.conn.executemany(
                "INSERT INTO crosscheck VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(snap.id, r["reference"], r["listing_id"], r["date"].isoformat(), r["field"], r["stored"],
                  r["ref_value"], int(r["ok"]), now) for r in res.iter_rows(named=True)])
    return res
