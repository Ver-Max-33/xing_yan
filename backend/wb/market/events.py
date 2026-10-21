"""盘面变化 (design 2026-10-12 §4): what changed during the day, folded from the day's per-minute rows (board.tape_row)
alone — so the same rows always give the same changes, a restart loses and repeats none. Each change is held before
it counts (a few minutes on the new side) and coming back must go past a margin; an event carries the time it
happened, the row a crossing began."""
from datetime import datetime

CLOSE_BAND, LINE_BAND, HOLD = 0.001, 0.002, 3      # 上证 vs its close before; vs its 60-day line and box edges; rows held
FLIPS = 4                                          # 翻红 / 翻绿 a day at most
SEAL_EDGES, SEAL_MARGIN, SEAL_FROM, SEAL_TOUCHED = (0.6, 0.7, 0.8), 0.03, 15, 10
RESEAL = 5                                         # rows at the limit again for the top board's 回封
LEAD, LEAD_GAP = 0.003, 15                         # a new top industry: 0.3 points ahead; 15 rows since the last
LIMIT_DOWNS = (10, 30, 50)
GAP = 0.003                                        # an opening gap worth a line


def _at(r: dict) -> str:
    a = r["at"]
    return a.isoformat(timespec="seconds") if isinstance(a, datetime) else str(a)


def _ev(r: dict, kind: str, tone: str, title: str, detail: str) -> dict:
    return {"i": r["i"], "at": _at(r), "kind": kind, "tone": tone, "title": title, "detail": detail}


def events(rows: list[dict], index: dict | None, top_ids: tuple[str, ...], names: dict[str, str], top_level: int | None = None) -> list[dict]:
    """The day's changes, oldest first. `rows`: the day's tape rows by minute; `index`: 上证's facts (prev, ma60_prev,
    box); `top_ids`: yesterday's highest board; `names`: listing names; `top_level`: that board today."""
    rows = sorted(rows, key=lambda r: r["i"])
    out: list[dict] = []
    if rows and index:
        out += _open(rows, index) + _index_crossings(rows, index)
    out += _seal(rows) + _top(rows, top_ids, names, top_level) + _leader(rows) + _limit_downs(rows)
    return sorted(out, key=lambda e: e["i"])


def _open(rows: list[dict], index: dict) -> list[dict]:
    r = rows[0]
    if r["i"] > 2 or r.get("sh_last") is None or not index.get("prev"):
        return []
    gap = r["sh_last"] / index["prev"] - 1
    if abs(gap) < GAP:
        return []
    detail = ""
    if index.get("ma60_prev"):
        d = r["sh_last"] / index["ma60_prev"] - 1
        detail = f"在 60 日线{'上' if d >= 0 else '下'} {abs(d):.1%}"
    return [_ev(r, "open", "neutral", f"上证{'高' if gap > 0 else '低'}开 {abs(gap):.1%}", detail)]


def _crossings(rows: list[dict], level: float, band: float) -> list[tuple[dict, int]]:
    """(the row a crossing began, its side: 1 up through the level, −1 down) — past the band and held HOLD rows."""
    out, side, start, count = [], None, None, 0
    for r in rows:
        p = r.get("sh_last")
        if p is None:
            continue
        if side is None:
            side = 1 if p >= level else -1
            continue
        c = 1 if p >= level * (1 + band) else -1 if p <= level * (1 - band) else None
        if c is None or c == side:
            start, count = None, 0
            continue
        if start is not None and start[1] == c:
            count += 1
        else:
            start, count = (r, c), 1
        if count == HOLD:
            out.append(start)
            side, start, count = c, None, 0
    return out


def _index_crossings(rows: list[dict], index: dict) -> list[dict]:
    out = []
    if index.get("prev"):
        flips = [_ev(r, "close", "up" if c > 0 else "warn", "上证翻红" if c > 0 else "上证翻绿", f"昨收 {index['prev']:,.2f}")
                 for r, c in _crossings(rows, index["prev"], CLOSE_BAND)]
        out += flips[:FLIPS]
    if index.get("ma60_prev"):
        out += [_ev(r, "ma60", "up" if c > 0 else "warn", "上证站上 60 日线" if c > 0 else "上证跌破 60 日线", f"60 日线 {index['ma60_prev']:,.2f}")
                for r, c in _crossings(rows, index["ma60_prev"], LINE_BAND)]
    box = index.get("box")
    if box:
        out += [_ev(r, "box", "up" if c > 0 else "warn", "上证突破箱顶" if c > 0 else "上证回到箱体里", f"箱顶 {box['top']:,.2f}")
                for r, c in _crossings(rows, box["top"], LINE_BAND)]
        out += [_ev(r, "box", "up" if c > 0 else "warn", "上证回到箱体里" if c > 0 else "上证跌破箱底", f"箱底 {box['bottom']:,.2f}")
                for r, c in _crossings(rows, box["bottom"], LINE_BAND)]
    return out


def _band(rate: float) -> int:
    return sum(rate >= e for e in SEAL_EDGES)


def _seal(rows: list[dict]) -> list[dict]:
    """封板率 into another band (60 / 70 / 80%), past the band's edge by 3 points and held HOLD rows; from the 15th
    minute with 10 stocks or more that touched the limit."""
    out, band, hi, lo, cand = [], None, None, None, None
    for r in rows:
        rate, n = r.get("seal_rate"), (r.get("limit_up") or 0) + (r.get("broken") or 0)
        if rate is None or r["i"] < SEAL_FROM or n < SEAL_TOUCHED:
            continue
        if band is None:
            band, hi, lo = _band(rate), (rate, r), (rate, r)
            continue
        target = _band(rate)
        ok = (target < band and rate <= SEAL_EDGES[target] - SEAL_MARGIN) or (target > band and rate >= SEAL_EDGES[target - 1] + SEAL_MARGIN)
        if not ok:
            cand = None
        elif cand is not None and cand[1] == target:
            cand = (cand[0], target, cand[2] + 1)
        else:
            cand = (r, target, 1)
        if cand is not None and cand[2] == HOLD:
            first = cand[0]
            if target < band:
                ref, ref_r = hi
                out.append(_ev(first, "seal", "warn", f"封板率跌破 {SEAL_EDGES[target]:.0%}",
                               f"{_hm(ref_r)} 时 {ref:.0%}，炸板 {first.get('broken') or 0} 只"))
            else:
                ref, ref_r = lo
                out.append(_ev(first, "seal", "up", f"封板率回到 {SEAL_EDGES[target - 1]:.0%} 以上",
                               f"{_hm(ref_r)} 时 {ref:.0%}，涨停 {first.get('limit_up') or 0} 只"))
            band, hi, lo, cand = target, (rate, r), (rate, r), None
            continue
        if rate > hi[0]:
            hi = (rate, r)
        if rate < lo[0]:
            lo = (rate, r)
    return out


def _hm(r: dict) -> str:
    a = r["at"]
    return a.strftime("%H:%M") if isinstance(a, datetime) else str(a)[11:16]


def _top(rows: list[dict], top_ids: tuple[str, ...], names: dict[str, str], level: int | None) -> list[dict]:
    """Yesterday's highest board: the first time it opens from the limit today, and its 回封 — at the limit again for
    RESEAL rows running (the event at the row it resealed); each once."""
    out = []
    for lid in top_ids:
        name, opened, start, count = names.get(lid, lid), False, None, 0
        for r in rows:
            if not opened:
                if lid in (r.get("broken_ids") or []):
                    board = f"（{level} 板）" if level else ""
                    out.append(_ev(r, "top", "warn", f"最高板 {name}{board}打开涨停", "此刻不在涨停"))
                    opened = True
                continue
            if lid in (r.get("sealed_ids") or []):
                start, count = (start or r), count + 1
                if count == RESEAL:
                    out.append(_ev(start, "top", "up", f"最高板 {name} 回封", f"封住 {RESEAL} 分钟以上"))
                    break
            else:
                start, count = None, 0
    return out


def _leader(rows: list[dict]) -> list[dict]:
    """A new top industry: on top HOLD rows running, 0.3 points ahead of the second, and LEAD_GAP rows after the last
    such change (the day's first top is where it starts, not a change). By 申万一级, the page's heat grid (design
    2026-10-12-market-page-redesign §2.6); a row recorded before it kept the 一级 reads its own top."""
    out, leader, last, cand = [], None, None, None
    for r in rows:
        l1 = r.get("top1_name") is not None
        name = r.get("top1_name") if l1 else r.get("top_name")
        if name is None:
            continue
        if leader is None:
            leader = name
            continue
        top, second = (r.get("top1_chg"), r.get("second1_chg")) if l1 else (r.get("top_chg"), r.get("second_chg"))
        lead = (top or 0) - second if second is not None else 1.0
        if name == leader or lead < LEAD:
            cand = None
            continue
        cand = (cand[0], name, cand[2] + 1) if cand is not None and cand[1] == name else (r, name, 1)
        due = max(cand[0]["i"], last + LEAD_GAP if last is not None else cand[0]["i"])
        if cand[2] >= HOLD and r["i"] >= due:
            at_row = cand[0] if due == cand[0]["i"] else r
            out.append(_ev(at_row, "leader", "up", f"{name}升到领涨第一", f"{top or 0:+.1%}" + (f"，领先第二名 {lead * 100:.1f} 个点" if second is not None else "")))
            leader, last, cand = name, at_row["i"], None
    return out


def _limit_downs(rows: list[dict]) -> list[dict]:
    out, left = [], list(LIMIT_DOWNS)
    for r in rows:
        n = r.get("limit_down")
        while n is not None and left and n >= left[0]:
            t = left.pop(0)
            out.append(_ev(r, "limit_down", "warn", f"跌停增加到 {t} 只", f"此刻 {n} 只"))
    return out
