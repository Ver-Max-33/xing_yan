"""组合回测 (spec §9.2): a long-only book of one market. A signal is bought at the open of the market's first trading
day after it — never at the signal day's close — equal weight (the last close's NAV ÷ the positions allowed), in
whole lots, in the rule's order when more signals come than free places. Prices are the exchange's own (raw); a
dividend or a split on a held stock is booked when its factors change (shares × the share factor's change), so
adjusted prices are never traded and nothing is counted twice. The cash is what the market's dividend records pay
(CN: cash a share, before tax) — a rights issue moves the adjust factor too, and a holder who does not subscribe is
paid nothing, so what the records do not explain is a row of its own and counted, never cash; a market without
records (JP: its factors carry splits only) is paid what the factors imply.

Exits (any of them): `hold` — sold at the close of the hold-th day (the entry day is day 1); `box` — a close below
the signal's box line (its bottom × (1 − tol), adjusted, judged like BOX's break_down: a wick that closes back
inside is no break) sells at the next open; `trail` — an intraday touch of the highest close since entry less `trail`;
`target` — an intraday touch of the entry price plus `target`; `own` — the rule's own 失效线 in place of the box line
(fail) and its 目标位 in place of the fixed target (spec §9.1); `stall` — that many of its own trading days closing
without a new high since entry sells at the next open (有新高再看高，直到没有高, read on the daily bars for
research). An open beyond the stop or the target fills at the open
(a gap is never filled at the stop price); a bar touching both the trailing stop and the target takes the stop (the
worse order) and is counted.

Market rules: CN never sells on the day it bought (T+1); a buy at or beyond the day's limit-up price (stk_limit) does
not fill and is dropped — not chased at a later open, as the event study counts it — and a sale at or beyond the
limit down waits for the next open. JP has no T+1; the limit flags are only known for the day as a whole, so a buy at
an open at the high of a UL day, or a sale at the low of an LL day, does not fill (an approximation the report says).
A day the stock does not trade: nothing is bought, exits wait.

Participation (随大势, simplified for research): on the days after the benchmark closed below its
200-day line (`weak`), new places stop at `weak_positions`; what is held is kept.

Costs (each market's own, configurable): commission (with a minimum per order), transfer fee, stamp duty on sales,
and slippage against you — each a ledger row of its own, apart from the trade at the market price. The NAV is
cash plus each holding at its last close, every trading day of the market including those with nothing held."""
import math
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import polars as pl

EPS = 1e-9
AGREE = 0.002   # the factors' rounding: a dividend record and the factor's change agree within this share of the price


RATES = ("commission", "min_commission", "transfer", "stamp", "slippage")


@dataclass(frozen=True)
class Costs:
    commission: float = 0.0       # of the value, each side
    min_commission: float = 0.0   # per order, when a commission is charged
    transfer: float = 0.0         # of the value, each side (CN 过户费)
    stamp: float = 0.0            # of the value, sales only (CN 印花税)
    slippage: float = 0.0         # of the price, against you, each side
    history: tuple[tuple[date, str, float], ...] = ()   # (until, rate, value): what a rate was on the days before `until`

    def scaled(self, k: float) -> "Costs":
        return Costs(*(getattr(self, r) * k for r in RATES), history=tuple((u, r, v * k) for u, r, v in self.history))

    def on(self, d: date) -> "Costs":
        """The rates of day `d`: each the value of its first change after `d`, or today's."""
        past = {r: v for u, r, v in sorted(self.history, reverse=True) if d < u}   # the earliest change after d is set last
        return replace(self, **past) if past else self


# each market's defaults (configurable per run): CN 佣金万 2.5（最低 5 元）、过户费十万分之一、印花税卖出万 5；
# JP 主流网络券商佣金为 0；两边滑点千分之一. CN's rates before they last changed (the bars start in 2013): 印花税卖出千分之一
# until 2023-08-27 (halved from 2023-08-28), 过户费万分之 0.2 until 2022-04-28 (halved from 2022-04-29). Before 2015-08-01
# 沪市 charged 0.3‰ of the face value and 深市 0.0255‰, and 北交所 0.025‰ until 2022-04-28: within a yuan or two a trade of
# the 0.02‰ that stands for them
COSTS = {"CN": Costs(commission=0.00025, min_commission=5.0, transfer=0.00001, stamp=0.0005, slippage=0.001,
                     history=((date(2023, 8, 28), "stamp", 0.001), (date(2022, 4, 29), "transfer", 0.00002))),
         "JP": Costs(slippage=0.001)}
CASH = {"CN": 1_000_000.0, "JP": 10_000_000.0}


@dataclass(frozen=True)
class Settings:
    cash: float
    costs: Costs = field(default_factory=Costs)
    max_positions: int = 10
    hold: int | None = 20       # sold at the close of the hold-th day; None: no time exit
    box_stop: bool = True       # a close below the signal's box line
    trail: float | None = None  # below the highest close since entry by this share
    target: float | None = None  # above the entry price by this share
    lot: int = 100
    t_plus_1: bool = False      # CN
    stall: int | None = None    # sold at the next open after this many of its days without a new highest close
    weak_positions: int | None = None   # the places allowed the day after the benchmark closed below its 200-day line
    own: bool = False           # the rule's own 失效线 / 目标位 (each signal's stop / target_px) in place of the box line and
                                # the fixed target (spec §9.1 规则自己的判断)

    def with_costs(self, k: float) -> "Settings":
        return replace(self, costs=self.costs.scaled(k))


@dataclass
class _Pos:
    lid: str
    signal_date: date
    entry_date: date
    k0: int              # the entry day's index in the market's days
    entry_price: float
    shares: float
    first_shares: float
    cost: float          # what went out for it: the price, its slippage and fees
    adj: float           # the factors of the last bar seen: a change is a dividend or a split
    share: float
    last_close: float
    entry_adj: float     # the entry price, adjusted
    high_adj: float      # the highest close since entry, adjusted (the entry price before the first close)
    line_adj: float | None
    income: float = 0.0  # dividends received
    pending: str | None = None  # an exit waiting for the next open the stock can be sold at
    seen: int = 0        # the bar up to which its dividend records are paid (the entry day's: bought ex-dividend)
    quiet: int = 0       # its trading days since the last close above the highest one since entry
    target_adj: float | None = None   # the rule's own 目标位, adjusted (`own`)


class _Bars:
    """One stock's bars as arrays, found by date."""

    def __init__(self, df: pl.DataFrame):
        self.dates = df["date"].to_numpy()
        self.o, self.h, self.l, self.c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
        # a factor missing before its first value takes that value: arriving late is no corporate action
        self.adj = df["adj_factor"].fill_null(strategy="forward").fill_null(strategy="backward").fill_null(1.0).to_numpy()
        self.share = df["share_factor"].fill_null(strategy="forward").fill_null(strategy="backward").fill_null(1.0).to_numpy()
        # the cash a share the dividend records pay, summed up to each bar (None: the market has no records)
        self.paid = np.cumsum(df["cash_div"].fill_null(0.0).to_numpy()) if "cash_div" in df.columns else None
        self.up = df["up_limit"].to_numpy() if "up_limit" in df.columns else np.full(len(df), np.nan)
        self.dn = df["down_limit"].to_numpy() if "down_limit" in df.columns else np.full(len(df), np.nan)
        self.ul = df["ul"].fill_null(False).to_numpy() if "ul" in df.columns else np.zeros(len(df), bool)
        self.ll = df["ll"].fill_null(False).to_numpy() if "ll" in df.columns else np.zeros(len(df), bool)

    def at(self, d: np.datetime64) -> int | None:
        i = int(np.searchsorted(self.dates, d))
        if i < len(self.dates) and self.dates[i] == d and not (math.isnan(self.o[i]) or math.isnan(self.c[i])):
            return i
        return None


def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def prepare(bars: pl.DataFrame) -> dict[str, _Bars]:
    """`bars` by listing as arrays, for one simulation or many (a heatmap's variants share them)."""
    return {k[0]: _Bars(g.sort("date")) for k, g in bars.partition_by("listing_id", as_dict=True).items()}


def simulate(signals: pl.DataFrame, bars: pl.DataFrame | dict, days: list[date], s: Settings,
             weak: set[date] | frozenset = frozenset()) -> dict:
    """`signals`: listing_id, signal_date, stop (the box line, adjusted; null: none), rank (lower first; null last).
    `bars`: listing_id, date, open, high, low, close, adj_factor, share_factor, up_limit, down_limit, ul, ll (raw), or
    them `prepare`d. `days`: the market's trading days to run over; `weak`: those after the benchmark closed below its
    200-day line. Returns nav, trades, ledger and counts."""
    books = bars if isinstance(bars, dict) else prepare(bars)
    day_index = {d: k for k, d in enumerate(days)}
    entering: dict[int, list[dict]] = {}
    clock = np.array(days, dtype="datetime64[D]")
    for r in signals.sort("signal_date", "listing_id").iter_rows(named=True):
        k = int(np.searchsorted(clock, np.datetime64(r["signal_date"]), side="right"))
        if k < len(days):  # the first trading day after the signal; none left: never entered
            entering.setdefault(k, []).append(r)
    cash, nav_prev = s.cash, s.cash
    held: dict[str, _Pos] = {}
    trades, ledger, nav_rows = [], [], []
    counts = {"blocked_buy": {}, "blocked_sell": {}, "skipped": {}, "same_bar": 0, "gap": 0, "corporate": 0, "unexplained": 0}
    c = s.costs

    def book(d, lid, kind, amount, shares=None, price=None, note=None):
        ledger.append((d, lid, kind, shares, price, amount, note))

    def fee(value: float, sale: bool, d: date) -> float:
        r = c.on(d)
        com = max(value * r.commission, r.min_commission) if r.commission > 0 else 0.0
        return com + value * r.transfer + (value * r.stamp if sale else 0.0)

    def blocked_buy(b: _Bars, i: int, price: float) -> bool:
        return (not math.isnan(b.up[i]) and price >= b.up[i] - EPS) or (bool(b.ul[i]) and price >= b.h[i] - EPS)

    def blocked_sell(b: _Bars, i: int, price: float) -> bool:
        return (not math.isnan(b.dn[i]) and price <= b.dn[i] + EPS) or (bool(b.ll[i]) and price <= b.l[i] + EPS)

    def sell(p: _Pos, d: date, price: float, reason: str):
        nonlocal cash
        value = p.shares * price
        slip, f = value * c.slippage, fee(value, True, d)
        cash += value - slip - f
        book(d, p.lid, "sell", value, -p.shares, price, reason)
        if slip:
            book(d, p.lid, "slippage", -slip)
        if f:
            book(d, p.lid, "fee", -f)
        pnl = value - slip - f + p.income - p.cost
        trades.append((p.lid, p.signal_date, p.entry_date, p.entry_price, p.first_shares, d, price, reason,
                       pnl, pnl / p.cost if p.cost else None, day_index[d] - p.k0 + 1))
        del held[p.lid]

    def try_sell(p: _Pos, b: _Bars, i: int, d: date, price: float, reason: str):
        if blocked_sell(b, i, price):
            counts["blocked_sell"]["limit"] = counts["blocked_sell"].get("limit", 0) + 1
            p.pending = reason
        else:
            sell(p, d, price, reason)

    for k, d in enumerate(days):
        nd = np.datetime64(d)
        # 1. dividends and splits since the last bar seen (a factor that changed on a day the stock did not trade
        #    is booked on the next day it trades)
        for p in list(held.values()):
            b = books[p.lid]
            i = b.at(nd)
            if i is None or (abs(b.adj[i] - p.adj) < EPS * p.adj and abs(b.share[i] - p.share) < EPS * p.share):
                continue
            ratio = b.share[i] / p.share
            implied = p.last_close * (1 - ratio * p.adj / b.adj[i])   # per old share: hfq continuous across the ex-date
            counts["corporate"] += 1
            if b.paid is not None:   # what the records pay since; never more than the factor moved
                div = max(0.0, min(float(b.paid[i] - b.paid[p.seen]), implied))
            else:
                div = max(implied, 0.0)
            if div > 1e-6 * p.last_close:
                cash += p.shares * div
                p.income += p.shares * div
                book(d, p.lid, "dividend", p.shares * div, p.shares, div)
            if abs(implied - div) > AGREE * p.last_close:   # a rights issue, a record missing: no cash invented
                counts["unexplained"] += 1
                book(d, p.lid, "unexplained", 0.0, p.shares, implied - div,
                     "复权因子的变化和分红记录对不上（配股、缺了记录的送转等），差额没有记现金")
            p.seen = i
            if abs(ratio - 1) > EPS:
                more = p.shares * (ratio - 1)
                book(d, p.lid, "split", 0.0, more, None, f"每股变 {ratio:g} 股")
                p.shares += more
            p.adj, p.share = b.adj[i], b.share[i]
        # 2. at the open: exits that were waiting, then an open beyond a stop or a target
        for lid in sorted(held):
            p = held[lid]
            b = books[lid]
            i = b.at(nd)
            if i is None or (s.t_plus_1 and p.entry_date >= d):
                continue
            if p.pending:
                try_sell(p, b, i, d, b.o[i], p.pending)
                continue
            stop, why = _stop(p, b.adj[i], s)
            if stop is not None and b.o[i] <= stop + EPS:
                counts["gap"] += 1
                try_sell(p, b, i, d, b.o[i], why)
            elif (tgt := _target(p, b.adj[i], s)) is not None and b.o[i] >= tgt - EPS:
                counts["gap"] += 1
                sell(p, d, b.o[i], "target")
        # 3. entries at the open, in the rule's order, while there is room
        for r in sorted(entering.get(k, []), key=lambda r: (r["rank"] is None, r["rank"] or 0, r["listing_id"])):
            lid = r["listing_id"]
            if lid in held:
                counts["skipped"]["held"] = counts["skipped"].get("held", 0) + 1
                continue
            b = books.get(lid)
            i = b.at(nd) if b is not None else None
            if i is None:
                counts["blocked_buy"]["halt"] = counts["blocked_buy"].get("halt", 0) + 1
                continue
            price = float(b.o[i])
            if blocked_buy(b, i, price):
                counts["blocked_buy"]["limit"] = counts["blocked_buy"].get("limit", 0) + 1
                continue
            cap = s.weak_positions if s.weak_positions is not None and d in weak else s.max_positions
            if len(held) >= cap:
                why = "capacity" if len(held) >= s.max_positions else "weak"
                counts["skipped"][why] = counts["skipped"].get(why, 0) + 1
                continue
            budget = min(nav_prev / s.max_positions, cash)
            shares = math.floor(budget / (price * (1 + c.slippage) * s.lot)) * s.lot
            while shares > 0 and shares * price * (1 + c.slippage) + fee(shares * price, False, d) > cash + EPS:
                shares -= s.lot
            if shares <= 0:
                counts["skipped"]["unaffordable"] = counts["skipped"].get("unaffordable", 0) + 1
                continue
            value = shares * price
            slip, f = value * c.slippage, fee(value, False, d)
            cash -= value + slip + f
            book(d, lid, "buy", -value, shares, price, None)
            if slip:
                book(d, lid, "slippage", -slip)
            if f:
                book(d, lid, "fee", -f)
            stop = _num(r.get("stop")) if s.box_stop or s.own else None
            held[lid] = _Pos(lid, r["signal_date"], d, k, price, float(shares), float(shares), value + slip + f, b.adj[i], b.share[i],
                             price, price * b.adj[i], price * b.adj[i], stop, seen=i,
                             target_adj=_num(r.get("target_px")) if s.own else None)
        # 4. during the day: the trailing stop and the target (a bar touching both takes the stop)
        for lid in sorted(held):
            p = held[lid]
            b = books[lid]
            i = b.at(nd)
            if i is None or p.pending or (s.t_plus_1 and p.entry_date >= d):
                continue
            stop = p.high_adj * (1 - s.trail) / b.adj[i] if s.trail is not None else None
            target = _target(p, b.adj[i], s)
            hit_stop = stop is not None and b.l[i] <= stop + EPS and b.o[i] > stop + EPS
            hit_target = target is not None and b.h[i] >= target - EPS and b.o[i] < target - EPS
            if hit_stop and hit_target:
                counts["same_bar"] += 1
            if hit_stop:
                try_sell(p, b, i, d, stop, "trail")
            elif hit_target:
                sell(p, d, target, "target")
        # 5. at the close: the box line, then the holding period (a day the stock does not trade, or CN's entry day:
        #    sold at the next open it can be); the day's close sets the trailing high
        for lid in sorted(held):
            p = held[lid]
            b = books[lid]
            i = b.at(nd)
            if i is not None:
                p.last_close = float(b.c[i])
                if b.c[i] * b.adj[i] > p.high_adj:
                    p.high_adj, p.quiet = b.c[i] * b.adj[i], 0
                else:
                    p.quiet += 1
            if p.pending:
                continue
            if i is not None and p.line_adj is not None and b.c[i] * b.adj[i] < p.line_adj - EPS:
                p.pending = "fail" if s.own else "box"   # known at the close: sold at the next open
            elif s.hold is not None and k - p.k0 + 1 >= s.hold:
                if i is None or (s.t_plus_1 and p.entry_date >= d):
                    p.pending = "hold"
                else:
                    try_sell(p, b, i, d, float(b.c[i]), "hold")
            elif s.stall is not None and i is not None and p.quiet >= s.stall:
                p.pending = "stall"   # known at the close: sold at the next open
        holdings = sum(p.shares * p.last_close for p in held.values())
        nav_prev = cash + holdings
        nav_rows.append((d, cash, holdings, nav_prev, len(held)))
    for p in held.values():   # still held at the end: marked to market, no sale invented
        trades.append((p.lid, p.signal_date, p.entry_date, p.entry_price, p.first_shares, None, None, None,
                       p.shares * p.last_close + p.income - p.cost, None, None))
    return {
        "nav": pl.DataFrame(nav_rows, schema={"date": pl.Date, "cash": pl.Float64, "holdings": pl.Float64, "nav": pl.Float64,
                                              "positions": pl.Int64}, orient="row"),
        "trades": pl.DataFrame(trades, schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "entry_date": pl.Date,
                                               "entry_price": pl.Float64, "shares": pl.Float64, "exit_date": pl.Date,
                                               "exit_price": pl.Float64, "reason": pl.Utf8, "pnl": pl.Float64, "ret": pl.Float64,
                                               "days": pl.Int64}, orient="row"),
        "ledger": pl.DataFrame(ledger, schema={"date": pl.Date, "listing_id": pl.Utf8, "kind": pl.Utf8, "shares": pl.Float64,
                                               "price": pl.Float64, "amount": pl.Float64, "note": pl.Utf8}, orient="row"),
        "counts": counts,
    }


def _target(p: _Pos, adj: float, s: Settings) -> float | None:
    """The target in today's price: the rule's own (`own`), else the entry price plus `target`."""
    if s.own:
        return p.target_adj / adj if p.target_adj is not None and p.target_adj > p.entry_adj else None   # no room: none
    return p.entry_adj * (1 + s.target) / adj if s.target is not None else None


def _stop(p: _Pos, adj: float, s: Settings) -> tuple[float | None, str]:
    """The trailing stop in today's price (the box line is judged on the close, not here)."""
    if s.trail is None:
        return None, "trail"
    return p.high_adj * (1 - s.trail) / adj, "trail"
