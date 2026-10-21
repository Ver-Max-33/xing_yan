"""交易计划的撮合 (design 2026-10-09 §5, 2026-10-11 §7): each signal traded by its plan on its stock's own daily bars.

The signal day is day 0. `entry: close` buys at day 0's close (holding from day 1), `entry: open` at the next bar's
open — only if the stock trades on the market's next day (`next_ok`; else a halt: never bought at a later open) —
`entry: cross` during day 0 at the signal's own price (`entry_price`: where the price reached the line; its high and
low count for the best and worst move, its close is the first the plan reads). A buy at or over the day's limit up
(CN: stk_limit; JP: a UL day at its high) is not filled: `limit`, not chased.
Each holding bar, in this order:

  1. the open — sales waiting from an earlier close (or refused at a limit down) at the open, unless it is at the
     limit down again; a 目标位 the open is already at or over sells its share at the open;
  2. the day — a 目标位 the high reaches sells its share at the line (bought this bar under T+1: at the next open);
  3. the close — a close under a 失效线, a 卖出条件 holding, `stall` closes without a new highest close since entry,
     the days-th bar after the signal: sold at this close (`fill: close`) or waiting for the next open (`fill: open`,
     T+1 on the bar bought, a close at the limit down). The days-th bar is known before its close: sold at the close
     whatever `fill` says (as the judging before plans did).

A share of 1 is all that is left; a smaller one is of the first position; nothing sells more than is left. Lines are
judged in adjusted prices (raw × factor, as the structures are), sales fill at raw prices. A 目标位 not above the entry
price is no target (`no_room`). The data's end with shares held is `open` (not in the statistics); a stock that no
longer trades by then (delisted, a long halt) is sold at its last close: `gone`. Days are the stock's own trading
days (a halt is not a day)."""
import numpy as np
import polars as pl
from numba import njit

from wb.engine.l3.rule import Plan

EPS = 1e-9
STATUS = ("done", "limit", "halt", "open", "gone")
DONE, LIMIT, HALT, OPEN, GONE = range(5)
STALL, DAYS, LAST = 300, 301, 302   # why a sale: stop j → j, take j → 100 + j, exit j → 200 + j
AT_OPEN, AT_CLOSE, AT_PRICE = 0, 1, 2   # entry: the next open, the signal close, the signal's price that day
ENTRY = {"open": AT_OPEN, "close": AT_CLOSE, "cross": AT_PRICE}


@njit(cache=True)
def _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, k, px, q, why, at_open):
    i = nf[t]
    if i < fbar.shape[1]:
        fbar[t, i], fpx[t, i], fshare[t, i], fwhy[t, i], fopen[t, i] = k, px, q, why, at_open
        nf[t] = i + 1


@njit(cache=True)
def _down(px, dn, ll, low):
    return px <= dn + EPS or (ll and px <= low + EPS)


@njit(cache=True)
def _kernel(o, h, l, c, adj, up, dn, ul, ll, flags, live, sbar, next_ok, given, stops, takes, entry, fill_close,  # noqa: E741
            stop_share, take_share, exit_share, stall, days, t1, relative,
            st, ebar, epx, eadj, nf, fbar, fpx, fshare, fwhy, fopen, mfe, mae, noroom, held):
    n = len(c)
    nstop, ntake, nexit = stops.shape[1], takes.shape[1], flags.shape[1]
    for t in range(len(sbar)):
        s = sbar[t]
        if s < 0:
            st[t] = HALT
            continue
        if entry == AT_CLOSE or entry == AT_PRICE:
            e = s
            px = c[s] if entry == AT_CLOSE else given[t]
            if np.isnan(px):
                st[t] = HALT
                continue
            if px >= up[s] - EPS or (ul[s] and px >= h[s] - EPS):
                st[t] = LIMIT
                continue
        else:
            if not next_ok[t]:
                st[t] = HALT
                continue
            if s + 1 >= n:   # no next bar in the data yet: not bought, not in the statistics
                st[t] = OPEN
                continue
            e = s + 1
            px = o[e]
            if px >= up[e] - EPS or (ul[e] and px >= h[e] - EPS):
                st[t] = LIMIT
                continue
        ea = px * adj[e]
        ebar[t], epx[t], eadj[t] = e, px, ea
        sl = stops[t].copy()
        tl = takes[t].copy()
        if relative:
            sl = sl * ea
            tl = tl * ea
        sdone = np.zeros(nstop, dtype=np.bool_)
        tdone = np.zeros(ntake, dtype=np.bool_)
        xdone = np.zeros(nexit, dtype=np.bool_)
        for j in range(nstop):
            sdone[j] = np.isnan(sl[j])
        for j in range(ntake):
            if np.isnan(tl[j]):
                tdone[j] = True
            elif tl[j] <= ea + EPS:   # no room to rise: no target
                tdone[j] = True
                noroom[t] += 1
        rem, pend, pwhy = 1.0, 0.0, -1
        best = c[e] * adj[e] if entry == AT_CLOSE else -np.inf
        quiet = 0
        hi, lo = -np.inf, np.inf
        last = e
        finished = False
        for k in range(e + 1 if entry == AT_CLOSE else e, n):
            a = adj[k]
            sellable = not (t1 and k == e)
            if k != e:   # an open entry's own open is the buy; a cross entry's open came before it
                if pend > EPS and sellable and not _down(o[k], dn[k], ll[k], l[k]):
                    _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, k, o[k], pend, pwhy, True)
                    rem -= pend
                    pend, pwhy = 0.0, -1
                for j in range(ntake):
                    if not tdone[j] and o[k] * a >= tl[j] - EPS and sellable:
                        q = rem - pend if take_share[j] >= 1 else min(take_share[j], rem - pend)
                        if q > EPS:
                            _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, k, o[k], q, 100 + j, True)
                            rem -= q
                        tdone[j] = True
            hi = max(hi, h[k] * a)
            lo = min(lo, l[k] * a)
            for j in range(ntake):
                if not tdone[j] and h[k] * a >= tl[j] - EPS:
                    q = rem - pend if take_share[j] >= 1 else min(take_share[j], rem - pend)
                    if q > EPS:
                        if sellable:
                            _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, k, tl[j] / a, q, 100 + j, False)
                            rem -= q
                        else:
                            pend += q
                            if pwhy < 0:
                                pwhy = 100 + j
                    tdone[j] = True
            trig, every, why = 0.0, False, -1
            for j in range(nstop):
                if not sdone[j] and c[k] * a < sl[j] - EPS:
                    sdone[j] = True
                    every = every or stop_share[j] >= 1
                    trig += stop_share[j]
                    if why < 0:
                        why = j
            for j in range(nexit):
                if not xdone[j] and flags[k, j]:
                    xdone[j] = True
                    every = every or exit_share[j] >= 1
                    trig += exit_share[j]
                    if why < 0:
                        why = 200 + j
            if c[k] * a > best + EPS:
                best = c[k] * a
                quiet = 0
            else:
                quiet += 1
            if stall > 0 and quiet >= stall:
                every = True
                if why < 0:
                    why = STALL
            last_day = k - s >= days   # known before its close: sold at the close whatever `fill` says
            if last_day:
                every = True
                if why < 0:
                    why = DAYS
            q = rem - pend if every else min(trig, rem - pend)
            if q > EPS:
                if (fill_close or last_day) and sellable and not _down(c[k], dn[k], ll[k], l[k]):
                    _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, k, c[k], q, why, False)
                    rem -= q
                else:
                    pend += q
                    if pwhy < 0:
                        pwhy = why
            last = k
            if rem <= EPS:
                st[t] = DONE
                finished = True
                break
        if not finished:
            if live:
                st[t] = OPEN
            else:
                _sell(t, nf, fbar, fpx, fshare, fwhy, fopen, n - 1, c[n - 1], rem, LAST, False)
                st[t] = GONE
                last = n - 1
        if hi > -np.inf:
            mfe[t] = hi / ea - 1
            mae[t] = lo / ea - 1
        held[t] = last - e


def _labels(plan: Plan) -> dict[int, str]:
    out = {j: f"stop{j}" for j in range(len(plan.stop))} | {100 + j: f"take{j}" for j in range(len(plan.take))}
    return out | {200 + j: f"exit{j}" for j in range(len(plan.exit))} | {STALL: "stall", DAYS: "days", LAST: "gone"}


FILL = pl.Struct({"date": pl.Date, "price": pl.Float64, "adj": pl.Float64, "share": pl.Float64, "why": pl.Utf8, "at_open": pl.Boolean})
SCHEMA = {"sid": pl.Int64, "status": pl.Utf8, "entry_date": pl.Date, "entry_price": pl.Float64, "entry_adj": pl.Float64,
          "held": pl.Int64, "mfe": pl.Float64, "mae": pl.Float64, "reason": pl.Utf8, "no_room": pl.Int64, "fills": pl.List(FILL)}


def _col(df: pl.DataFrame, name: str, n: int, dtype, fill) -> np.ndarray:
    if name not in df.columns:
        return np.full(n, fill, dtype=dtype)
    return df[name].cast(pl.Float64 if dtype is np.float64 else pl.Boolean).fill_null(fill).to_numpy().astype(dtype)


def trade(signals: pl.DataFrame, bars: pl.DataFrame, plan: Plan, *, t_plus_1: bool, relative: bool = False) -> pl.DataFrame:
    """`signals`: sid, listing_id, signal_date, next_ok, stop0…, take0… (adjusted line prices; with `relative`, shares
    of the entry price; null: none), entry_price (raw, `entry: cross`). `bars`: listing_id, date, open, high, low, close (raw), adj (raw → adjusted),
    up, down (raw limits; null: none), ul, ll (JP's day flags), exit0… (the plan's 卖出条件 on that close), live (the
    listing trades on the data's last day), each listing's rows in date order. One row a signal (SCHEMA)."""
    labels = _labels(plan)
    nstop, ntake, nexit = len(plan.stop), len(plan.take), len(plan.exit)
    width = nstop + ntake + nexit + 4
    stop_share = np.array([x.share for x in plan.stop], dtype=np.float64)
    take_share = np.array([x.share for x in plan.take], dtype=np.float64)
    exit_share = np.array([x.share for x in plan.exit], dtype=np.float64)
    by = bars.partition_by("listing_id", as_dict=True)
    rows = []
    for (lid,), sg in signals.partition_by("listing_id", as_dict=True).items():
        b = by.get((lid,))
        m = sg.height
        if b is None or b.is_empty():   # no bars at all: never traded
            rows += [{"sid": x, "status": "halt", "fills": []} for x in sg["sid"].to_list()]
            continue
        n = b.height
        dates = b["date"].to_numpy()
        sd = sg["signal_date"].to_numpy()
        pos = np.searchsorted(dates, sd)
        hit = (pos < n) & (dates[np.minimum(pos, n - 1)] == sd)
        sbar = np.where(hit, pos, -1).astype(np.int64)
        flags = (np.column_stack([_col(b, f"exit{j}", n, np.bool_, False) for j in range(nexit)])
                 if nexit else np.zeros((n, 0), dtype=np.bool_))
        stops = (np.column_stack([_col(sg, f"stop{j}", m, np.float64, np.nan) for j in range(nstop)])
                 if nstop else np.zeros((m, 0)))
        takes = (np.column_stack([_col(sg, f"take{j}", m, np.float64, np.nan) for j in range(ntake)])
                 if ntake else np.zeros((m, 0)))
        out = {k: np.zeros(m, dtype=d) for k, d in (("st", np.int64), ("ebar", np.int64), ("epx", np.float64), ("eadj", np.float64),
                                                    ("nf", np.int64), ("noroom", np.int64), ("held", np.int64))}
        mfe, mae = np.full(m, np.nan), np.full(m, np.nan)
        fb, fp, fs, fw = (np.zeros((m, width), dtype=np.int64), np.zeros((m, width)), np.zeros((m, width)),
                          np.zeros((m, width), dtype=np.int64))
        fo = np.zeros((m, width), dtype=np.bool_)
        _kernel(_col(b, "open", n, np.float64, np.nan), _col(b, "high", n, np.float64, np.nan), _col(b, "low", n, np.float64, np.nan),
                _col(b, "close", n, np.float64, np.nan), _col(b, "adj", n, np.float64, 1.0), _col(b, "up", n, np.float64, np.nan),
                _col(b, "down", n, np.float64, np.nan), _col(b, "ul", n, np.bool_, False), _col(b, "ll", n, np.bool_, False),
                flags, bool(b["live"][0]), sbar, _col(sg, "next_ok", m, np.bool_, True), _col(sg, "entry_price", m, np.float64, np.nan),
                stops, takes, ENTRY[plan.entry],
                plan.fills == "close", stop_share, take_share, exit_share, plan.stall or 0, plan.days, t_plus_1, relative,
                out["st"], out["ebar"], out["epx"], out["eadj"], out["nf"], fb, fp, fs, fw, fo, mfe, mae, out["noroom"], out["held"])
        adj = _col(b, "adj", n, np.float64, 1.0)
        for i, sid in enumerate(sg["sid"].to_list()):
            status = STATUS[out["st"][i]]
            bought = status in ("done", "open", "gone") and not (status == "open" and out["epx"][i] == 0)
            fills = [{"date": dates[fb[i, j]].astype("datetime64[D]").item(), "price": float(fp[i, j]),
                      "adj": float(adj[fb[i, j]]), "share": float(fs[i, j]), "why": labels.get(int(fw[i, j]), "?"),
                      "at_open": bool(fo[i, j])} for j in range(out["nf"][i])]
            rows.append({"sid": sid, "status": status,
                         "entry_date": dates[out["ebar"][i]].astype("datetime64[D]").item() if bought else None,
                         "entry_price": float(out["epx"][i]) if bought else None, "entry_adj": float(out["eadj"][i]) if bought else None,
                         "held": int(out["held"][i]) if status in ("done", "gone") else None,
                         "mfe": None if np.isnan(mfe[i]) else float(mfe[i]), "mae": None if np.isnan(mae[i]) else float(mae[i]),
                         "reason": fills[-1]["why"] if fills and status in ("done", "gone") else None,
                         "no_room": int(out["noroom"][i]), "fills": fills})
    return pl.DataFrame(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)


def returns(trades: pl.DataFrame, bench: pl.DataFrame, costs, entry: str) -> pl.DataFrame:
    """Each bought and finished trade's return net of that day's costs — Σ share × sale price × (1 − its rate) ÷ (entry
    price × (1 + the buy's rate)) − 1, in adjusted prices (a dividend is in them); rates are commission, transfer fee and
    slippage each side, stamp duty on sales, as on each day (no minimum per order: no amounts here) — and the benchmark
    over the same times (bought at its close for `entry: close`, its open for `open`, its (open + high + low + close)
    ÷ 4 for `cross` — daily bars do not say when the line was reached; each sale at its open or close), `excess` the
    difference. `bench`: date, open, close (high, low for `cross`). Adds ret, bench_ret, excess, exit_date (null for the
    rest)."""
    done = trades.filter(pl.col("status").is_in(["done", "gone"]))
    if done.is_empty():
        return trades.with_columns(*[pl.lit(None, pl.Float64).alias(c) for c in ("ret", "bench_ret", "excess")],
                                   pl.lit(None, pl.Date).alias("exit_date"))
    f = done.select("sid", "entry_date", "entry_adj", "fills").explode("fills", empty_as_null=False).unnest("fills")
    when = sorted(set(f["date"].to_list()) | set(f["entry_date"].to_list()))
    rate = pl.DataFrame([{"date": d, "_buy": r.commission + r.transfer + r.slippage,
                          "_sell": r.commission + r.transfer + r.stamp + r.slippage} for d in when for r in [costs.on(d)]],
                        schema={"date": pl.Date, "_buy": pl.Float64, "_sell": pl.Float64})
    avg = ((pl.col("open") + pl.col("high") + pl.col("low") + pl.col("close")) / 4 if entry == "cross" else pl.col("close"))
    b = bench.select("date", pl.col("open").alias("_bo"), pl.col("close").alias("_bc"), avg.alias("_ba"))
    at0 = {"open": "_bo", "close": "_bc", "cross": "_ba"}[entry]
    f = (f.join(rate.select("date", "_sell"), on="date", how="left").join(b, on="date", how="left")
         .join(rate.select(pl.col("date").alias("entry_date"), "_buy"), on="entry_date", how="left")
         .join(b.select(pl.col("date").alias("entry_date"), pl.col(at0).alias("_b0")), on="entry_date", how="left")
         .with_columns(pl.when(pl.col("at_open")).then(pl.col("_bo")).otherwise(pl.col("_bc")).alias("_b1")))
    per = f.group_by("sid").agg(
        ((pl.col("share") * pl.col("price") * pl.col("adj") * (1 - pl.col("_sell"))).sum()
         / (pl.col("entry_adj").first() * (1 + pl.col("_buy").first())) - 1).alias("ret"),
        ((pl.col("share") * pl.col("_b1")).sum() / pl.col("_b0").first() - 1).alias("bench_ret"),
        pl.col("date").max().alias("exit_date"))
    return (trades.join(per, on="sid", how="left")
            .with_columns((pl.col("ret") - pl.col("bench_ret")).alias("excess")))
