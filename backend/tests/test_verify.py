from datetime import date

import httpx
import polars as pl

from tests.test_pipeline import DAYS, FakeJP, pipe
from wb.data.http import HttpClient
from wb.data.spotcheck import spotcheck
from wb.data.verify import compare, crosscheck, references, sse_szse_reference, tickflow_reference, yfinance_reference


def test_sse_szse_reference_units_and_columns():
    def handler(request):
        if "sse.com.cn" in request.url.host:
            return httpx.Response(200, json={"kline": [[20260930, 1239.53, 1268.0, 1236.05, 1258.62, 3833098, 4797246636]]})
        return httpx.Response(200, json={"data": {"picupdata": [
            ["2026-09-30", "290.00", "291.11", "285.80", "292.70", "4.31", "1.50", 296995, 8613929784.0]]}})

    fetch = sse_szse_reference(HttpClient(1000, transport=httpx.MockTransport(handler)))
    df = fetch(["CN:600519", "CN:300750"], date(2026, 9, 30), date(2026, 9, 30)).sort("listing_id")
    catl = df.row(0, named=True)
    assert catl["listing_id"] == "CN:300750"
    assert (catl["open"], catl["high"], catl["low"], catl["close"]) == (290.0, 292.7, 285.8, 291.11)
    assert catl["volume"] == 29_699_500  # SZSE reports lots
    assert df.row(1, named=True)["volume"] == 3_833_098  # SSE reports shares


def test_the_japanese_witness_has_no_turnover_and_is_compared_on_what_it_has():
    # Yahoo's daily bars carry no 成交额: the crosscheck of the JP market fell over on it (2026-10-10, real run)
    class Yahoo:
        def fetch(self, dataset, symbol, start, end):
            return {"bars_1d": pl.DataFrame({"date": [date(2026, 10, 8)], "open": [3000.0], "high": [3050.0], "low": [2990.0],
                                             "close": [3020.0], "volume": [1_000_000.0], "listing_id": ["JP:7203"],
                                             "suspended": [False], "source": ["yfinance"]})}

    ref = yfinance_reference(Yahoo())(["JP:7203"], date(2026, 10, 8), date(2026, 10, 8))
    assert ref["amount"].to_list() == [None] and ref["close"].to_list() == [3020.0]
    stored = ref.with_columns(pl.lit(3.02e9).alias("amount"))
    assert sorted(compare(stored, ref)["field"].to_list()) == ["close", "high", "low", "open", "volume"]


def test_compare_tolerances():
    s = pl.DataFrame({"listing_id": ["CN:1"] * 2, "date": [date(2026, 9, 30)] * 2, "open": [10.0, 10.0],
                      "high": [10.0, 10.0], "low": [10.0, 10.0], "close": [10.0, 10.01], "volume": [1000.0, 1000.0],
                      "amount": [10000.0, 10000.0]}).with_columns(pl.Series("date", [date(2026, 9, 29), date(2026, 9, 30)]))
    r = s.with_columns(pl.Series("close", [10.0, 10.0]), pl.Series("amount", [10040.0, 10000.0]))
    res = compare(s, r)
    bad = res.filter(~pl.col("ok"))
    assert bad.select("date", "field").rows() == [(date(2026, 9, 30), "close")]  # 0.01 off; amount 0.4% is fine


def test_crosscheck_stores_results(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])

    def reference(ids, start, end):
        b = p.store.read("bars_1d").filter(pl.col("listing_id").is_in(ids) & pl.col("date").is_between(start, end))
        return b.with_columns(pl.when(pl.col("listing_id") == "JP:7203").then(pl.col("close") + 1).otherwise(pl.col("close")).alias("close"))

    res = crosscheck(p, n=3, days=2, refs={"fake": reference})
    assert set(res.filter(~pl.col("ok"))["listing_id"]) == {"JP:7203"}
    n = p.store.conn.execute("SELECT COUNT(*) FROM crosscheck WHERE ok=0").fetchone()[0]
    assert n == 2  # close on two days


def test_spotcheck_matches_and_detects_difference(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])
    res = spotcheck(pipe(paths, FakeJP()), n=3, day=DAYS[1])
    assert res.height > 0 and res["ok"].all()
    res = spotcheck(pipe(paths, FakeJP(closes={("JP:7203", DAYS[1]): 1001.0})), n=3, day=DAYS[1])
    bad = res.filter(~pl.col("ok"))
    assert set(bad["field"]) == {"open", "high", "low", "close"} and bad["listing_id"].unique().to_list() == ["JP:7203"]


def test_daily_checks_never_load_the_whole_bars_table(paths, monkeypatch):
    # 10 years × 6,000 listings is ~1.5 GB in memory: spotcheck, crosscheck and refetch read one day
    from wb.data.store import Store

    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])
    real = Store.read

    def guarded(self, dataset, snap=None):
        assert dataset != "bars_1d", "read the whole bars table"
        return real(self, dataset, snap)

    monkeypatch.setattr(Store, "read", guarded)
    assert spotcheck(pipe(paths, FakeJP()), n=3).height > 0
    def same(ids, start, end):  # a reference that agrees, read the cheap way
        return p.store.scan("bars_1d").filter(pl.col("listing_id").is_in(ids) & pl.col("date").is_between(start, end)).collect()

    assert crosscheck(pipe(paths, FakeJP()), n=3, days=2, refs={"same": same})["ok"].all()
    assert pipe(paths, FakeJP()).refetch(DAYS[1]) is None


def test_crosscheck_stays_inside_the_reference_window_and_survives_refusals(paths):
    # J-Quants free lags 12 weeks: compare older days it has; a refusal is reported, not a crash
    from wb.data.providers.base import Unavailable

    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])

    def lagging(ids, start, end):
        assert end <= DAYS[1]
        return p.store.scan("bars_1d").filter(pl.col("listing_id").is_in(ids) & pl.col("date").is_between(start, end)).collect()

    lagging.latest = DAYS[1]

    def refusing(ids, start, end):
        raise type("Denied", (Unavailable,), {})("outside the subscription window")

    res = crosscheck(pipe(paths, FakeJP()), n=3, days=2, refs={"lagging": lagging, "refusing": refusing})
    assert res["ok"].all() and set(res["date"]) <= {DAYS[0], DAYS[1]} and set(res["reference"]) == {"lagging"}


def test_tickflow_is_a_witness_for_every_board_the_beijing_exchange_too(monkeypatch):
    # the exchanges' websites cover Shanghai and Shenzhen only; TickFlow's daily bars agreed with Tushare's on every
    # price of 25,228 Beijing exchange stock-days (2026-10-11): with its key set, the prod profile's second witness
    class Days:
        def days(self, ids, start, end):
            return pl.DataFrame({"listing_id": ids, "date": [start] * len(ids), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                 "volume": 100.0, "amount": 100.0})
    fetch = tickflow_reference(Days())
    pool = pl.DataFrame({"listing_id": [f"CN:{c}" for c in ("600000", "600001", "688001", "000001", "002001", "300001",
                                                                "300002", "920001", "920002", "920003")]})
    picked = fetch.pick(pool, 5, seed=0)
    boards = {x[3:5] for x in picked}
    assert len(picked) == 5 and boards == {"60", "68", "00", "30", "92"}       # one from each board, not a random five
    assert fetch(["CN:920001"], DAYS[0], DAYS[0])["listing_id"].to_list() == ["CN:920001"]
    monkeypatch.setattr("wb.data.verify.secret_source", lambda name: "keychain" if name == "tickflow_api_key" else None)
    assert sorted(references("prod", "CN")) == ["sse_szse", "tickflow"]
    assert sorted(references("test", "CN")) == ["sse_szse"]                    # the test profile's data are not TickFlow's to judge
    monkeypatch.setattr("wb.data.verify.secret_source", lambda name: None)
    assert sorted(references("prod", "CN")) == ["sse_szse"]


def test_a_witness_may_choose_its_own_sample(paths):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])
    asked = []

    def ref(ids, start, end):
        asked.append(ids)
        return pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, **{f: pl.Float64 for f in ("open", "high", "low", "close", "volume", "amount")}})
    ref.pick = lambda pool, n, seed: pool["listing_id"].sort().to_list()[:1]
    crosscheck(p, n=30, days=1, refs={"mine": ref})
    assert len(asked) == 1 and len(asked[0]) == 1
