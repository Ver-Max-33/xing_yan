"""Deterministic demo/test data (no network): `wb devdata --data-dir DIR`.

Writes a small but realistic test-profile dataset for both markets so the UI and end-to-end
tests run without API keys: random-walk bars with a JP 1:5 split and a CN 10送3 bonus (so the
adjustment paths are exercised), limit prices, instruments, calendars and indices.
"""
import random
from datetime import date, timedelta

import polars as pl

from wb.config import Paths
from wb.data.normalize import apply_factors
from wb.data.store import Store
from wb.datasets import conform

END = date(2026, 9, 30)

DEMO = "devdata"   # the note on a demo snapshot: made-up data is never sent, notified or evaluated


def trading_days(n: int, end: date = END) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def walk(seed: int, n: int, start: float) -> list[float]:
    rnd, px, out = random.Random(seed), start, []
    for _ in range(n):
        px = max(1.0, px * (1 + rnd.gauss(0.0004, 0.018)))
        out.append(round(px, 1))
    return out


def stock_rows(listing: str, days: list[date], seed: int, start: float, event_day: int | None, ratio: float,
               cn: bool) -> tuple[pl.DataFrame, pl.DataFrame]:
    closes = walk(seed, len(days), start)
    rnd = random.Random(seed + 1)
    rows, factor = [], 1.0
    for i, (d, c) in enumerate(zip(days, closes, strict=True)):
        if event_day is not None and i >= event_day:
            factor = ratio
        raw = round(c / factor, 2 if cn else 1)
        prev_raw = rows[-1]["close"] if rows else raw
        pre = round(prev_raw / (ratio if (event_day is not None and i == event_day) else 1.0), 2 if cn else 1)
        o = round(pre * (1 + rnd.uniform(-0.01, 0.01)), 2 if cn else 1)
        hi, lo = max(o, raw) * (1 + rnd.uniform(0, 0.012)), min(o, raw) * (1 - rnd.uniform(0, 0.012))
        vol = float(int(rnd.uniform(0.6, 1.6) * 1_000_000 * factor))
        rows.append({"listing_id": listing, "date": d, "open": o, "high": round(hi, 2), "low": round(lo, 2), "close": raw,
                     "pre_close": pre if cn else None, "volume": vol, "amount": round(vol * (o + raw) / 2, 0),
                     "adj_factor": factor if cn else None,
                     "up_limit": round(pre * 1.1, 2) if cn else None, "down_limit": round(pre * 0.9, 2) if cn else None,
                     "ul": False if not cn else None, "ll": False if not cn else None, "suspended": False, "source": "devdata"})
    bars = conform(pl.DataFrame(rows), "bars_1d")
    actions = pl.DataFrame({"listing_id": [listing], "ex_date": [days[event_day]], "kind": ["split" if not cn else "dividend"],
                            "ratio": [ratio], "source": ["devdata"]}) if event_day is not None else pl.DataFrame(
        schema={"listing_id": pl.Utf8, "ex_date": pl.Date, "kind": pl.Utf8, "ratio": pl.Float64, "source": pl.Utf8})
    return bars, conform(actions, "corp_actions")


def index_rows(index_id: str, days: list[date], seed: int, start: float) -> pl.DataFrame:
    closes = walk(seed, len(days), start)
    return conform(pl.DataFrame({"index_id": index_id, "date": days, "open": closes, "high": [c * 1.005 for c in closes],
                                 "low": [c * 0.995 for c in closes], "close": closes, "source": "devdata"}), "index_bars_1d")


# the instruments list's own industry: Tushare's label for A shares (shown nowhere; a rule naming it is told where it sits
# in 申万), the 33 業種 for 日股 (design 2026-10-12-market-page-redesign §6.3)
LABEL = {"CN:600519": "白酒", "CN:000001": "银行", "JP:7203": "輸送用機器", "JP:130A": "医薬品", "JP:6758": "電気機器"}
SHENWAN = {"CN:600519": ("801120", "食品饮料", "801125", "白酒Ⅱ"), "CN:000001": ("801780", "银行", "801782", "股份制银行Ⅱ")}


def shenwan(start: date) -> pl.DataFrame:
    """The demo A shares' 申万 一级 · 二级 from the first demo day on."""
    rows = [(lid, c1, n1, c2, n2) for lid, (c1, n1, c2, n2) in SHENWAN.items()]
    return conform(pl.DataFrame({"listing_id": [r[0] for r in rows], "scheme": "SW2021", "code": [r[1] for r in rows],
                                 "name": [r[2] for r in rows], "in_date": start, "out_date": None, "source": "devdata",
                                 "l2_code": [r[3] for r in rows], "l2_name": [r[4] for r in rows]},
                                schema_overrides={"out_date": pl.Date}), "industry")


def write(paths: Paths, n: int = 320) -> None:
    days = trading_days(n)
    cal = conform(pl.DataFrame({"date": days, "is_open": True, "source": "devdata"}), "calendar")
    spec = {
        "JP": [("JP:7203", "7203", "トヨタ自動車", "TOYOTA MOTOR", 11, 2800.0, 200, 5.0),
               ("JP:130A", "130A", "Veritas In Silico", "Veritas In Silico", 12, 450.0, None, 1.0),
               ("JP:6758", "6758", "ソニーグループ", "SONY GROUP", 13, 3200.0, None, 1.0)],
        "CN": [("CN:600519", "600519", "贵州茅台", "Kweichow Moutai", 21, 1500.0, 150, 1.3),
               ("CN:000001", "000001", "平安银行", "Ping An Bank", 22, 11.0, None, 1.0)],
    }
    for market, stocks in spec.items():
        bars, actions = [], []
        for lid, code, name, en, seed, start, ev, ratio in stocks:
            b, a = stock_rows(lid, days, seed, start, ev, ratio, market == "CN")
            bars.append(b)
            actions.append(a)
        all_bars, all_actions = pl.concat(bars), pl.concat(actions)
        all_bars = apply_factors(all_bars, all_actions, set_price=market == "JP")
        inst = conform(pl.DataFrame({
            "listing_id": [s[0] for s in stocks], "market": market, "code": [s[1] for s in stocks],
            "exchange": "XTKS" if market == "JP" else "XSHG", "name": [s[2] for s in stocks], "name_en": [s[3] for s in stocks],
            "board": "プライム（内国株式）" if market == "JP" else "主板", "status": "L", "source": "devdata",
            "industry": [LABEL[s[0]] for s in stocks]}), "instruments")
        idx = (pl.concat([index_rows("JP:IDX:N225", days, 31, 38000.0), index_rows("JP:IDX:TOPIX", days, 32, 2700.0)])
               if market == "JP" else pl.concat([index_rows("CN:IDX:000001", days, 41, 3300.0), index_rows("CN:IDX:000300", days, 42, 3900.0)]))
        extra = {"industry": shenwan(days[0])} if market == "CN" else {}
        Store(paths, market).publish({"bars_1d": all_bars, "corp_actions": all_actions, "instruments": inst,
                                      "calendar": cal, "index_bars_1d": idx, **extra}, as_of=days[-1], note=DEMO)
