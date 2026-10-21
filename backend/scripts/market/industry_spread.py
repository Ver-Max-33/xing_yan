"""How far apart industries usually are against the market — the basis of the strength tags' thresholds (design
2026-10-12-market-page-redesign §3.3, board.TREND). For each of the last 250 trading days: each industry's median return
less the whole market's, over the last 5 bars, the 15 before them and the last 20; the spread (std) across industries
that day; then the median of the daily spreads, and how often each tag comes out at the thresholds in use. Also: whether
申万二级 names stay distinct with their trailing "Ⅱ" dropped (the page shows them so).

    uv run --frozen python -m scripts.market.industry_spread <data_dir> <l1|l2> <CN|JP> [--end YYYY-MM-DD]

Read-only on the data it is given (a copy is safest); prints numbers, writes nothing."""
import argparse
from datetime import date, timedelta

import polars as pl


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir")
    ap.add_argument("level", choices=["l1", "l2"])
    ap.add_argument("market", choices=["CN", "JP"])
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    a = ap.parse_args()

    from wb.config import get_paths
    from wb.data.store import Store
    from wb.engine.l1.compiler import Compiler
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.engine.l1.run import evaluate
    from wb.market.board import TREND, trend

    paths = get_paths("prod", a.data_dir)
    st = Store(paths, a.market)
    snap = st.latest()
    end = a.end or snap.as_of
    cal = sorted(st.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list())
    comp = Compiler(a.market, available=available_columns(st, snap))
    for n, f in {"c": "C", "c5": "REF(C, 5)", "c20": "REF(C, 20)"}.items():
        comp.add(n, f)
    prog = comp.program()
    frame = build_frame(st, a.market, paths.profile, prog.columns | {"industry"}, start=end - timedelta(days=420), snap=snap)
    v = evaluate(frame, prog, cal).join(frame.select("listing_id", "date", "industry"), on=["listing_id", "date"], how="left")
    days = [d for d in cal if d <= end][-250:]
    if a.level == "l2":   # 申万二级: each listing's latest spell (an estimate of the spread)
        rows = st.scan("industry", snap).select("listing_id", "in_date", "l2_name").collect().sort("in_date")
        l2 = rows.unique("listing_id", keep="last").select("listing_id", pl.col("l2_name").alias("l2"))
        v = v.join(l2, on="listing_id", how="left").drop("industry").rename({"l2": "industry"})
        names = sorted(rows["l2_name"].drop_nulls().unique().to_list())
        plain = {n[:-1] if n.endswith("Ⅱ") else n for n in names}
        print(f"二级 names: {len(names)}, {sum(n.endswith('Ⅱ') for n in names)} with Ⅱ; distinct without it: {len(plain) == len(names)}")
    v = v.filter(pl.col("date").is_in(days) & pl.col("industry").is_not_null()).with_columns(
        (pl.col("c") / pl.col("c5") - 1).alias("r5"), (pl.col("c5") / pl.col("c20") - 1).alias("e15"),
        (pl.col("c") / pl.col("c20") - 1).alias("r20"))
    mk = v.group_by("date").agg([pl.col(k).median().alias(f"m_{k}") for k in ("r5", "e15", "r20")])
    ind = (v.group_by("date", "industry").agg(pl.len().alias("n"), *[pl.col(k).median() for k in ("r5", "e15", "r20")])
           .filter(pl.col("n") > 3).join(mk, on="date")
           .with_columns([(pl.col(k) - pl.col(f"m_{k}")).alias(f"x_{k}") for k in ("r5", "e15", "r20")]))
    sd = ind.group_by("date").agg([pl.col(f"x_{k}").std().alias(k) for k in ("r5", "e15", "r20")])
    print(f"{a.market} {a.level}: {ind.group_by('date').len()['len'].median():.0f} industries a day, {days[0]} … {days[-1]}")
    print("typical spread (median of the daily std) — last 5 / the 15 before / last 20:",
          " / ".join(f"{sd[k].median():.4f}" for k in ("r5", "e15", "r20")))
    t5, t15 = TREND.get((a.market, a.level), TREND[("CN", a.level)])
    tags = ind.select(pl.struct("x_r5", "x_e15").map_elements(lambda r: trend(r["x_r5"], r["x_e15"], t5, t15) or "不标",
                                                                return_dtype=pl.Utf8).alias("tag"))["tag"]
    share = {k: tags.to_list().count(k) / len(tags) for k in ("一直强", "转强", "转弱", "一直弱", "不标")}
    print(f"tags at ±{t5:.3f} / ±{t15:.3f}:", ", ".join(f"{k} {v:.3f}" for k, v in share.items()))
    old = ind.filter((pl.col("x_r20") >= 0.01) & (pl.col("x_r5") >= 0.01))   # the old quadrant's 一直强
    print(f"the old 一直强 (20 and 5 bars both ≥ +1%) whose 15 bars before were under +1%: {old.filter(pl.col('x_e15') < 0.01).height / max(1, old.height):.2f}")


if __name__ == "__main__":
    main()
