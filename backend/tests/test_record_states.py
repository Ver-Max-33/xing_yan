"""The state recorder (scripts/market/record_states.py, design 2026-10-12-market-page-redesign §9.2) refuses a real tick
that cannot stand for its moment: TickFlow gives the quotes of the moment it is asked (down-mid was once recorded at
14:15 with its clock at 14:50 — its "14:50" board held 14:15's quotes)."""
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
spec = importlib.util.spec_from_file_location("record_states", Path(__file__).parents[1] / "scripts" / "market" / "record_states.py")
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


def t(h, m, day=12):
    return datetime(2026, 10, day, h, m, tzinfo=CST)


def test_a_moment_in_the_session_is_recorded_within_two_minutes_after_it():
    assert rec.why_not_real(t(14, 50), t(14, 50)) is None and rec.why_not_real(t(14, 50), t(14, 52)) is None
    assert "14:50–14:52" in rec.why_not_real(t(14, 50), t(14, 15))      # the quotes would be 14:15's
    assert rec.why_not_real(t(14, 50), t(14, 53)) is not None             # and here 14:53's


def test_after_the_close_any_time_that_evening_and_never_another_day():
    assert rec.why_not_real(t(19, 0), t(15, 6)) is None and rec.why_not_real(t(19, 0), t(23, 0)) is None
    assert rec.why_not_real(t(19, 0), t(14, 59)) is not None              # still trading
    assert rec.why_not_real(t(19, 0), t(9, 0, day=13)) is not None        # the next day
