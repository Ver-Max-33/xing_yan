"""Data-layer review 2026-10-13: a JP report is readable at the close of its day only when it came before that day's
close — 15:00 until TSE moved it to 15:30 on 2024-11-05 (59,753 stored reports of 15:00–15:29 were read a day early),
and a time J-Quants gave as "15:30" (until 2018-03) is the close itself, not before "15:30:00" (3,058 reports)."""
from datetime import date

import polars as pl

from tests.test_pipeline import FakeJP, pipe
from wb.datasets import conform

OPEN = [date(2018, 3, 1), date(2018, 3, 2), date(2024, 11, 1), date(2024, 11, 5), date(2024, 11, 6)]


def test_a_jp_report_is_read_against_the_close_of_its_own_day(paths):
    p = pipe(paths, FakeJP())
    p.store.publish({"calendar": conform(pl.DataFrame({"date": OPEN, "is_open": True}), "calendar")}, as_of=OPEN[-1])
    cases = [  # (announced, time, readable from)
        (date(2024, 11, 1), "14:30:00", date(2024, 11, 1)),
        (date(2024, 11, 1), "15:00:00", date(2024, 11, 5)),   # the close was 15:00 then (11-04 closed)
        (date(2024, 11, 5), "15:00:00", date(2024, 11, 5)),   # 15:30 since that day
        (date(2024, 11, 5), "15:30:00", date(2024, 11, 6)),
        (date(2018, 3, 1), "15:30", date(2018, 3, 2)),        # the short form of the time
    ]
    fin = pl.DataFrame({"listing_id": [f"JP:{1300 + i}" for i in range(len(cases))], "period_end": date(2024, 9, 30),
                        "ann_date": [c[0] for c in cases], "ann_time": [c[1] for c in cases]})
    got = p._known(fin).sort("listing_id")["known_at"].to_list()
    assert got == [c[2] for c in cases]
