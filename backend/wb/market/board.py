"""市场看板的数 (design 2026-10-12 §3–§6) from one cross-section of every A share's day so far — a whole-market quote
during the session, or a stored day's bars after it — and the day's base (facts.Facts). Pure: the same inputs give
the same board. Limit-ups by the engine's own definitions (LIMIT_UP_CLOSE, LIMIT_UP_TOUCH, LIMIT_DOWN_CLOSE): a
price within 1e-6 of the exchange's limit price; a listing without a limit (a new one: Tushare's placeholder over
twice the close before) is never at it; one that has not traded today counts nowhere."""
import json
from datetime import date, datetime, time

import polars as pl

from wb.market.facts import Facts

EPS = 1e-6
HALF_FEN = 0.005   # prices are in fen: anything further beyond a limit price is not at it
SMALL = 3                        # an industry of this few members or fewer is not ranked against the big ones (§6.1)
SH = "CN:IDX:000001"
# 一直强 / 转强 / 转弱 / 一直弱 (design 2026-10-12-market-page-redesign §3.3): (last 5 bars, the 15 before them) against the
# market, by market and level — about three quarters of the industries' typical spread (the median over the 250
# trading days to 2026-10-09 of each day's std of industry median return less the market's), rounded to 0.5%;
# scripts/market/industry_spread.py measures it again
TREND = {("CN", "l1"): (0.015, 0.025), ("CN", "l2"): (0.02, 0.035), ("JP", "l1"): (0.01, 0.02)}
PROJECT_FROM = time(10, 30)      # the volume clock is unbiased from 10:30 (realtime/clock.py): no projection before
NAMES = 4                        # names shown per ladder level of three boards and more


def trend(rel5: float | None, prior15: float | None, t5: float, t15: float) -> str | None:
    """The tag (design 2026-10-12-market-page-redesign §3.1): the last 5 bars against the market, read with the 15
    bars before them — the two windows do not overlap, so strength that is only five days old is 转强, not 一直强."""
    if rel5 is None or prior15 is None:
        return None
    if rel5 >= t5 - 1e-12:
        return "一直强" if prior15 >= t15 - 1e-12 else "转强"
    if rel5 <= -t5 + 1e-12:
        return "一直弱" if prior15 <= -t15 + 1e-12 else "转弱"
    return None


def traded(quotes: pl.DataFrame, day: date) -> pl.DataFrame:
    """The listings that have traded on `day`: the quote's time is on it and its volume above 0 (an auction not
    matched, a suspended stock, a stamp of the day before are not)."""
    return quotes.filter((pl.col("at").dt.date() == day) & (pl.col("volume") > 0) & pl.col("last").is_not_null())


def possible(quotes: pl.DataFrame, facts: Facts, day: date) -> tuple[pl.DataFrame, int]:
    """The quote without its rows of `day` priced beyond the day's limit prices (an open, high, low or last above the
    limit up or below the limit down by more than half a fen): no such price is a trade — 10-13 at 09:25 the
    whole-market quote gave 1,750 沪主板, 创业板 and 科创板 stocks twice their auction's price for ten seconds, and the
    board read +96% industries and 1,750 limit-ups. With how many it left out. Without the day's limits (not fetched
    yet, 日股) nothing is judged; a listing without a limit (its placeholder limits) is never beyond them."""
    if facts.limits == "none" or quotes.is_empty():
        return quotes, 0
    q = quotes.join(facts.stocks.select("listing_id", "up_limit", "down_limit"), on="listing_id", how="left")
    hi, lo = pl.max_horizontal("open", "high", "low", "last"), pl.min_horizontal("open", "high", "low", "last")
    beyond = ((pl.col("at").dt.date() == day) & ((hi > pl.col("up_limit") + HALF_FEN) | (lo < pl.col("down_limit") - HALF_FEN))).fill_null(False)
    q = q.with_columns(beyond.alias("_beyond"))
    return q.filter(~pl.col("_beyond")).select(quotes.columns), int(q["_beyond"].sum())


def from_bars(bars: pl.DataFrame, day: date) -> pl.DataFrame:
    """A stored day's bars as the quote at its close would be (at 15:00; raw prices; the exchange's close before).
    Where the source gives no close before (日股), it is the last close in today's share units — close[t−1] ×
    adj_factor[t−1] / adj_factor[t], the chart's base price: pass the bars of the days before too."""
    at = datetime.combine(day, time(15, 0))
    b = bars.filter(pl.col("close").is_not_null() & (pl.col("date") <= day)).sort("listing_id", "date")
    if "adj_factor" in b.columns:
        adj = pl.col("adj_factor").forward_fill().over("listing_id").fill_null(1.0)
        before = pl.col("close").shift(1).over("listing_id") * adj.shift(1).over("listing_id") / adj
        b = b.with_columns(pl.coalesce("pre_close", before).alias("pre_close"))
    return (b.filter((pl.col("date") == day) & (pl.col("volume") > 0))
            .select("listing_id", pl.lit(at).alias("at"), "open", "high", "low", pl.col("close").alias("last"),
                    pl.col("pre_close").alias("prev_close"), "volume", "amount"))


def compute(xs: pl.DataFrame, facts: Facts, at: datetime, share: float, indexes_now: dict[str, float] | None = None,
            indexes_prev: dict[str, float] | None = None) -> dict:
    """The board (JSON-ready) of the traded cross-section `xs` (traded / from_bars) at `at`; `share`: the part of a
    day's turnover usually done by now (realtime/clock.share; 1 at the close); `indexes_now`: each index's price now;
    `indexes_prev`: its close before as the quote gives it (preferred to the data's, as for stocks)."""
    stale = facts.stale
    s = xs.join(facts.stocks.drop("prev"), on="listing_id", how="left").with_columns(
        (pl.col("last") / pl.col("prev_close") - 1).round(9).alias("chg"))
    known = facts.limits != "none" and s["up_limit"].drop_nulls().len() > 0
    has = pl.col("up_limit").is_not_null() & (pl.col("up_limit") <= 2 * pl.col("prev_close")) & pl.lit(known)
    sealed = has & (pl.col("last") >= pl.col("up_limit") - EPS)
    touched = has & ((pl.col("high") >= pl.col("up_limit") - EPS) | sealed)
    s = s.with_columns(sealed.alias("sealed"), (touched & ~sealed).alias("broken"),
                       (has & (pl.col("down_limit") > 0) & (pl.col("last") <= pl.col("down_limit") + EPS)).alias("ld"),
                       (pl.col("streak").fill_null(0) + 1).alias("level"))
    chg = pl.col("chg")
    n_sealed, n_broken, n_ld = (int(s[c].sum()) for c in ("sealed", "broken", "ld"))
    touch = s.filter(pl.col("sealed") | pl.col("broken"))
    pool = promoted = height = top_sealed = None
    ladder, sealed_ids, broken_ids = [], sorted(s.filter("sealed")["listing_id"].to_list()), sorted(s.filter("broken")["listing_id"].to_list())
    if known and not stale:
        pool = int(facts.stocks["lu_prev"].fill_null(False).sum())
        promoted = int(s.filter(pl.col("sealed") & pl.col("lu_prev").fill_null(False)).height)
        if touch.height:
            height = int(touch["level"].max())
            top_sealed = bool(touch.filter(pl.col("level") == height)["sealed"].any())
            ladder = _ladder(touch, height)
    limit_list = [{"listing_id": r["listing_id"], "name": r["name"], "level": None if stale else r["level"], "sealed": r["sealed"],
                   "chg": r["chg"]} for r in touch.sort(["level", "chg", "listing_id"], descending=[True, True, False]).iter_rows(named=True)]
    lim = {"known": known, "limit_up": n_sealed if known else None, "broken": n_broken if known else None,
           "limit_down": n_ld if known else None,
           "seal_rate": n_sealed / (n_sealed + n_broken) if known and n_sealed + n_broken else None,
           "pool": pool, "promoted": promoted, "promo_rate": promoted / pool if pool and pool >= 10 else None,
           "height": height, "top_sealed": top_sealed}
    industries = {lv: _industries(s, facts.stocks, lv, share, known, facts.market) for lv in ("l1", "l2")}
    level = "l2" if any(r["name"] for r in industries["l2"]) else "l1"
    ranked = [r for r in industries[level] if r["rank"] is not None]
    top = None
    if ranked:
        best = ranked[0]
        top = {"level": level, "name": best["name"], "chg": best["chg"], "up_ratio": best["up_ratio"],
               "second": ranked[1]["chg"] if len(ranked) > 1 else None}
    ranked1 = [r for r in industries["l1"] if r["rank"] is not None]   # the 一级 leader: 盘面变化 reads it (§2.6)
    top1 = ({"name": ranked1[0]["name"], "chg": ranked1[0]["chg"], "second": ranked1[1]["chg"] if len(ranked1) > 1 else None}
            if ranked1 else None)
    amount = float(s["amount"].sum())
    return {
        "at": at.isoformat(timespec="seconds"), "day": facts.day.isoformat(), "traded": s.height,
        "up": s.filter(chg > 0).height, "down": s.filter(chg < 0).height, "flat": s.filter(chg == 0).height,
        "amount": amount, "projected": amount / share if share and at.time() >= PROJECT_FROM else None,
        "limits": lim, "ladder": ladder, "limit_list": limit_list, "industries": industries, "top": top, "top1": top1,
        "indexes": _indexes(facts.indexes, indexes_now or {}, indexes_prev or {}), "sealed_ids": sealed_ids, "broken_ids": broken_ids}


def _ladder(touch: pl.DataFrame, height: int) -> list[dict]:
    """Every level from the highest down to the first board, an empty one too (where the ladder breaks): sealed and
    broken counts, and from three boards up the names (sealed first, then by today's change)."""
    out = []
    for lv in range(height, 0, -1):
        g = touch.filter(pl.col("level") == lv).sort(["sealed", "chg", "listing_id"], descending=[True, True, False])
        names = ([{"listing_id": r["listing_id"], "name": r["name"], "sealed": r["sealed"], "chg": r["chg"]}
                  for r in g.head(NAMES).iter_rows(named=True)] if lv >= 3 else [])
        out.append({"level": lv, "sealed": int(g["sealed"].sum()), "broken": int(g["broken"].sum()), "names": names,
                    "more": g.height - len(names) if lv >= 3 else 0})
    return out


def _industries(s: pl.DataFrame, stocks: pl.DataFrame, level: str, share: float, known: bool, market: str) -> list[dict]:
    """Each industry of `level` (l1 / l2): members (with a bar before today), traded, up, the median change, the share
    up, limit-ups, the amount against its members' 5-day average scaled to now; against the market — its median return
    less the whole traded market's — over the last 5 bars (`rel5`), the 15 before them (`prior15`) and the last 20
    (`rel20`), today's price in; its tag (§3 of the redesign; none for an industry of SMALL members or fewer, as it has
    no rank); its rank by today's change among the ranked; a 二级's 一级 (`parent`); and its three leaders (limit-ups
    first, then by today's change). The ranked by today's median change, the strongest first; then the small ones;
    unknown changes last."""
    now5 = (1 + pl.col("chg")) * pl.col("adj_last") / pl.col("c4") - 1
    now20 = (1 + pl.col("chg")) * pl.col("adj_last") / pl.col("c19") - 1
    p15 = pl.col("c4") / pl.col("c19") - 1
    m5, m20, mp15 = (s.select(e.median()).item() for e in (now5, now20, p15))
    today = (s.filter(pl.col(level).is_not_null()).group_by(level).agg(
        pl.len().alias("traded"), (pl.col("chg") > 0).sum().alias("up"), pl.col("chg").median().alias("chg"),
        pl.col("sealed").sum().alias("limit_up"), pl.col("amount").sum().alias("_amt"), pl.col("amount5").sum().alias("_amt5"),
        now20.median().alias("_r20"), now5.median().alias("_r5"), p15.median().alias("_p15")))
    before = stocks.filter(pl.col(level).is_not_null()).group_by(level).agg(pl.len().alias("members"))
    j = before.join(today, on=level, how="left").with_columns(pl.col("traded").fill_null(0), pl.col("up").fill_null(0))
    parents = {} if level == "l1" else dict(
        stocks.filter(pl.col("l2").is_not_null()).group_by("l2").agg(pl.col("l1").drop_nulls().first()).iter_rows())
    t5, t15 = TREND.get((market, level), TREND[("CN", level)])
    leaders: dict[str, list[dict]] = {}
    for (name,), g in (s.filter(pl.col(level).is_not_null()).sort(["sealed", "chg", "listing_id"], descending=[True, True, False])
                       .group_by(level, maintain_order=True)):
        leaders[name] = [{"listing_id": r["listing_id"], "name": r["name"], "chg": r["chg"], "sealed": r["sealed"]}
                         for r in g.head(3).iter_rows(named=True)]
    out = []
    for r in j.iter_rows(named=True):
        sub = lambda a, b: a - b if a is not None and b is not None else None   # noqa: E731
        small = r["members"] <= SMALL
        rel5, prior15 = sub(r["_r5"], m5), sub(r["_p15"], mp15)
        out.append({"name": r[level], "members": r["members"], "traded": r["traded"], "up": r["up"], "chg": r["chg"],
                    "up_ratio": r["up"] / r["traded"] if r["traded"] else None, "limit_up": (r["limit_up"] or 0) if known else None,
                    "amount_ratio": r["_amt"] / (r["_amt5"] * share) if r["_amt5"] and share else None,
                    "rel5": rel5, "prior15": prior15, "rel20": sub(r["_r20"], m20),
                    "tag": None if small else trend(rel5, prior15, t5, t15), "rank": None, "parent": parents.get(r[level]),
                    "small": small, "leaders": leaders.get(r[level], [])})
    out.sort(key=lambda r: (r["chg"] is None, r["small"], -(r["chg"] or 0), r["name"]))   # the ranked, then the small
    k = 0
    for r in out:
        if not r["small"] and r["chg"] is not None:
            k += 1
            r["rank"] = k
    return out


def _indexes(facts: dict, now: dict[str, float], prev_now: dict[str, float]) -> dict:
    """Each index with a price now: change (against the quote's close before, else the data's), distance to its 60-day
    line, its state (the INDEX_STATE rule on today's lines), and how far its box's top and bottom are."""
    out = {}
    for iid, f in facts.items():
        last = now.get(iid)
        if last is None:
            continue
        ma60 = (f["sum59"] + last) / 60 if f.get("sum59") is not None else None
        ma200 = (f["sum199"] + last) / 200 if f.get("sum199") is not None else None
        state = None
        if ma60 is not None and ma200 is not None and f.get("ma60_5") is not None:
            state = (1 if last > ma60 and last > ma200 and ma60 > f["ma60_5"]
                     else -1 if last < ma60 and last < ma200 and ma60 < f["ma60_5"] else 0)
        box = f.get("box")
        prev = prev_now.get(iid) or f.get("prev")
        out[iid] = {"last": last, "chg": last / prev - 1 if prev else None,
                    "dist60": last / ma60 - 1 if ma60 else None, "state": state,
                    "to_top": last / box["top"] - 1 if box else None, "to_bottom": last / box["bottom"] - 1 if box else None}
    return out


def tape_row(board: dict, i: int) -> dict:
    """One row of the per-minute record (§8.2): the whole market's numbers, the top industry (and the top 一级, which
    盘面变化 reads), 上证, the ladder and which stocks were at the limit and which had opened from it this minute."""
    lim, top, top1 = board["limits"], board["top"] or {}, board.get("top1") or {}
    sh = board["indexes"].get(SH) or {}
    return {"i": i, "at": datetime.fromisoformat(board["at"]), "traded": board["traded"], "up": board["up"], "down": board["down"],
            "flat": board["flat"], "amount": board["amount"], "limit_up": lim["limit_up"], "broken": lim["broken"],
            "limit_down": lim["limit_down"], "seal_rate": lim["seal_rate"], "pool": lim["pool"], "promoted": lim["promoted"],
            "promo_rate": lim["promo_rate"], "height": lim["height"], "top_sealed": lim["top_sealed"],
            "top_name": top.get("name"), "top_chg": top.get("chg"), "second_chg": top.get("second"), "sh_last": sh.get("last"),
            "top1_name": top1.get("name"), "top1_chg": top1.get("chg"), "second1_chg": top1.get("second"),
            "ladder": json.dumps([[r["level"], r["sealed"], r["broken"]] for r in board["ladder"]]),
            "sealed_ids": board["sealed_ids"], "broken_ids": board["broken_ids"]}
