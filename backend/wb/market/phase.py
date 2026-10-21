"""情绪阶段 (design 2026-10-12 §5.2): each trading day's limit-up ladder from the stored daily bars — first boards, two
boards and more, the highest board, 晋级率 (yesterday's limit-ups at the limit today), 封板率, limit-downs — and the
phase it is in; and today's read against the same minute of yesterday (§5.3). Descriptive only: over 2020-08-24 …
2026-10-09 the next day of a phase's limit-up stocks was the same in every phase (design §1.5)."""
from datetime import date, timedelta

import polars as pl

from wb.data.store import Snapshot, Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame, index_states
from wb.engine.l1.run import evaluate

ANCHOR = date(2020, 8, 24)   # 创业板's 20% limits from this day: the thresholds are percentiles of the days from it
SH = "CN:IDX:000001"
ALPHA = 1 / 3                # the smoothing: an exponential average of about five days
HOT = ("启动", "主升", "高潮")
# the percentiles of 2020-08-24 … 2026-10-09 (1,483 trading days, design §1.4) each threshold is
T = dict(climax_ge2=62, climax_first=218,                      # p90 × 2, p90 × 2.5
         rally_height=8, rally_ge2=19, rally_promo=0.270,      # p60
         rally2_promo=0.346, rally2_height=7, rally2_ge2=16,   # p85; p50
         ice_height=4, ice_ge2=8, ice_first=26,                # p10
         ebb_promo=0.181, ebb_was_ge2=14, ebb_was_height=6,    # p20; p40
         ebb2_promo=0.154, ebb2_seal=0.565,                    # p10
         ebb3_down=39, ebb3_promo=0.246,                       # p85 (smoothed); p50
         ignite_promo=0.204, ignite_ge2=8, ignite_height=5,    # p30; p10; p20
         crash_down=50)                                        # p90, the day's own
FORMULAS = {"limit_up": "LIMIT_UP_CLOSE", "touched": "LIMIT_UP_TOUCH", "limit_down": "LIMIT_DOWN_CLOSE",
            "streak": "LIMIT_UP_STREAK", "chg": "C / REF(C, 1) - 1", "high60": "NEW_HIGH(60)", "low60": "C < REF(LLV(L, 60), 1)"}
SMOOTHED = ("ge2", "first", "height", "promo_rate", "seal_rate", "limit_down")


def ladder_days(store: Store, profile: str, snap: Snapshot, start: date = ANCHOR) -> pl.DataFrame:
    """One row a trading day from `start`: date, n (listings with a bar), up, down, limit_up, touched, limit_down,
    first, ge2, height, pool (listings whose bar before closed at the limit), promoted (those of them at the limit
    today), seal_rate, promo_rate (null under a pool of 10), high60, low60, idx_state (上证's INDEX_STATE). The limit
    columns are null on a day no listing has limit prices (a test profile): unknown, never 0."""
    comp = Compiler("CN", available=available_columns(store, snap))
    for name, formula in FORMULAS.items():
        comp.add(name, formula)
    prog = comp.program()
    frame = build_frame(store, "CN", profile, prog.columns, start=start - timedelta(days=90), snap=snap)
    calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    v = (evaluate(frame, prog, calendar).sort("listing_id", "date")
         .with_columns(pl.col("limit_up").shift(1).over("listing_id").alias("lu_prev")))
    known = pl.col("limit_up").is_not_null().any()
    count = lambda e: pl.when(known).then(e.sum().cast(pl.Int64))   # noqa: E731
    d = (v.filter(pl.col("date") >= start).group_by("date").agg(
            pl.len().cast(pl.Int64).alias("n"), (pl.col("chg") > 0).sum().cast(pl.Int64).alias("up"),
            (pl.col("chg") < 0).sum().cast(pl.Int64).alias("down"), count(pl.col("limit_up")).alias("limit_up"),
            count(pl.col("touched")).alias("touched"), count(pl.col("limit_down")).alias("limit_down"),
            count(pl.col("streak") == 1).alias("first"), count(pl.col("streak") >= 2).alias("ge2"),
            pl.when(known).then(pl.col("streak").max().cast(pl.Int64)).alias("height"),
            count(pl.col("lu_prev")).alias("pool"), count(pl.col("lu_prev") & pl.col("limit_up")).alias("promoted"),
            pl.col("high60").sum().cast(pl.Int64).alias("high60"), pl.col("low60").sum().cast(pl.Int64).alias("low60"))
         .sort("date")
         .with_columns((pl.col("limit_up") / pl.col("touched")).alias("seal_rate"),
                       pl.when(pl.col("pool") >= 10).then(pl.col("promoted") / pl.col("pool")).alias("promo_rate")))
    idx = store.scan("index_bars_1d", snap).filter(pl.col("index_id") == SH).select("index_id", "date", "close").collect()
    states = (index_states(idx).select("date", "idx_state") if idx.height
              else pl.DataFrame(schema={"date": pl.Date, "idx_state": pl.Int8}))
    return d.join(states, on="date", how="left")


def classify(days: pl.DataFrame) -> pl.DataFrame:
    """`days` with raw (the day's own label: 急跌, 高潮, 主升, 冰点, 退潮, 启动 or 平稳) and phase (the confirmed one:
    冰点, 启动, 主升, 高潮, 退潮, 平稳 or 修复), both null when the limit columns are. The rules and their order are
    design §5.2; a falling 上证 takes 启动, 主升 and 高潮 down to 平稳; a new label counts on its second day running,
    a crash (急跌) at once — 退潮 after 主升 or 高潮, else 冰点; a confirmed 平稳 after 冰点 or 退潮 is 修复 until
    another phase is confirmed."""
    if days.is_empty() or days["limit_up"].null_count() == days.height:
        return days.with_columns(pl.lit(None, pl.Utf8).alias("raw"), pl.lit(None, pl.Utf8).alias("phase"))
    e = days.with_columns([pl.col(c).cast(pl.Float64).forward_fill().ewm_mean(alpha=ALPHA, adjust=False).alias(f"e_{c}")
                           for c in SMOOTHED])
    e = e.with_columns(pl.col("e_ge2").shift(5).alias("g5"), pl.col("e_height").shift(5).alias("h5"))
    g, f, h, p, s, ld = (pl.col(f"e_{c}") for c in SMOOTHED)
    crash = (pl.col("limit_down") >= T["crash_down"]) & (pl.col("limit_down") > 2 * pl.col("limit_up"))
    climax = (g >= T["climax_ge2"]) | (f >= T["climax_first"])
    rally = (((h >= T["rally_height"]) & (g >= T["rally_ge2"]) & (p >= T["rally_promo"]))
             | ((p >= T["rally2_promo"]) & (h >= T["rally2_height"]) & (g >= T["rally2_ge2"])))
    ice = (h <= T["ice_height"]) & (g <= T["ice_ge2"]) & (f <= T["ice_first"])
    ebb = (((p < T["ebb_promo"]) & ((pl.col("g5") > T["ebb_was_ge2"]) | (pl.col("h5") > T["ebb_was_height"])) & (g < pl.col("g5")))
           | ((p < T["ebb2_promo"]) & (s < T["ebb2_seal"])) | ((ld >= T["ebb3_down"]) & (p < T["ebb3_promo"])))
    ignite = ((((g - pl.col("g5")) >= 3) & (g >= T["ignite_ge2"])) | ((h > pl.col("h5")) & (h >= T["ignite_height"]))) \
        & (p >= T["ignite_promo"])
    raw = (pl.when(crash.fill_null(False)).then(pl.lit("急跌")).when(climax.fill_null(False)).then(pl.lit("高潮"))
           .when(rally.fill_null(False)).then(pl.lit("主升")).when(ice.fill_null(False)).then(pl.lit("冰点"))
           .when(ebb.fill_null(False)).then(pl.lit("退潮")).when(ignite.fill_null(False)).then(pl.lit("启动"))
           .otherwise(pl.lit("平稳")))
    e = e.with_columns(raw.alias("raw"))
    e = e.with_columns(pl.when((pl.col("idx_state") == -1) & pl.col("raw").is_in(HOT)).then(pl.lit("平稳"))
                       .otherwise(pl.col("raw")).alias("raw"))
    out, cur, pending, last_real = [], None, None, None
    for r in e["raw"].to_list():
        if r == "急跌":
            cur, pending = ("退潮" if cur in ("主升", "高潮") else "冰点"), None
        elif cur is None:
            cur = r
        elif r != cur:
            cur, pending = (r, None) if pending == r else (cur, r)
        else:
            pending = None
        if cur != "平稳":
            last_real = cur
        out.append("修复" if cur == "平稳" and last_real in ("冰点", "退潮") else cur)
    return days.with_columns(e["raw"], pl.Series("phase", out, dtype=pl.Utf8))


def summary(days: pl.DataFrame) -> dict | None:
    """The phase now for the page: as_of (its last day), phase, days (how many days running), pending (the last
    day's own label when it differs: a change that may come), path (the last three spans) and recent (the last 60
    days: phase, limit-ups, the share up, new highs less new lows). None when the phase is unknown."""
    rows = days.drop_nulls("phase") if "phase" in days.columns else days.head(0)
    if rows.is_empty():
        return None
    spans: list[list] = []
    for p in rows["phase"].to_list():
        if spans and spans[-1][0] == p:
            spans[-1][1] += 1
        else:
            spans.append([p, 1])
    phase, raw = rows["phase"][-1], rows["raw"][-1]
    pending = None if raw in (phase, "急跌") or (phase == "修复" and raw == "平稳") else raw
    recent = rows.tail(60).select(pl.col("date").cast(pl.Utf8), "phase", "limit_up", (pl.col("up") / pl.col("n")).alias("up_ratio"),
                                  (pl.col("high60") - pl.col("low60")).alias("net_high"))
    return {"as_of": str(rows["date"][-1]), "phase": phase, "days": spans[-1][1], "pending": pending,
            "path": [{"phase": p, "days": n} for p, n in spans[-3:]], "recent": recent.to_dicts()}


# today's read (§5.3): a count moves a vote past 3 and 15% of yesterday's, a rate past 5 points
ITEMS = (("limit_up", "涨停", 1, "多", "少"), ("broken", "炸板", -1, "多", "少"), ("seal_rate", "封板率", 1, "高", "低"),
         ("promo_rate", "晋级率", 1, "高", "低"), ("limit_down", "跌停", -1, "多", "少"))
RATES = ("seal_rate", "promo_rate")


def today_read(now: dict | None, then: dict | None, minute: int) -> dict | None:
    """偏强 / 偏弱 / 持平 against the same minute of yesterday, with what was better and worse (§5.3); None before the
    15th minute of the session or without both rows."""
    if now is None or then is None or minute < 15:
        return None
    better, worse = [], []
    for key, label, good, more, less in ITEMS:
        a, b = now.get(key), then.get(key)
        if a is None or b is None:
            continue
        d = a - b
        moved = abs(d) >= 0.05 - 1e-12 if key in RATES else abs(d) >= 3 and abs(d) > 0.15 * b
        if moved:
            (better if d * good > 0 else worse).append(label + (more if d > 0 else less))
    a, b = now.get("top_sealed"), then.get("top_sealed")
    if a is not None and b is not None and a != b:
        (better if a else worse).append("最高板封住" if a else "最高板炸板")
    score = len(better) - len(worse)
    return {"label": "偏强" if score >= 2 else "偏弱" if score <= -2 else "持平", "better": better, "worse": worse}
