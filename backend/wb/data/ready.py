"""Readiness detection (spec §4.5): neither source announces "done", so poll every N minutes from
the ready window's start and accept when today's row count is >= 98% of the expected listings and
did not change between two consecutive polls — the second look comes CONFIRM minutes after a count that is already
there, not a whole poll later. At or after the window's end: one last count, then accept or give up (data.delayed)."""
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from wb.markets import Market

CONFIRM = 2   # minutes: a day that looks complete is looked at once more to see it settled


def is_ready(have: int, expected: int, previous: int | None, ratio: float = 0.98) -> bool:
    return expected > 0 and have >= expected * ratio and previous == have


def wait_ready(count: Callable[[], tuple[int, int]], market: Market, *,
               now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
               sleep: Callable[[float], None], log: Callable[[str], None] = print) -> bool:
    tz = ZoneInfo(market.timezone)
    previous: int | None = None
    while True:
        t = now().astimezone(tz)
        start = t.replace(hour=market.ready_start.hour, minute=market.ready_start.minute, second=0, microsecond=0)
        end = t.replace(hour=market.ready_end.hour, minute=market.ready_end.minute, second=0, microsecond=0)
        if t < start:
            sleep((start - t).total_seconds())
            continue
        if t >= end:  # last look after the window: take the data if it is there, else report a delay
            have, expected = count()
            log(f"{market.code} {t:%H:%M} rows {have}/{expected} (after the window)")
            return is_ready(have, expected, have)
        have, expected = count()
        log(f"{market.code} {t:%H:%M} rows {have}/{expected}")
        if is_ready(have, expected, previous):
            return True
        settling = expected > 0 and have >= expected * 0.98
        previous = have
        sleep(timedelta(minutes=CONFIRM if settling else market.poll_minutes).total_seconds())
