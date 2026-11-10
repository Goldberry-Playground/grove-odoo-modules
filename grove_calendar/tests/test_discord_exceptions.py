"""grove.calendar.discord exception alerts (GOL-3832, phase D, spec D.7).

Odoo-runtime TransactionCase. A recurring public series is created, then its
occurrences are cancelled / moved / restored; ``_detect_exceptions`` is run with
``requests.post`` monkeypatched, asserting one events-channel message per
exception, the reversal, and the past-date pruning.
"""

import json
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_POST = "odoo.addons.grove_calendar.models.calendar_discord.requests.post"

_ENV = {
    "DISCORD_EVENTS_WEBHOOK_URL": "https://discord.example/events",
    "GROVE_EVENTS_WINDOW_DAYS": "30",
}


@tagged("grove_calendar", "post_install", "-at_install")
class GroveDiscordExceptionsCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.sync = self.env["grove.calendar.discord"]
        self.CalendarEvent = self.env["calendar.event"]
        self.now = fields.Datetime.now()
        self.env.user.grove_discord_publisher = True

    def _series(self):
        start = self.now + timedelta(days=2)
        weekday_flag = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[start.weekday()]
        event = self.CalendarEvent.create(
            {
                "name": "Grove Team Meeting",
                "start": start,
                "stop": start + timedelta(hours=1),
                "privacy": "public",
                "location": "grove-chat",
                "recurrency": True,
                "rrule_type": "weekly",
                "interval": 1,
                weekday_flag: True,
                "count": 8,
                "event_tz": "America/New_York",
            }
        )
        return event.recurrence_id

    def _units(self, recurrence):
        now = fields.Datetime.now()
        horizon = now + timedelta(days=30)
        rep = self.sync._representative(recurrence, now, horizon)
        return [
            {
                "marker": f"odoo:calendar.recurrence:{recurrence.id}",
                "series": {"recurrence": recurrence, "representative": rep},
            }
        ]

    def _occurrences(self, recurrence):
        return recurrence.calendar_event_ids.sorted("start")

    def _run(self, recurrence, env=None):
        with mock.patch.dict("os.environ", env or _ENV, clear=True):
            with mock.patch(_POST) as post:
                self.sync._detect_exceptions(self._units(recurrence))
        return post

    # ---------------------------------------------------------------- cases
    def test_clean_series_posts_nothing(self):
        recurrence = self._series()
        post = self._run(recurrence)
        post.assert_not_called()

    def test_cancelled_occurrence_alerts_once(self):
        recurrence = self._series()
        # Archive the second occurrence (cancelled in Gmail → row goes inactive).
        self._occurrences(recurrence)[1].active = False
        post = self._run(recurrence)
        self.assertEqual(post.call_count, 1)
        self.assertIn("is cancelled", post.call_args.kwargs["json"]["content"])
        # Second run does not re-alert (dedupe).
        post2 = self._run(recurrence)
        post2.assert_not_called()

    def test_moved_occurrence_alerts_once_with_both_times(self):
        recurrence = self._series()
        occ = self._occurrences(recurrence)[1]
        original = occ.start
        occ.write({"start": original + timedelta(days=1), "stop": occ.stop + timedelta(days=1)})
        post = self._run(recurrence)
        self.assertEqual(post.call_count, 1)
        content = post.call_args.kwargs["json"]["content"]
        self.assertIn("moves from", content)
        self.assertIn("to", content)
        # Alerts once.
        post2 = self._run(recurrence)
        post2.assert_not_called()

    def test_restored_occurrence_alerts_reversal(self):
        recurrence = self._series()
        occ = self._occurrences(recurrence)[1]
        occ.active = False
        self._run(recurrence)  # cancelled alert, date recorded in dedupe
        occ.active = True
        post = self._run(recurrence)
        self.assertEqual(post.call_count, 1)
        self.assertIn("back on", post.call_args.kwargs["json"]["content"])

    def test_past_dates_are_pruned(self):
        recurrence = self._series()
        recurrence.discord_alerted_exceptions = json.dumps(["2020-01-01"])
        self._run(recurrence)  # clean series: no new alerts, stale date dropped
        self.assertNotIn("2020-01-01", recurrence.discord_alerted_exceptions or "")
