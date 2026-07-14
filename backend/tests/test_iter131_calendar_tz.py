"""iter-131 · Calendar timezone fix.

2026-07-14: US CPI spiked gold 600 pips at 12:30 UTC. The FF feed said
'12:30pm' (already UTC) but the parser assumed Eastern and added +4h, so
every pre-event guard armed at 16:30 — four hours after the shock.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from economic_calendar import _parse_event_time  # noqa: E402


class TestParseEventTime:
    def test_cpi_time_is_utc_verbatim(self):
        # Feed said 12:30pm on CPI day; actual spike was 12:30 UTC.
        r = _parse_event_time("07-14-2026", "12:30pm")
        assert (r.hour, r.minute) == (12, 30)
        assert r.isoformat().startswith("2026-07-14T12:30")

    def test_am_pm_parsing(self):
        assert _parse_event_time("07-15-2026", "8:30am").hour == 8
        assert _parse_event_time("07-15-2026", "2:00pm").hour == 14
        assert _parse_event_time("07-15-2026", "12:00am").hour == 0
        assert _parse_event_time("07-15-2026", "12:45pm").hour == 12

    def test_all_day_midnight_utc(self):
        r = _parse_event_time("07-14-2026", "All Day")
        assert (r.hour, r.minute) == (0, 0)

    def test_no_double_shift_left_in_source(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "economic_calendar.py")).read()
        assert "timedelta(hours=4)" not in src

    def test_bad_input(self):
        assert _parse_event_time("garbage", "8:30am") is None
