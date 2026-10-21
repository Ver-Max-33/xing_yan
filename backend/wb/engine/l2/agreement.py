"""How far the algorithm's boxes agree with your labels (spec §5.10).

A label is compared with the box the algorithm had confirmed by the label's cutoff (seen as of that
day: one still running ends at the cutoff), the one whose price range overlaps the label most. A
sample agrees when both edges are within 2%, the price ranges overlap at least 0.7 and the starts
are at most max(5 bars, 15% of the box) apart; the end and the first close outside are reported but
not judged. A redraw of a label measures your own repeatability the same way — the baseline the
algorithm is held to. Prices are in one basis (the computation's, backward-adjusted)."""
from bisect import bisect_left
from datetime import date

import polars as pl

THRESHOLDS = {"edge": 0.02, "overlap": 0.7, "start_bars": 5, "start_share": 0.15}


def _i(dates: list[date], d: date) -> int:
    return bisect_left(dates, d)


def _overlap(a_top, a_bottom, b_top, b_bottom) -> float:
    inter = max(0.0, min(a_top, b_top) - max(a_bottom, b_bottom))
    union = max(a_top, b_top) - min(a_bottom, b_bottom)
    return inter / union if union > 0 else 0.0


def compare(label: dict, box: dict, dates: list[date], first_break: tuple | None = None, th: dict = THRESHOLDS) -> dict:
    """The §5.10 measures of `box` against `label` (start, end, top, bottom); bars counted on `dates`."""
    top_diff = abs(box["top"] / label["top"] - 1)
    bottom_diff = abs(box["bottom"] / label["bottom"] - 1)
    overlap = _overlap(label["top"], label["bottom"], box["top"], box["bottom"])
    start_diff = abs(_i(dates, box["start"]) - _i(dates, label["start"]))
    end_diff = abs(_i(dates, box["end"]) - _i(dates, label["end"])) if box.get("end") and label.get("end") else None
    length = _i(dates, label["end"]) - _i(dates, label["start"]) + 1
    consistent = (top_diff <= th["edge"] and bottom_diff <= th["edge"] and overlap >= th["overlap"]
                  and start_diff <= max(th["start_bars"], th["start_share"] * length))
    return {"top_diff": top_diff, "bottom_diff": bottom_diff, "overlap": overlap, "start_diff": start_diff,
            "end_diff": end_diff, "same_first_break": None if first_break is None else first_break[0] == first_break[1],
            "consistent": consistent}


def _first_close_outside(closes: pl.DataFrame, start: date, top: float, bottom: float) -> date | None:
    out = closes.filter((pl.col("date") > start) & ((pl.col("close") > top) | (pl.col("close") < bottom)))
    return out["date"][0] if out.height else None


def agreement(labels: list[dict], boxes: pl.DataFrame, bars: pl.DataFrame, th: dict = THRESHOLDS) -> dict:
    """`labels`: id, listing_id, cutoff, is_box, category, start, end, top, bottom (computation basis),
    grp, redraw_of, verdict; `boxes`: the algorithm's boxes of those listings; `bars`: listing_id,
    date, close (the bars the boxes were computed on)."""
    by_lid = {lid: g.sort("date") for (lid,), g in bars.group_by("listing_id")}
    est = boxes.filter(pl.col("status") == "established") if "status" in boxes.columns else boxes
    rows, own = [], {lb["id"]: lb for lb in labels}
    for lb in labels:
        if lb.get("redraw_of"):
            continue
        g = by_lid.get(lb["listing_id"])
        dates = g["date"].to_list() if g is not None else []
        cut = lb["cutoff"]
        seen = (est.filter((pl.col("listing_id") == lb["listing_id"]) & (pl.col("known_at") <= cut))
                .with_columns(pl.when(pl.col("ended_at").is_not_null() & (pl.col("ended_at") <= cut))
                              .then(pl.col("end")).otherwise(pl.lit(cut)).alias("end_seen"))
                .filter((pl.col("start") <= lb["end"]) & (pl.col("end_seen") >= lb["start"])))
        row = {"id": lb["id"], "listing_id": lb["listing_id"], "category": lb["category"], "is_box": lb["is_box"],
               "grp": lb["grp"], "verdict": lb.get("verdict"), "box_id": None, "consistent": False, "false_draw": None}
        # a 斜箱 label against the slant boxes (their edges on the confirming bar: ponytail — compare
        # the lines themselves if slanted labels are drawn), every other label against the boxes
        seen = seen.filter(pl.col("scale") == "slant") if lb["category"] == "slant" else seen.filter(pl.col("scale") != "slant")
        if lb["is_box"]:
            best = None
            for b in seen.iter_rows(named=True):
                box = {"start": b["start"], "end": b["end_seen"], "top": b["top"], "bottom": b["bottom"]}
                fb = None
                if g is not None:
                    fb = (_first_close_outside(g, lb["start"], lb["top"], lb["bottom"]),
                          _first_close_outside(g, b["start"], b["top"], b["bottom"]))
                m = compare(lb, box, dates, fb, th)
                key = (m["consistent"], m["overlap"], -m["start_diff"])  # a box that agrees beats one that only overlaps more
                if best is None or key > best[0]:
                    best = (key, b["box_id"], b["scale"], m)
            if best:
                row.update({"box_id": best[1], "box_scale": best[2], **best[3]})
        else:  # the algorithm drew a box where you said there is none
            span = max(1, _i(dates, lb["end"]) - _i(dates, lb["start"]) + 1)
            hits = [b["box_id"] for b in seen.iter_rows(named=True)
                    if (_i(dates, min(b["end_seen"], lb["end"])) - _i(dates, max(b["start"], lb["start"])) + 1) / span >= 0.5
                    and _overlap(lb["top"], lb["bottom"], b["top"], b["bottom"]) >= 0.5]
            row.update({"false_draw": bool(hits), "box_id": hits[0] if hits else None, "consistent": not hits})
        rows.append(row)

    pairs = [(own[lb["redraw_of"]], lb) for lb in labels if lb.get("redraw_of") and lb["redraw_of"] in own]
    same = 0
    for orig, again in pairs:
        g = by_lid.get(orig["listing_id"])
        if compare(orig, again, g["date"].to_list() if g is not None else [], None, th)["consistent"]:
            same += 1
    summary = {}
    for grp in ("tune", "test"):
        mine = [r for r in rows if r["grp"] == grp]
        box = [r for r in mine if r["is_box"]]
        ok = [r for r in box if r["consistent"] or r["verdict"] == "both_ok"]
        cats: dict[str, list[int]] = {}
        for r in box:
            c = cats.setdefault(r["category"], [0, 0])
            c[0] += r in ok
            c[1] += 1
        summary[grp] = {"boxes": len(box), "consistent": len(ok), "rate": len(ok) / len(box) if box else None,
                        "by_category": cats, "not_boxes": sum(1 for r in mine if not r["is_box"]),
                        "false_draws": sum(1 for r in mine if r["false_draw"]),
                        "pending": sum(1 for r in mine if not r["consistent"] and r["verdict"] is None)}
    own_rate = same / len(pairs) if pairs else None
    test = summary["test"]
    checks = {
        "test_rate_vs_yours": (test["rate"] is not None and own_rate is not None and test["rate"] >= 0.8 * own_rate),
        "no_category_at_zero": bool(test["by_category"]) and all(c[0] > 0 for c in test["by_category"].values()),
        "false_draws_at_most_one": test["false_draws"] <= 1,
        "all_reviewed": test["pending"] == 0 and summary["tune"]["pending"] == 0,
    }
    return {"labels": rows, "summary": summary, "self": {"pairs": len(pairs), "consistent": same, "rate": own_rate},
            "acceptance": {"passed": all(checks.values()), "checks": checks,
                           "too_strict": own_rate is not None and own_rate < 0.6},
            "thresholds": th}
