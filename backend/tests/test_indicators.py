"""指标库 (design 2026-10-10): the built-in indicators, yours kept as versions, an indicator over a chart's own bars (its
lines meeting the chart's own numbers, the zones and levels carried to where the bars end them) and the API."""
from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from tests.test_chart_series import cn_daily
from tests.test_l1_script import THS, walk
from wb.api.app import create_app
from wb.charts.series import Series, build, to_epoch_ms
from wb.config import get_paths
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.run import evaluate
from wb.engine.l1.script import compile_script
from wb.indicators import GROUPS, IndicatorError, IndicatorExists, Indicators, builtin_indicators, formula_problems, load_indicator
from wb.indicators.compute import compute

H = {"X-WB-Token": "t"}
MINE = """name: 我的均线
pane: auto
params:
  - {id: N, value: 5, range: [1, 250], step: 1}
formula: |
  M:MA(CLOSE,N),COLORRED;
"""


def test_every_built_in_indicator_reads_compiles_in_its_markets_and_draws_on_bars():
    df = walk(400, seed=5)
    df = df.with_columns(pre=df["close"].shift(1), vwap=df["close"], turnover_rate=pl.lit(1.0))
    seen = set()
    for iid, (ind, text) in builtin_indicators().items():
        assert iid not in seen and ind.group in GROUPS and ind.group != "mine" and ind.doc, iid
        seen.add(iid)
        load_indicator(text)                          # the full check: the formula compiles in each of its markets
        assert formula_problems(ind) == [], iid
        c = compile_script(ind.formula, ind.markets[0], ind.param_values())
        out = evaluate(df, c.program)
        assert out.height == 400 and all(out[col].drop_nulls().len() > 0 for it in c.items for col in it.cols.values()), iid
    assert {i.group for i, _ in builtin_indicators().values()} == {"common", "setup", "ict"} and len(seen) >= 40


def test_yours_are_kept_as_versions_put_away_and_never_over_a_built_in(tmp_path):
    store = Indicators(get_paths("test", tmp_path))
    iid, v, created = store.save(MINE)
    assert iid.startswith("u") and (v, created) == (1, True)
    text = store.get(iid)[1]
    assert store.save(text) == (iid, 1, False)                                # the same again: no new version
    assert store.save(text.replace("value: 5", "value: 10")) == (iid, 2, True)
    ind, _, v = store.get(iid)
    assert (v, ind.params[0].value, ind.group) == (2, 10, "mine")
    store.set_state(iid, archived=True)
    mine = [r for r in store.catalog() if not r["builtin"]]
    assert [(r["id"], r["version"], r["archived"]) for r in mine] == [(iid, 2, True)]
    with pytest.raises(IndicatorError, match="内置指标"):
        store.save("id: rsi\nname: x\nformula: 'A:C;'")
    with pytest.raises(IndicatorExists):
        store.save(text, create=True)
    with pytest.raises(IndicatorError, match="Level-2"):
        store.save(f"name: 同花顺\nformula: |\n" + "\n".join(f"  {x}" for x in THS.splitlines()))


def _series(df: pl.DataFrame) -> Series:
    return Series("CN:600519", "day", "qfq", True, df.drop("listing_id").with_columns(scale=pl.lit(1.0), pre_close=pl.lit(None, pl.Float64),
                                                                                       vwap=pl.col("close"), turnover_rate=pl.lit(None, pl.Float64)))


def test_a_zone_is_drawn_from_its_first_bar_until_the_price_goes_through_it_and_a_level_until_it_is_taken():
    from tests.test_l1_compiler import frame
    hi = [10.0, 10.2, 10.6, 11.0, 11.2, 11.1, 10.9, 10.4, 10.5, 10.6]
    lo = [9.6, 9.9, 9.95, 10.5, 10.8, 10.7, 10.6, 10.0, 10.1, 10.2]  # bar 3's low 10.5 over bar 1's high 10.2: a gap 10.2–10.5
    df = frame(close=[(h + low) / 2 for h, low in zip(hi, lo, strict=True)], high=hi, low=lo)
    ind = load_indicator("name: 缺口\nformula: |\n  DRAWZONE(LOW>REF(HIGH,2),LOW,REF(HIGH,2),1,2),COLORUP;\n"
                         "  DRAWLEVEL(HIGH=HHV(HIGH,0),HIGH,1,0);\n")
    res = compute(ind, _series(df), "CN:600519", "CN")
    t = [to_epoch_ms(df["date"][i]) for i in range(10)]
    first = res["zones"][0]
    assert (first["s"], first["at"], first["top"], first["bottom"]) == (t[1], t[3], 10.5, 10.2)
    assert first["e"] == t[7]                                                   # bar 7's low 10.0 goes through 10.2
    assert res["pane"] == "main" and res["items"][0]["color"] == "up"
    lv = [x for x in res["levels"] if x["at"] == t[4]][0]                       # bar 4's 11.2: the highest so far
    assert lv["price"] == 11.2 and lv["e"] is None                              # never reached again: it stands


def test_an_indicator_meets_the_charts_own_numbers_and_a_check_says_what_is_wrong(tmp_path):
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    lid = Store(paths, "CN").read("instruments")["listing_id"][0]
    c = TestClient(create_app(tmp_path, token="t"))
    cat = c.get("/api/indicators", headers=H).json()
    assert {r["id"] for r in cat["indicators"]} >= {"ma_group", "rsi", "ict_fvg", "setup_momentum"}
    bars = c.get(f"/api/bars?listing_id={lid}&limit=5000", headers=H).json()["rows"]
    res = c.get(f"/api/indicator-series?listing_id={lid}&id=ma_group", headers=H).json()
    assert res["pane"] == "main" and res["t"] == [b["t"] for b in bars]
    col = next(it["cols"]["value"] for it in res["items"] if it["name"] == "MA1")
    assert all((a is None and b["ma5"] is None) or abs(a - b["ma5"]) < 1e-9 for a, b in zip(res["values"][col], bars, strict=True))
    weekly = c.get(f"/api/indicator-series?listing_id={lid}&id=turnover&period=week", headers=H).json()
    assert "error" in weekly                                                    # 换手率 is a daily field

    ok = c.post("/api/indicators/check", json={"text": MINE, "listing_id": lid}, headers=H).json()
    assert ok["ok"] and ok["pane"] == "main" and [i["name"] for i in ok["items"]] == ["M"]
    assert len(ok["sample"]["rows"]) == 5 and abs(ok["sample"]["rows"][-1]["M"] - bars[-1]["ma5"]) < 1e-9
    ths = c.post("/api/indicators/check", json={"text": "name: 同花顺\nformula: |\n" + "\n".join(f"  {x}" for x in THS.splitlines())},
                 headers=H).json()
    assert not ths["ok"] and any("Level-2" in u["why"] for u in ths["unsupported"])
    bad = c.post("/api/indicators/check", json={"text": "name: x\nformula: 'A:MA(C,5)+FOO;'"}, headers=H).json()
    assert bad["errors"][0]["line"] == 1 and "FOO" in bad["errors"][0]["message"]

    saved = c.post("/api/indicators", json={"text": MINE}, headers=H).json()
    assert saved["created"] and c.get(f"/api/indicators/{saved['id']}", headers=H).json()["indicator"]["name"] == "我的均线"
    assert c.post("/api/indicators", json={"text": "name: x\nformula: 'A:FOO;'"}, headers=H).status_code == 422
    assert c.put(f"/api/indicators/{saved['id']}/state", json={"archived": True}, headers=H).json() == {"ok": True}
    assert c.get("/api/indicators/no_such", headers=H).status_code == 422


# review 10-13 (indicators)
def test_turnover_on_a_weekly_chart_says_it_is_a_day_s_field():
    """It said 第 1 个字符：TURNOVER（换手率（%））在当前数据档里没有 — twice, about a formula the user never wrote."""
    d = cn_daily().with_columns(pl.lit(1.5).alias("turnover_rate"))
    ind = builtin_indicators()["turnover"][0]
    for period in ("week", "month"):
        s = build(d, "CN:600519", "CN", period, "qfq")
        res = compute(ind, s, "CN:600519", "CN", None, set(), None)
        assert "只有日线有" in res["error"] and "数据档" not in res["error"] and "字符" not in res["error"], res
    assert "error" not in compute(ind, build(d, "CN:600519", "CN", "day", "qfq"), "CN:600519", "CN", None, set(), None)
