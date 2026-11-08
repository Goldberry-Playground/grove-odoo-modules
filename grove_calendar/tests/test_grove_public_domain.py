"""calendar.event._grove_public_domain (GOL-3830).

Odoo-runtime TransactionCase (runs under the install-smoke-test, not pytest).
Asserts the shared "which events reach Discord" domain: active + Privacy ==
Public + not yet ended + starting within the horizon.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("grove_calendar", "post_install", "-at_install")
class GrovePublicDomainCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.CalendarEvent = self.env["calendar.event"]
        self.now = fields.Datetime.now()

    def _event(self, name, *, start, stop, privacy="public", active=True):
        event = self.CalendarEvent.create(
            {
                "name": name,
                "start": start,
                "stop": stop,
                "privacy": privacy,
            }
        )
        if not active:
            event.active = False
        return event

    def _matches(self, window_days=14):
        domain = self.CalendarEvent._grove_public_domain(window_days)
        return self.CalendarEvent.search(domain)

    def test_public_future_in_window_is_included(self):
        event = self._event(
            "In window",
            start=self.now + timedelta(days=3),
            stop=self.now + timedelta(days=3, hours=1),
        )
        self.assertIn(event, self._matches())

    def test_ongoing_event_not_yet_ended_is_included(self):
        # Started in the past but still running (stop in the future): the "not yet
        # ended" near edge keeps it in the digest.
        event = self._event(
            "Ongoing",
            start=self.now - timedelta(hours=1),
            stop=self.now + timedelta(hours=1),
        )
        self.assertIn(event, self._matches())

    def test_private_event_is_excluded(self):
        event = self._event(
            "Private",
            start=self.now + timedelta(days=2),
            stop=self.now + timedelta(days=2, hours=1),
            privacy="private",
        )
        self.assertNotIn(event, self._matches())

    def test_archived_event_is_excluded(self):
        event = self._event(
            "Archived",
            start=self.now + timedelta(days=2),
            stop=self.now + timedelta(days=2, hours=1),
            active=False,
        )
        self.assertNotIn(event, self._matches())

    def test_finished_event_is_excluded(self):
        event = self._event(
            "Over",
            start=self.now - timedelta(days=2),
            stop=self.now - timedelta(days=1),
        )
        self.assertNotIn(event, self._matches())

    def test_event_beyond_horizon_is_excluded(self):
        event = self._event(
            "Too far out",
            start=self.now + timedelta(days=40),
            stop=self.now + timedelta(days=40, hours=1),
        )
        self.assertNotIn(event, self._matches(window_days=14))
        # The wider native-events horizon does include it.
        self.assertIn(event, self._matches(window_days=60))
