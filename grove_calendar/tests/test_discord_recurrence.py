"""Tests for discord_recurrence.py (GOL-3832, phase D).

Pure stdlib — loaded by file path so these run both under Odoo's test runner and
standalone (``python3 -m pytest grove_calendar/tests/test_discord_recurrence.py``).
No Odoo import, matching test_discord_format.py.

One test per row of the spec's section-3 mapping table, plus each fallback
trigger.
"""

import importlib.util
import os
import unittest
from datetime import datetime

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "discord_recurrence.py")
_spec = importlib.util.spec_from_file_location("grove_discord_recurrence", _MODULE_PATH)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

# A fixed representative start (Tue 2026-11-17 18:00 UTC) for rules that read it.
START = datetime(2026, 11, 17, 18, 0, 0)


def _rec(**over):
    """A recurrence dict with every field present and false/empty by default."""
    base = {
        "rrule_type": False,
        "interval": 1,
        "mon": False,
        "tue": False,
        "wed": False,
        "thu": False,
        "fri": False,
        "sat": False,
        "sun": False,
        "month_by": False,
        "byday": False,
        "weekday": False,
    }
    base.update(over)
    return base


class RecurrenceMappingTable(unittest.TestCase):
    """One assertion per row of spec section 3 (the expressible patterns)."""

    def test_weekly_interval_1_one_weekday(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="weekly", interval=1, tue=True), START)
        self.assertEqual(rule, {"frequency": dr.FREQ_WEEKLY, "interval": 1, "by_weekday": [1]})

    def test_weekly_interval_2_one_weekday(self):
        # Eldritch Counsel: every two weeks on Friday.
        rule = dr.build_recurrence_rule(_rec(rrule_type="weekly", interval=2, fri=True), START)
        self.assertEqual(rule, {"frequency": dr.FREQ_WEEKLY, "interval": 2, "by_weekday": [4]})

    def test_daily_interval_1(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="daily", interval=1), START)
        self.assertEqual(
            rule,
            {"frequency": dr.FREQ_DAILY, "interval": 1, "by_weekday": [0, 1, 2, 3, 4, 5, 6]},
        )

    def test_monthly_nth_weekday(self):
        # Second Wednesday of the month.
        rule = dr.build_recurrence_rule(
            _rec(rrule_type="monthly", interval=1, month_by="day", byday="2", weekday="WED"),
            START,
        )
        self.assertEqual(
            rule,
            {"frequency": dr.FREQ_MONTHLY, "interval": 1, "by_n_weekday": [{"n": 2, "day": 2}]},
        )

    def test_monthly_weekday_lowercase_selection(self):
        # Odoo's weekday selection is upper-case; accept either case defensively.
        rule = dr.build_recurrence_rule(
            _rec(rrule_type="monthly", interval=1, month_by="day", byday="1", weekday="mon"),
            START,
        )
        self.assertEqual(rule["by_n_weekday"], [{"n": 1, "day": 0}])

    def test_yearly_interval_1_from_representative(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="yearly", interval=1), START)
        self.assertEqual(
            rule,
            {"frequency": dr.FREQ_YEARLY, "interval": 1, "by_month": [11], "by_month_day": [17]},
        )


class RecurrenceFallbacks(unittest.TestCase):
    """Each pattern Discord cannot express returns None (caller posts one at a time)."""

    def test_weekly_several_weekdays(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="weekly", interval=1, mon=True, wed=True, fri=True), START)
        self.assertIsNone(rule)

    def test_weekly_no_weekday_set(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="weekly", interval=1), START)
        self.assertIsNone(rule)

    def test_weekly_interval_3(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="weekly", interval=3, tue=True), START)
        self.assertIsNone(rule)

    def test_daily_interval_above_1(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="daily", interval=2), START)
        self.assertIsNone(rule)

    def test_monthly_by_date(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="monthly", interval=1, month_by="date", day=15), START)
        self.assertIsNone(rule)

    def test_monthly_last_weekday(self):
        # byday '-1' (last weekday) has no Discord by_n_weekday form.
        rule = dr.build_recurrence_rule(
            _rec(rrule_type="monthly", interval=1, month_by="day", byday="-1", weekday="FRI"),
            START,
        )
        self.assertIsNone(rule)

    def test_monthly_interval_above_1(self):
        rule = dr.build_recurrence_rule(
            _rec(rrule_type="monthly", interval=2, month_by="day", byday="1", weekday="MON"),
            START,
        )
        self.assertIsNone(rule)

    def test_yearly_interval_above_1(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="yearly", interval=2), START)
        self.assertIsNone(rule)

    def test_unknown_rrule_type(self):
        rule = dr.build_recurrence_rule(_rec(rrule_type="hourly", interval=1), START)
        self.assertIsNone(rule)


class ExpectedOccurrences(unittest.TestCase):
    """The local wall-clock grid used by exception detection (spec D.7)."""

    def test_weekly_interval_1(self):
        rep = datetime(2026, 11, 17, 18, 0)  # Tue
        horizon = datetime(2026, 12, 9, 23, 59)  # ~3 weeks out
        occ = dr.expected_occurrences(_rec(rrule_type="weekly", interval=1, tue=True), rep, horizon)
        self.assertEqual(
            occ,
            [
                datetime(2026, 11, 17, 18, 0),
                datetime(2026, 11, 24, 18, 0),
                datetime(2026, 12, 1, 18, 0),
                datetime(2026, 12, 8, 18, 0),
            ],
        )

    def test_weekly_interval_2_biweekly(self):
        rep = datetime(2026, 11, 20, 19, 0)  # Fri
        horizon = datetime(2026, 12, 20, 0, 0)
        occ = dr.expected_occurrences(_rec(rrule_type="weekly", interval=2, fri=True), rep, horizon)
        self.assertEqual(
            occ, [datetime(2026, 11, 20, 19, 0), datetime(2026, 12, 4, 19, 0), datetime(2026, 12, 18, 19, 0)]
        )

    def test_weekly_wall_clock_is_dst_stable(self):
        # US DST ended 2026-11-01; a weekly series straddling it keeps the same
        # local wall-clock hour because the grid is naive local time.
        rep = datetime(2026, 10, 27, 18, 0)  # Tue, before the change
        horizon = datetime(2026, 11, 18, 0, 0)
        occ = dr.expected_occurrences(_rec(rrule_type="weekly", interval=1, tue=True), rep, horizon)
        self.assertTrue(all(dt.hour == 18 for dt in occ))

    def test_daily(self):
        rep = datetime(2026, 11, 17, 9, 0)
        horizon = datetime(2026, 11, 20, 9, 0)
        occ = dr.expected_occurrences(_rec(rrule_type="daily", interval=1), rep, horizon)
        self.assertEqual(len(occ), 4)
        self.assertEqual(occ[1], datetime(2026, 11, 18, 9, 0))

    def test_monthly_nth_weekday(self):
        # Second Wednesday, two months in the window.
        rep = datetime(2026, 11, 11, 18, 0)  # 2nd Wed of Nov 2026
        horizon = datetime(2026, 12, 31, 0, 0)
        occ = dr.expected_occurrences(
            _rec(rrule_type="monthly", interval=1, month_by="day", byday="2", weekday="WED"),
            rep,
            horizon,
        )
        self.assertEqual(occ, [datetime(2026, 11, 11, 18, 0), datetime(2026, 12, 9, 18, 0)])

    def test_yearly_single_in_short_window(self):
        rep = datetime(2026, 11, 17, 18, 0)
        horizon = datetime(2026, 12, 17, 0, 0)
        occ = dr.expected_occurrences(_rec(rrule_type="yearly", interval=1), rep, horizon)
        self.assertEqual(occ, [datetime(2026, 11, 17, 18, 0)])

    def test_fallback_pattern_returns_empty(self):
        # Several weekdays: no Discord grid, published as fallbacks → no exceptions.
        occ = dr.expected_occurrences(
            _rec(rrule_type="weekly", interval=1, mon=True, wed=True),
            datetime(2026, 11, 17, 18, 0),
            datetime(2026, 12, 17, 0, 0),
        )
        self.assertEqual(occ, [])


if __name__ == "__main__":
    unittest.main()
