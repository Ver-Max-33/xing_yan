"""Shared helpers for the 价格行为分析 tests (design 2026-10-20): synthetic PA frames, stage-1 shapes, a store with daily
bars, plans and the model's canned answers. One-off helpers a single test file needs stay in that file."""
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import numpy as np
import polars as pl
from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import build_analysis_frame

DATA = Path(__file__).parent / "data" / "pa"
STAGE1_BULLISH = {"cycle_position": "normal_channel", "direction": "bullish", "detected_patterns": []}
STAGE1_BEARISH_CHANNEL = {"cycle_position": "normal_channel", "direction": "bearish", "detected_patterns": []}
STAGE1_BEARISH_SPIKE = {"cycle_position": "spike", "direction": "bearish", "spike_stage": "active", "detected_patterns": []}


def synthetic_frame(n: int = 150, *, timeframe: str = "1d", trend: float = 0.002, **fields) -> KlineFrame:
    """`n` closed bars rising by `trend` a bar, newest first, prices on the 0.01 grid, through PA's own snapshot builder
    (100 bars kept, the rest warm EMA20 / ATR14 up); `fields` replace KlineFrame fields (limit marks, previous plan…)."""
    step = timedelta(days=1) if timeframe == "1d" else timedelta(hours=1)
    newest = datetime(2026, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(n):   # i = 0 is the newest bar
        close = round(10.0 * (1 - trend * i), 2)
        ts = int((newest - step * i).timestamp() * 1000)
        bars.append(KlineBar(seq=i + 1, ts_open=ts, open=round(close * 0.995, 2), high=round(close * 1.01, 2),
                             low=round(close * 0.985, 2), close=close, volume=1e6, closed=True))
    frame = build_analysis_frame(bars, 100, "600000", timeframe)
    assert frame is not None
    return dataclasses.replace(frame, **fields)


def _days(n: int, last: date = date(2026, 9, 30)) -> list[date]:
    """`n` weekdays ending on `last`, oldest first."""
    ds, d = [], last
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d)
        d -= timedelta(days=1)
    return ds[::-1]


def _cents(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _walk(rng, lid: str, days: list[date], start: float, *, ex_at: int | None = None, factor: float = 1.0,
          limits: dict[int, str] | None = None) -> list[dict]:
    """A stock's raw daily bars on the 0.01 grid, with the exchange's pre_close and ±10% limits: ex-rights at `ex_at`
    divides the price by `factor` (so close[t-1] / pre_close[t] is the factor's step); `limits` maps an index to
    "涨停" (closes at the limit, traded below it) or "一字" (opens, trades and closes at it)."""
    rows, prev = [], start
    for i, d in enumerate(days):
        pre = _cents(prev / factor) if i == ex_at else prev
        up, down = _cents(pre * 1.1), _cents(pre * 0.9)
        kind = (limits or {}).get(i)
        if kind == "一字":
            o = h = lo = c = up
        elif kind == "涨停":
            o, h, lo, c = _cents(pre * 1.02), up, _cents(pre * 1.01), up
        else:
            c = _cents(min(max(pre * (1 + rng.normal(0.0005, 0.015)), down), up))
            o = _cents(min(max(pre * (1 + rng.normal(0, 0.005)), down), up))
            h = _cents(min(max(o, c) * (1 + abs(rng.normal(0, 0.006))), up))
            lo = _cents(max(min(o, c) * (1 - abs(rng.normal(0, 0.006))), down))
        rows.append({"listing_id": lid, "date": d, "open": o, "high": h, "low": lo, "close": c, "pre_close": pre,
                     "volume": float(int(rng.uniform(0.8, 1.2) * 1e4) * 100),
                     "adj_factor": factor if ex_at is not None and i >= ex_at else 1.0,
                     "up_limit": up, "down_limit": down, "suspended": False, "source": "t"})
        prev = c
    return rows


def pa_market(paths, *, bars: int = 160, ex_at: int = 120, factor: float = 1.25, codes: tuple[str, ...] = ("600000",),
              extra_listings: int = 0) -> None:
    """A CN store for the PA tests: each of `codes` has `bars` days with an ex-rights date at `ex_at`, a limit-up close
    on its 5th-newest bar and a 一字涨停 on its 3rd-newest. `extra_listings` plain random walks make the control pool:
    the first is ST (the last 60 days), the second the 北交所's 830001, the third listed 30 trading days before the end."""
    from wb.data.store import Store
    from wb.datasets import conform
    rng = np.random.default_rng(7)
    days = _days(bars)
    rows, inst, st = [], [], []
    for k, code in enumerate(codes):
        rows += _walk(rng, f"CN:{code}", days, 10.0 + k, ex_at=ex_at, factor=factor, limits={bars - 5: "涨停", bars - 3: "一字"})
        inst.append({"code": code, "exchange": "XSHG" if code.startswith("6") else "XSHE", "board": "主板", "list_date": days[0]})
    for i in range(extra_listings):
        code = "830001" if i == 1 else str(600100 + i)
        own = days[-30:] if i == 2 else days
        rows += _walk(rng, f"CN:{code}", own, float(rng.uniform(5, 30)))
        inst.append({"code": code, "exchange": "XBSE" if i == 1 else "XSHG", "board": "北交所" if i == 1 else "主板",
                     "list_date": own[0]})
        if i == 0:
            st.append({"listing_id": f"CN:{code}", "kind": "st", "value": "ST", "start": days[-60], "source": "t"})
    bars_df = pl.DataFrame(rows)
    idx = (bars_df.group_by("date").agg(pl.col("open").mean(), pl.col("high").mean(), pl.col("low").mean(),
                                         pl.col("close").mean()).sort("date").with_columns(pl.lit("t").alias("source")))
    ins = pl.DataFrame(inst).with_columns((pl.lit("CN:") + pl.col("code")).alias("listing_id"), pl.lit("CN").alias("market"),
                                          ("测试" + pl.col("code")).alias("name"), pl.lit("L").alias("status"),
                                          pl.lit("t").alias("source"))
    data = {"bars_1d": conform(bars_df, "bars_1d"), "instruments": conform(ins, "instruments"),
            "calendar": conform(pl.DataFrame({"date": days, "is_open": True, "source": "t"}), "calendar"),
            "index_bars_1d": conform(pl.concat([idx.with_columns(pl.lit(i).alias("index_id"))
                                                for i in ("CN:IDX:000001", "CN:IDX:399001", "CN:IDX:000300")]), "index_bars_1d")}
    if st:
        data["status_intervals"] = conform(pl.DataFrame(st, schema_overrides={"start": pl.Date}), "status_intervals")
    Store(paths, "CN").publish(data, as_of=days[-1], note="pa")


def bar_on(frame: KlineFrame, d: date) -> KlineBar:
    ms = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return next(b for b in frame.bars if b.ts_open == ms)


def plan(**kw) -> dict:
    """A stage-2 decision: 做多 限价单 at 10.00 by default, stop 9.60, TP1 10.40, TP2 10.80, confidence and win rate 60."""
    return {"order_type": "限价单", "order_direction": "做多", "entry_price": 10.0, "stop_loss_price": 9.6,
            "take_profit_price": 10.4, "take_profit_price_2": 10.8, "trade_confidence": 60, "estimated_win_rate": 60,
            "reasoning": "x", "invalidation_condition": "x", **kw}


def stage2(decision: dict, terminal: dict | None = None, trace: list | None = None) -> dict:
    """A stage-2 answer around `decision`, ending in a trade at §11.2 unless `terminal` says otherwise."""
    return {"decision": decision, "terminal": terminal or {"node_id": "11.2", "outcome": "trade", "label": "x"},
            "decision_trace": trace or []}


def answers(frame: KlineFrame, kind: str = "limit", r: float = 1.0) -> list[str]:
    """The model's replies for `frame`, from tests/data/pa (they pass PA's own validator): stage 1 proceeds, stage 2 a
    pending 限价单 2% under K1 with the stop 4% under the entry, TP1 at `r` R and TP2 one R further, confidence and win
    rate 60. `kind="short"`: the same mirrored above K1 for 做空; `kind="wait"`: stage 1's gate says wait (no stage 2)."""
    if kind == "wait":
        return [(DATA / "stage1_wait.json").read_text()]
    side = -1 if kind == "short" else 1
    entry = round(frame.bars[0].close * (1 - 0.02 * side), 2)
    stop = round(entry * (1 - 0.04 * side), 2)
    values = {"entry": entry, "stop": stop, "tp1": round(entry + r * (entry - stop), 2),
              "tp2": round(entry + (r + 1) * (entry - stop), 2), "conf": 60, "win": 60}
    text = (DATA / "stage2_limit.json").read_text()
    for k, v in values.items():
        text = text.replace(f'"{{{k}}}"', str(v)).replace(f"{{{k}}}", str(v))
    s2 = json.loads(text)
    if kind == "short":
        s2["decision"]["order_direction"], s2["bar_analysis"]["always_in"] = "做空", "short"
    return [(DATA / "stage1_proceed.json").read_text(), json.dumps(s2, ensure_ascii=False)]


def transport(replies: list[str]) -> httpx.MockTransport:
    """The model's side: each request gets the next reply, with the token use DeepSeek reports."""
    it = iter(replies)
    return httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": next(it)}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}))
