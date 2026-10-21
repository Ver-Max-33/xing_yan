"""价格行为分析's settlement (design 2026-10-20 §5.2): each plan traded as A-share rules let it — a waiting order for three
of the stock's own days, a market buy only at the start day's open, no sale on the buy day (T+1), the stop before the
target when a day touches both, a sealed limit-down holding the stop to the next open, 20 days at most. Lines are in
adjusted prices from day 0, so an ex-date between changes nothing; a fill is at that day's real price."""
import hashlib
import logging
import random
import statistics
import threading
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect, tx
from wb.engine.l1.frame import build_frame
from wb.engine.l3.rule import RuleError
from wb.pa import records
from wb.pa.plans import PRICES, is_order
from wb.validate.outcomes import outcomes
from wb.validate.plantrades import benchmark
from wb.validate.portfolio import COSTS
from wb.validate.trades import SCHEMA, returns

log = logging.getLogger(__name__)

STATUSES = ("no_plan", "short", "waiting", "not_bought", "void", "holding", "done", "gone")
FINAL = {"no_plan", "short", "not_bought", "void", "done", "gone"}
TARGETS = {"tp1": "take_profit_price", "tp2": "take_profit_price_2"}
TRACKS = ("ai", "final")
HOLD = 20          # trading days: a plan's longest hold, the forward excess, a halt longer than this is the end
DRAWS = 20         # stocks per random control
YOUNG = 60         # trading days a stock must have been listed before day 0 to be drawn
BEIJING = ("4", "8", "92")
ENOUGH = 30        # settled plans before the evaluation reads as more than numbers (§5.5)
COLS = {"open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close", "up_limit", "down_limit"}
_SETTLED: set[tuple[str, str, str]] = set()   # (data folder, profile, snapshot) already settled by this process
_SETTLING = threading.Lock()


@dataclass(frozen=True)
class Trade:
    status: str
    entry_date: date | None = None
    entry_price: float | None = None     # raw, that day's real price
    entry_adj: float | None = None
    exit_date: date | None = None
    exit_price: float | None = None
    exit_adj: float | None = None
    reason: str | None = None            # stop / tp1 / tp2 / days / gone
    days: int | None = None              # bars held, the buy day being 1
    r: float | None = None
    at_open: bool = False                # sold at the open


def _cents(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def simulate(plan: dict | None, bars: pl.DataFrame, *, day0: date, start: date, target: Literal["tp1", "tp2"],
             live: bool, window: int = 3, hold: int = 20) -> Trade:
    """`bars`: the listing's rows from day 0 on — date, open, high, low, close (raw), adj (raw → adjusted), up, down (raw
    limits, null: none). `live`: the listing still trades, so data that ends early is not the end."""
    if not is_order(plan):
        return Trade("no_plan")
    if plan.get("order_direction") == "做空":
        return Trade("short")
    rows = bars.sort("date").to_dicts()
    upto = [b for b in rows if b["date"] <= day0]
    adj0 = (upto[-1] if upto else rows[0])["adj"] if rows else 1.0

    def line(key: str) -> float | None:
        return plan[key] * adj0 if plan.get(key) is not None else None

    kind, entry, stop, take = plan["order_type"], line("entry_price"), line("stop_loss_price"), line(TARGETS[target])
    if entry is None and kind != "市价单":               # an order without its price (an AI answer can lack it)
        return Trade("no_plan")
    after = [b for b in rows if b["date"] >= start]
    if not after:                                       # the start day is not in the data yet
        return Trade("waiting" if live else "not_bought")
    fill = None                                         # (index in `after`, raw price)
    for i, b in enumerate(after[:window]):
        a, up = b["adj"], b["up"] if b["up"] is not None else float("inf")
        if kind == "市价单":
            if b["date"] == start and b["open"] < up:   # halted that day, or opened at the limit-up: not bought
                fill = (i, b["open"])
            break
        if kind == "限价单" and b["low"] * a <= entry:
            fill = (i, min(b["open"], entry / a))
            break
        if kind == "突破单" and b["high"] * a >= entry and _cents(entry / a) < up and not b["high"] == b["low"] == up:
            fill = (i, max(b["open"], entry / a))
            break
        if stop is not None and b["low"] * a <= stop:   # the stop came before the buy: the plan no longer stands
            return Trade("void", exit_date=b["date"])
    if fill is None:   # a plan that ended unbought keeps its last day, where its lines on the chart stop
        if live and kind != "市价单" and len(after) < window:
            return Trade("waiting")
        return Trade("not_bought", exit_date=start if kind == "市价单" else after[:window][-1]["date"])

    i0, price = fill
    held = after[i0:]
    first = held[0]
    entry_adj = price * first["adj"]

    def out(status: str, b: dict | None, px: float | None, reason: str | None, n: int, at_open: bool = False) -> Trade:
        exit_adj = px * b["adj"] if b is not None else None
        r = ((exit_adj - entry_adj) / (entry_adj - stop) if exit_adj is not None and stop is not None and entry_adj > stop
             else None)
        return Trade(status, first["date"], price, entry_adj, b["date"] if b else None, px, exit_adj, reason, n, r, at_open)

    pending = False                                      # a stop due on a sealed limit-down day: sold at the next open
    for n, b in enumerate(held[1:], start=2):
        a = b["adj"]
        o, h, lo = b["open"] * a, b["high"] * a, b["low"] * a
        sealed = b["down"] is not None and b["high"] == b["low"] == b["down"]
        if pending or (stop is not None and o <= stop):
            if sealed:
                pending = True
                continue
            return out("done", b, b["open"], "stop", n, at_open=True)
        if take is not None and o >= take:
            return out("done", b, b["open"], target, n, at_open=True)
        if stop is not None and lo <= stop:
            if sealed:
                pending = True
                continue
            return out("done", b, stop / a, "stop", n)
        if take is not None and h >= take:
            return out("done", b, take / a, target, n)
        if n >= hold:
            return out("done", b, b["close"], "days", n)
    if live:
        return out("holding", None, None, None, len(held))
    return out("gone", held[-1], held[-1]["close"], "gone", len(held))


def net_return(trades: list[tuple[str, Trade, str]], bench: pl.DataFrame) -> dict[str, float | None]:
    """Each finished trade's return net of the backtests' CN costs (wb.validate.trades.returns): `(key, trade,
    entry_kind)`, the kind "open" for a market buy and "cross" for the rest. -> key → ret (None while not finished)."""
    out: dict[str, float | None] = {k: None for k, _, _ in trades}
    for kind in ("open", "cross"):
        mine = [(k, t) for k, t, e in trades if e == kind and t.status in ("done", "gone")]
        if not mine:
            continue
        rows = [{"sid": i, "status": t.status, "entry_date": t.entry_date, "entry_price": t.entry_price,
                 "entry_adj": t.entry_adj, "held": t.days, "mfe": None, "mae": None, "reason": t.reason, "no_room": 0,
                 "fills": [{"date": t.exit_date, "price": t.exit_price, "adj": t.exit_adj / t.exit_price, "share": 1.0,
                            "why": t.reason, "at_open": t.at_open}]} for i, (k, t) in enumerate(mine)]
        got = returns(pl.DataFrame(rows, schema=SCHEMA), bench, COSTS["CN"], kind).sort("sid")
        out |= dict(zip([k for k, _ in mine], got["ret"].to_list()))
    return out


def pool(paths: Paths, day0: date, *, exclude: str | None) -> list[str]:
    """§5.2's control draws from these: 沪深 stocks that traded on day 0, not ST that day, listed at least 60 trading
    days before it (by instruments.list_date and the calendar, so no ten years of bars are read), sorted."""
    store = Store(paths, "CN")
    snap = store.latest()
    cal = records.calendar(paths)
    k = bisect_right(cal, day0) - 1
    if snap is None or k < YOUNG:
        return []
    traded = store.scan("bars_1d", snap).filter((pl.col("date") == day0) & pl.col("close").is_not_null()).select("listing_id")
    st = (store.scan("status_intervals", snap).filter((pl.col("kind") == "st") & (pl.col("start") <= day0)
                                                      & (pl.col("end").is_null() | (pl.col("end") >= day0)))
          .select("listing_id"))
    old = store.scan("instruments", snap).filter(
        pl.col("list_date").is_not_null() & (pl.col("list_date") <= cal[k - YOUNG])
        & ~pl.any_horizontal([pl.col("code").str.starts_with(b) for b in BEIJING]))
    ids = (old.join(traded, on="listing_id", how="semi").join(st, on="listing_id", how="anti")
           .filter(pl.col("listing_id") != (exclude or "")).select("listing_id").collect()["listing_id"])
    return sorted(ids.to_list())


def bars_for(paths: Paths, listings: list[str], day0: date) -> pl.DataFrame:
    """The listings' traded days from day 0 on as `simulate` reads them — raw prices, the factor to adjusted prices, the
    raw limits — and `live`: still listed and traded within the last 20 trading days, so a shorter halt is not the end
    of a plan (§5.2: 退市或长期停牌)."""
    store = Store(paths, "CN")
    snap = store.latest()
    df = (build_frame(store, "CN", paths.profile, COLS, listings=listings, snap=snap)   # all of it: the factor carries on
          .filter(pl.col("date") >= day0).drop_nulls(["close", "raw_open", "raw_high", "raw_low", "raw_close"])
          .select("listing_id", "date", pl.col("raw_open").alias("open"), pl.col("raw_high").alias("high"),
                  pl.col("raw_low").alias("low"), pl.col("raw_close").alias("close"),
                  (pl.col("close") / pl.col("raw_close")).alias("adj"), pl.col("up_limit").alias("up"),
                  pl.col("down_limit").alias("down")))
    cal = [d for d in records.calendar(paths) if d <= snap.as_of]
    recent = cal[-HOLD] if len(cal) >= HOLD else (cal[0] if cal else snap.as_of)
    gone = (store.scan("instruments", snap).filter(pl.col("listing_id").is_in(listings)
                                                   & (pl.col("delist_date").is_not_null() | (pl.col("status") == "D")))
            .select("listing_id").collect()["listing_id"].to_list())
    last = df.group_by("listing_id").agg(pl.col("date").max().alias("_last"))
    return (df.join(last, on="listing_id").with_columns(((pl.col("_last") >= recent) & ~pl.col("listing_id").is_in(gone))
                                                        .alias("live")).drop("_last").sort("listing_id", "date"))


def _bench(paths: Paths) -> pl.DataFrame:
    store = Store(paths, "CN")
    return benchmark(store, "CN", paths.profile, store.latest())


def _kind(plan: dict | None) -> str:
    return "open" if (plan or {}).get("order_type") == "市价单" else "cross"


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def control(paths: Paths, analysis: dict, track: str, plan: dict | None, target: str, *, draws: int = DRAWS,
            pools: dict | None = None) -> dict | None:
    """§5.2: the same plan on `draws` stocks drawn at random from day 0's pool — the same order type, the same distances
    from day 0's close (each stock's prices scaled by its own day-0 close ÷ the analysed stock's), the same rules and
    costs; the draw seeded by the analysis, track and target. None for a plan that is not a long order. `pools`: day 0 →
    pool, kept for one settle_all run."""
    if not is_order(plan) or plan.get("order_direction") == "做空":
        return None
    day0, start = date.fromisoformat(analysis["day"]), date.fromisoformat(analysis["start_date"])
    pools = {} if pools is None else pools
    if day0 not in pools:
        pools[day0] = pool(paths, day0, exclude=None)
    cand = [x for x in pools[day0] if x != analysis["listing_id"]]
    rng = random.Random(int(hashlib.sha256(f"{analysis['id']}|{track}|{target}".encode()).hexdigest()[:8], 16))
    order = rng.sample(cand, len(cand))   # the seeded draw in order: a stock without a clean day-0 bar is passed over
    picked, frames, i = [], [], 0
    while len(picked) < draws and i < len(order):
        batch = order[i:i + draws - len(picked)]   # bars read only for the stocks still missing
        i += len(batch)
        frames.append(b := bars_for(paths, batch, day0))
        clean = set(b.filter(pl.col("date") == day0)["listing_id"].to_list())
        picked += [x for x in batch if x in clean]
    bars = pl.concat(frames) if frames else None
    k1 = analysis["input"]["k1_close"]
    trades = []
    for lid in picked:
        mine = bars.filter(pl.col("listing_id") == lid)
        close0 = mine.filter(pl.col("date") == day0)["close"]
        scaled = {**plan, **{f: plan[f] * close0[0] / k1 for f in PRICES if plan.get(f) is not None}}
        trades.append((lid, simulate(scaled, mine, day0=day0, start=start, target=target, live=bool(mine["live"][0])),
                       _kind(plan)))
    rets = net_return(trades, _bench(paths))
    done = [(lid, t) for lid, t, _ in trades if t.status in ("done", "gone")]
    return {"n": len(trades), "bought": sum(t.entry_date is not None for _, t, _ in trades),
            "win": sum(t.reason == target for _, t in done) / len(done) if done else None,
            "ret_mean": _mean([rets[lid] for lid, _ in done if rets[lid] is not None]),
            "ret_median": _median([rets[lid] for lid, _ in done if rets[lid] is not None]),
            "r_mean": _mean([t.r for _, t in done if t.r is not None]),
            "final": all(t.status in FINAL for _, t, _ in trades)}


def _trade_one(paths: Paths, conn, a: dict, cal: list[date], bench: pl.DataFrame, pools: dict) -> bool | None:
    """One analysis's start day (once the calendar has it), its 2 tracks × 2 targets traded with their controls and
    upserted into pa_trade. -> whether all of it is final; None while its start day is unknown."""
    if a["start_date"] is None:
        if (s := records.start_date(datetime.fromisoformat(a["created_at"]), cal)) is None:
            return None
        a["start_date"] = s.isoformat()
        conn.execute("UPDATE pa_analysis SET start_date=? WHERE id=?", (a["start_date"], a["id"]))
    day0, start = date.fromisoformat(a["day"]), date.fromisoformat(a["start_date"])
    own = bars_for(paths, [a["listing_id"]], day0)
    live = bool(own["live"][0]) if own.height else False
    got = {}
    for track in TRACKS:
        p = a[f"{track}_plan"]
        for target in TARGETS:
            got[(track, target)] = (simulate(p, own, day0=day0, start=start, target=target, live=live),
                                    control(paths, a, track, p, target, pools=pools))
    rets = net_return([(f"{tr}|{tg}", t, _kind(a[f"{tr}_plan"])) for (tr, tg), (t, _) in got.items()], bench)
    with tx(conn):
        for (tr, tg), (t, c) in got.items():
            c = c or {}
            conn.execute("INSERT OR REPLACE INTO pa_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                a["id"], tr, tg, t.status, t.entry_date and t.entry_date.isoformat(), t.entry_price,
                t.exit_date and t.exit_date.isoformat(), t.exit_price, t.reason, t.days, rets[f"{tr}|{tg}"], t.r,
                c.get("n"), c.get("bought"), c.get("win"), c.get("ret_mean"), c.get("ret_median"), c.get("r_mean"),
                int(t.status in FINAL and c.get("final", True))))
    return all(t.status in FINAL and (c is None or c["final"]) for t, c in got.values())


def settle_all(paths_of: Callable[[str], Paths], *, wait: bool = True) -> int:
    """Every successful analysis on real 正式档 data not yet settled, on the CN profile in use: its start day once the
    calendar has it; both plans × both targets traded, each with its control (pa_trade); after 20 trading days from the
    first open, the 5- and 20-day excess (wb.validate.outcomes, journal.settle's rule). An analysis is settled once
    every trade and control is final and the excess is known. Once per snapshot: nothing settles further without new
    bars (one made since starts after them). `wait` False: back at once while another run settles (a page load does
    not queue behind the daily run). -> analyses settled now."""
    if not _SETTLING.acquire(blocking=wait):
        return 0
    try:
        return _settle_all(paths_of)
    finally:
        _SETTLING.release()


def _settle_all(paths_of: Callable[[str], Paths]) -> int:
    paths = paths_of("CN")
    conn = connect(paths.shared_db, "shared")
    rows = [records._row(r) for r in conn.execute(
        "SELECT * FROM pa_analysis WHERE status='ok' AND demo=0 AND adjusted=1 AND settled=0 AND profile=?", (paths.profile,))]
    snap = Store(paths, "CN").latest()
    key = (str(paths.root), paths.profile, snap.id if snap else "")
    if not rows or snap is None or key in _SETTLED:
        return 0
    cal, bench, pools, finals, due = records.calendar(paths), _bench(paths), {}, {}, []
    # ponytail: every unsettled analysis is traded again each new snapshot (≤ 20 days each); keep the final ones if slow
    for a in rows:
        try:   # one that cannot be settled (a data slip) is logged and tried again with the next snapshot, not the end
            got = _trade_one(paths, conn, a, cal, bench, pools)
        except Exception:  # noqa: BLE001
            log.exception("价格行为 settle %s", a["id"])
            continue
        if got is None:
            continue
        finals[a["id"]] = got
        made = datetime.fromisoformat(a["created_at"])
        k = bisect_right(cal, made.astimezone(records.TZ).date()) - 1
        if a["fwd_outcome"] is None and 0 <= k and k + HOLD < len(cal) and cal[k + HOLD] <= snap.as_of:
            due.append((a, cal[k]))
    known = {a["id"] for a in rows if a["fwd_outcome"] is not None}
    if due:
        try:
            o = outcomes(paths, "CN", pl.DataFrame({"listing_id": [a["listing_id"] for a, _ in due],
                                                    "signal_date": [d for _, d in due]}))
            by = {(x["listing_id"], x["signal_date"]): x for x in o.iter_rows(named=True)}
            with tx(conn):
                for a, d in due:
                    x = by[(a["listing_id"], d)]
                    outcome = "gone" if x["gone"] else "halt" if x["blocked"] == "halt" else "settled"
                    ok = outcome == "settled"
                    conn.execute("UPDATE pa_analysis SET fwd_outcome=?, r5=?, x5=?, r20=?, x20=? WHERE id=?",
                                 (outcome, x["r5"] if ok else None, x["x5"] if ok else None, x["r20"] if ok else None,
                                  x["x20"] if ok else None, a["id"]))
                    known.add(a["id"])
        except RuleError as e:   # no benchmark bars yet: wait
            log.warning("价格行为 settle: %s", e)
    settled = [aid for aid, ok in finals.items() if ok and aid in known]
    with tx(conn):
        for aid in settled:
            conn.execute("UPDATE pa_analysis SET settled=1 WHERE id=?", (aid,))
    _SETTLED.add(key)
    return len(settled)


def _plan_stats(rows: list[dict], trades: dict, track: str, target: str) -> dict:
    got = [t for a in rows if (t := trades.get((a["id"], track, target)))]
    settled = [t for t in got if t["status"] in ("done", "gone")]
    rets = [t["ret"] for t in settled if t["ret"] is not None]
    orders = sum(is_order(p := a[f"{track}_plan"]) and p.get("order_direction") != "做空" for a in rows)   # buyable ones
    bought = sum(t["entry_date"] is not None for t in got)
    return {"n": len(rows), "orders": orders, "bought": bought, "bought_share": bought / orders if orders else None,
            "settled": len(settled), "win": sum(t["reason"] == target for t in settled) / len(settled) if settled else None,
            "ret_mean": _mean(rets), "ret_median": _median(rets), "r_mean": _mean([t["r"] for t in settled if t["r"] is not None])}


def _control_stats(rows: list[dict], trades: dict, track: str, target: str) -> dict:
    """Read per analysis, as the plan beside it is: each analysis's control is one number, the mean of its draws, so
    the mean and the median are across analyses; what share of the draws got bought (a count of draws would read as
    analyses)."""
    got = [t for a in rows if (t := trades.get((a["id"], track, target))) and t["ctrl_n"]]
    done = [t for t in got if t["final"]]
    means = [t["ctrl_ret_mean"] for t in done if t["ctrl_ret_mean"] is not None]
    draws = sum(t["ctrl_n"] for t in got)
    return {"n": len(got), "orders": len(got), "bought": None, "bought_share": sum(t["ctrl_bought"] for t in got) / draws if draws else None,
            "settled": len(done), "win": _mean([t["ctrl_win"] for t in done if t["ctrl_win"] is not None]),
            "ret_mean": _mean(means), "ret_median": _median(means),
            "r_mean": _mean([t["ctrl_r_mean"] for t in done if t["ctrl_r_mean"] is not None])}


def _plans(rows: list[dict], trades: dict) -> dict:
    out = {}
    for track in TRACKS:
        out[track] = {g: _plan_stats(rows, trades, track, g) for g in TARGETS}
        out[f"{track}_control"] = {g: _control_stats(rows, trades, track, g) for g in TARGETS}
    return out


def _program_direction(a: dict) -> str | None:
    """§5.4: the direction PA's program judged (§2.3's five-signal vote), before any AI override of it."""
    for n in (a["stage1"] or {}).get("gate_trace") or []:
        if isinstance(n, dict) and str(n.get("node_id", "")).strip() == "2.3":
            return n.get("program_branch") if n.get("overridden_by_ai") else n.get("branch")
    return None


def evaluation(paths: Paths) -> dict:
    """§5.4, §5.5: over each stock's first successful analysis of a day 0 on real 正式档 data — what both plans and their
    controls did per target; the 5- and 20-day excess by the program's direction, the AI's direction, its stage, its
    next-stage call and whether it said buy; the same plans for 盘中 analyses and the rest; `enough` once 30 final plans are settled."""
    conn = connect(paths.shared_db, "shared")
    rows = [records._row(r) for r in conn.execute(
        "SELECT * FROM pa_analysis WHERE status='ok' AND first_of_day=1 AND demo=0 AND adjusted=1 ORDER BY rowid")]
    trades = {(r["analysis_id"], r["track"], r["target"]): dict(r) for r in conn.execute("SELECT * FROM pa_trade")}
    keys = {"program": _program_direction,
            "direction": lambda a: (a["stage1"] or {}).get("direction"),
            "cycle": lambda a: (a["stage1"] or {}).get("cycle_position"),
            "next": lambda a: ((a["stage2"] or {}).get("next_cycle_prediction") or {}).get("direction"),
            "buy": lambda a: "买" if is_order(a["final_plan"]) else "不买"}
    groups = {}
    for name, key in keys.items():
        groups[name] = []
        for k in sorted({key(a) for a in rows}, key=str):
            mine = [a for a in rows if key(a) == k]
            x5 = [a["x5"] for a in mine if a["x5"] is not None]
            x20 = [a["x20"] for a in mine if a["x20"] is not None]
            groups[name].append({"key": k, "n": len(mine), "settled": len(x20), "x5_mean": _mean(x5), "x5_median": _median(x5),
                                 "x20_mean": _mean(x20), "x20_median": _median(x20)})
    plans = _plans(rows, trades)
    return {"plans": plans, "groups": groups,
            "intraday": {"yes": _plans([a for a in rows if a["intraday"]], trades),
                         "no": _plans([a for a in rows if not a["intraday"]], trades)},
            "enough": plans["final"]["tp1"]["settled"] >= ENOUGH}
