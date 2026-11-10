"""Tests for discord_format.py (GOL-3830).

Pure stdlib — loaded by file path so these run both under Odoo's test runner and
standalone (``python3 -m pytest grove_calendar/tests/test_discord_format.py``).
No Odoo import, matching grove_headless/tests/test_order_digest.py.
"""

import importlib.util
import os
import unittest
from datetime import date, datetime

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "discord_format.py")
_spec = importlib.util.spec_from_file_location("grove_discord_format", _MODULE_PATH)
df = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df)

# Window used by most assertions (Mon 2026-11-09 .. 2026-11-23).
WIN_START = date(2026, 11, 9)
WIN_END = date(2026, 11, 23)


def _timed(name, start, stop=None, location=""):
    """A timed event. ``start``/``stop`` are naive-UTC datetimes."""
    return {"name": name, "allday": False, "start": start, "stop": stop or start, "location": location}


def _allday(name, start, stop=None, location=""):
    """An all-day event. ``start``/``stop`` are inclusive dates."""
    return {"name": name, "allday": True, "start": start, "stop": stop or start, "location": location}


class EventLineTests(unittest.TestCase):
    def test_allday_single_day_shows_date_only(self):
        line = df.format_event_line(_allday("Young Farmers meeting", date(2026, 11, 13)))
        self.assertEqual(line, "- **Fri Nov 13** — Young Farmers meeting")

    def test_timed_single_day_shows_start_time(self):
        # 2026-11-27 23:30 UTC == 6:30 PM America/New_York (EST, UTC-5).
        line = df.format_event_line(_timed("Co-op alliance contract", datetime(2026, 11, 27, 23, 30)))
        self.assertEqual(line, "- **Fri Nov 27, 6:30 PM** — Co-op alliance contract")

    def test_location_appended_after_middot(self):
        line = df.format_event_line(
            _timed("Meeting with Wes", datetime(2026, 11, 27, 23, 30), location="2291 Armstrong Rd, Summersville")
        )
        self.assertEqual(line, "- **Fri Nov 27, 6:30 PM** — Meeting with Wes · 2291 Armstrong Rd, Summersville")

    def test_voice_room_location_renders_with_speaker(self):
        line = df.format_event_line(
            _timed("Farm Meeting", datetime(2026, 11, 10, 23, 0), location="grove-chat"),
            voice_rooms=("grove-chat", "Game-time"),
        )
        self.assertEqual(line, "- **Tue Nov 10, 6:00 PM** — Farm Meeting · 🔊 grove-chat")

    def test_non_room_location_renders_verbatim(self):
        line = df.format_event_line(
            _timed("Meeting with Wes", datetime(2026, 11, 11, 14, 0), location="2291 Armstrong Rd"),
            voice_rooms=("grove-chat",),
        )
        self.assertEqual(line, "- **Wed Nov 11, 9:00 AM** — Meeting with Wes · 2291 Armstrong Rd")

    def test_no_location_omits_middot(self):
        line = df.format_event_line(_allday("Market day", date(2026, 11, 14)))
        self.assertNotIn(" · ", line)

    def test_multiday_allday_shows_date_range_no_time(self):
        line = df.format_event_line(_allday("Harvest weekend", date(2026, 11, 14), date(2026, 11, 15)))
        self.assertEqual(line, "- **Sat Nov 14 – Sun Nov 15** — Harvest weekend")

    def test_multiday_timed_shows_date_range_no_time(self):
        # Nov 14 18:00 UTC == Nov 14 13:00 ET; Nov 15 18:00 UTC == Nov 15 13:00 ET
        # -> spans two Eastern days, so a date range (no time) is shown.
        line = df.format_event_line(_timed("Workshop", datetime(2026, 11, 14, 18, 0), datetime(2026, 11, 15, 18, 0)))
        self.assertEqual(line, "- **Sat Nov 14 – Sun Nov 15** — Workshop")

    def test_timezone_conversion_crosses_to_previous_eastern_day(self):
        # 2026-11-10 01:30 UTC == 2026-11-09 20:30 ET (previous calendar day).
        line = df.format_event_line(_timed("Evening tour", datetime(2026, 11, 10, 1, 30)))
        self.assertEqual(line, "- **Mon Nov 9, 8:30 PM** — Evening tour")

    def test_untitled_event_does_not_crash(self):
        line = df.format_event_line(_allday("", date(2026, 11, 13)))
        self.assertIn("(untitled)", line)


class MatchRoomTests(unittest.TestCase):
    ROOMS = ("grove-chat", "General", "Events", "Game-time")

    def test_exact_name_matches(self):
        self.assertEqual(df.match_room("grove-chat", self.ROOMS), "grove-chat")

    def test_match_is_case_insensitive_and_strips_hash(self):
        self.assertEqual(df.match_room("  #Grove-Chat ", self.ROOMS), "grove-chat")

    def test_non_room_text_does_not_match(self):
        self.assertIsNone(df.match_room("2291 Armstrong Rd, Summersville", self.ROOMS))

    def test_blank_does_not_match(self):
        self.assertIsNone(df.match_room("", self.ROOMS))
        self.assertIsNone(df.match_room("   ", self.ROOMS))

    def test_dict_channels_return_the_matched_object(self):
        channels = [{"id": "42", "name": "grove-chat"}, {"id": "7", "name": "General"}]
        self.assertEqual(df.match_room("grove-chat", channels), {"id": "42", "name": "grove-chat"})


class DigestMessageTests(unittest.TestCase):
    def test_empty_week_returns_none(self):
        self.assertIsNone(df.build_digest_message([], WIN_START, WIN_END))

    def test_header_uses_month_day_window(self):
        msg = df.build_digest_message([_allday("Market day", date(2026, 11, 14))], WIN_START, WIN_END)
        self.assertTrue(msg.startswith("📅 **This week at the Grove** (Nov 9 – Nov 23)\n"))

    def test_events_appear_in_given_order(self):
        events = [
            _allday("Young Farmers meeting", date(2026, 11, 13)),
            _timed("Farm Meeting", datetime(2026, 11, 27, 23, 30), location="grove-chat"),
        ]
        msg = df.build_digest_message(events, WIN_START, WIN_END, voice_rooms=("grove-chat",))
        lines = msg.splitlines()
        self.assertEqual(lines[1], "- **Fri Nov 13** — Young Farmers meeting")
        self.assertEqual(lines[2], "- **Fri Nov 27, 6:30 PM** — Farm Meeting · 🔊 grove-chat")

    def test_cut_at_2000_chars_with_more_note(self):
        events = [_allday(f"Event number {i} with a reasonably long title", date(2026, 11, 13)) for i in range(200)]
        msg = df.build_digest_message(events, WIN_START, WIN_END)
        self.assertLessEqual(len(msg), 2000)
        self.assertIn("more", msg.splitlines()[-1])

    def test_honours_custom_limit(self):
        events = [_allday(f"Event {i}", date(2026, 11, 13)) for i in range(50)]
        msg = df.build_digest_message(events, WIN_START, WIN_END, limit=200)
        self.assertLessEqual(len(msg), 200)


if __name__ == "__main__":
    unittest.main()
