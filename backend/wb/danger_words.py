"""危险提醒的文字 (design 2026-10-15 §4; plain words 10-17: the reason and the risk, said plainly):
each warning's name says the risk, its reason this stock's own numbers of the day, its risk what followed in ten years —
the numbers of the ledger docs/superpowers/ledgers/2026-10-15-danger-warnings.md (independently recomputed), against the
same day's stocks of the same float value, from the next day's open. History only — no trading words."""
from datetime import date

ORDER = ("unlock", "limit_up", "strong_burst", "weak_spike", "retail")
NAMES = {"unlock": "解禁前易走弱", "strong_burst": "高位放量易回落", "weak_spike": "弱势急拉易回落", "retail": "散户接盘易走弱"}
AFTER = "之后约三个月"     # 60 open days
PEERS = "比同样大小的股票"
USUAL = "（平时约一成）"   # the same day's same-size stocks 20% or more short: 8% / 10%
# boards (3: three or more) → 60 days' mean short (首板 −4.3 / −3.5, 二板 −6.9 / −5.1, 三板+ −9.6 / −6.6), the share 20%+ short
LIMIT = {1: ("这样收盘涨停的股票", "约 4%", "约两成"), 2: ("连续 2 天涨停的股票", " 5–7%", "约三分之一"),
         3: ("连续 3 天以上涨停的股票", " 7–10%", "四成多")}
# the ranked three: the 60 days' mean short (−3.4 / −4.3, −4.6 / −2.5, −4.8 / −3.3) and the share 20%+ short; two weaker since 2024
RANKED = {"strong_burst": ("这样的股票", " 3–4%", "约四分之一", ""),
          "weak_spike": ("这样急拉的股票", " 2–5%", "不到两成", "；2024 年以来这条变弱了"),
          "retail": ("这样的股票", " 3–5%", "约两成", "；2024 年以来这条变弱了")}
# unlock kind → what unlocks, the ledger's group, its last 20 open days' mean short (IPO −6.9 / −5.3, last 5 days −3.5 / −1.8;
# 定增 −1.6 / −3.9; listed 3 years −1.4 / −2.2; all ≥ 5% −2.6 / −3.0)
UNLOCK = {"ipo": ("首发原始股", "首发一年期解禁前的 20 个交易日", " 5–7%", "，最后一周最明显"),
          "placement": ("定增股份", "定增股份解禁前的 20 个交易日", " 2–4%", ""),
          "old": ("限售股", "上市 3 年以上的股票解禁前的 20 个交易日", " 1–2%", ""),
          "other": ("限售股", "其他 5% 以上的解禁前的 20 个交易日", "约 3%", "")}


def md(d: date) -> str:
    return f"{d:%m-%d}"


def label(kind: str, boards: int | None = None) -> str:
    if kind == "limit_up":
        return "涨停后易回落" if (boards or 1) == 1 else "连板后易回落"
    return NAMES[kind]


def _pct(x: float) -> str:
    """0.853 → 85%; the sign is said in words."""
    return f"{abs(x) * 100:.0f}%"


def _share(bp: float) -> str:
    """Basis points of the float value as a percent: 120 → 1.2%, 45 → 0.45%."""
    x = abs(bp) / 100
    return f"{x:.1f}%" if x >= 1 else f"{x:.2f}%"


def _ratio(x: float) -> str:
    return f"{x:.0f}%" if x >= 10 else f"{x:.1f}%"


def _reason(kind: str, day: date, boards: int | None, v: dict) -> tuple[str, str]:
    """(the reason, its short form for the briefing): the day's numbers, or the definition when they were not kept."""
    d, n = md(day), boards or 1
    if kind == "limit_up":
        return (f"{d} 收盘涨停。" if n == 1 else f"{d} 收盘涨停，已连续 {n} 天。"), f"{d} 收盘涨停" + ("" if n == 1 else f"（连续 {n} 天）")
    r20, r120, ma20, to5, mfs = (v.get(k) for k in ("r20", "r120", "ma20", "to5rel", "mfs20"))
    if kind == "strong_burst":
        if None in (r120, ma20, to5):
            return f"{d}：近半年的涨幅、高出 20 日均线的幅度和近 5 天的换手，三项都在全市场最高的一成里。", f"{d} 涨多了又放量"
        up = f"近半年{'涨了' if r120 >= 0 else '跌了'} {_pct(r120)}"
        return (f"{d}：{up}，股价{'高出' if ma20 >= 0 else '低于'} 20 日均线 {_pct(ma20)}，近 5 天的换手是近 3 个月平均的 {to5:.1f} 倍"
                "——三项都在全市场最高的一成里。"), f"{d} {up}、换手放大到 {to5:.1f} 倍"
    if kind == "weak_spike":
        if None in (r120, r20, to5):
            return f"{d}：近半年的涨幅在全市场偏弱的四成里，近 20 天的涨幅和近 5 天的换手却都在最高的一成里。", f"{d} 弱势股急拉放量"
        move = f"{'涨了' if r20 >= 0 else '跌了'} {_pct(r20)}"
        return (f"{d}：近半年{'跌了' if r120 < 0 else '只涨了'} {_pct(r120)}，在全市场偏弱的四成里；近 20 天却{move}，"
                f"近 5 天的换手是近 3 个月平均的 {to5:.1f} 倍（这两项都在最高的一成里）。"), f"{d} 弱势股近 20 天{move}、换手放大到 {to5:.1f} 倍"
    # retail: the 20 days' small-order net the move does not explain, top 1/20 — always a net buying (every flagged day of
    # 2021–2026: 3–15% of the float, the move −9…+35%); each trade counted on both sides: what small orders bought net, they bought from the others
    if None in (r20, mfs) or mfs <= 0:
        return f"到 {d} 的 20 天里，在涨跌差不多的股票里，它的小单（多是散户）净买入排在最多的 5%。", f"{d} 散户买得多"
    price = f"同期股价{'涨了' if r20 >= 0 else '跌了'} {_pct(r20)}"
    return (f"到 {d} 的 20 天里，小单（每笔 5 万元以下，多是散户）净买入相当于流通市值的 {_share(mfs)}，{price}；小单净买入的部分，"
            "是从中单、大单手里买来的。在涨跌差不多的股票里，它的散户买入排在最多的 5%。"), f"{d} 散户 20 天净买入占流通市值 {_share(mfs)}，{price}"


def event(kind: str, day: date, boards: int | None, days: int, numbers: dict | None = None) -> dict:
    """A limit-up close or the first day of a ranked warning, `days` open days ago, with the day's numbers (danger.NUMBERS)."""
    reason, short = _reason(kind, day, boards, numbers or {})
    if kind == "limit_up":
        who, avg, big = LIMIT[min(boards or 1, 3)]
        weaker = ""
    else:
        who, avg, big, weaker = RANKED[kind]
    risk = f"过去十年，{who}{AFTER}平均{PEERS}少涨{avg}，{big}少涨 20% 以上{USUAL}{weaker}。"
    return {"kind": kind, "label": label(kind, boards), "date": day.isoformat(), "days": days, "new": days == 0, "predicted": False,
            "reason": reason, "risk": risk, "brief": f"{label(kind, boards)}：{short}；过去十年{who}{AFTER}平均少涨{avg}"}


def unlock(r: dict, new: bool) -> dict:
    """An unlock window's row (danger.unlock_windows): its day, open days left, ratio, kind, predicted or announced."""
    what, group, avg, last = UNLOCK[r["kind"]]
    day, left = r["unlock_day"], r["left"]
    if r["predicted"]:
        when = f"还有约 {left} 个交易日" if left > 0 else "按推算是今天" if left == 0 else "推算的日子已过"
        reason = f"约 {md(day)} 上市满一年，首发原始股通常这时解除限售（按上市日推算，还没公告；{when}）。"
        short = f"约 {md(day)} 首发原始股解禁（按上市日推算）"
    else:
        when = f"还有 {left} 个交易日" if left > 0 else "就是今天"
        reason = f"{md(day)} 有占总股本 {_ratio(r['ratio'])} 的{what}解除限售（{when}）。"
        short = f"{md(day)} {what}解禁 {_ratio(r['ratio'])}（{when}）"
    risk = f"解禁后这些股份可以上市交易。过去十年，{group}，股价平均{PEERS}少涨{avg}{last}。"
    return {"kind": "unlock", "label": NAMES["unlock"], "date": day.isoformat(), "days": left, "new": new,
            "predicted": bool(r["predicted"]), "reason": reason, "risk": risk,
            "brief": f"{NAMES['unlock']}：{short}；过去十年{group}平均少涨{avg}"}
