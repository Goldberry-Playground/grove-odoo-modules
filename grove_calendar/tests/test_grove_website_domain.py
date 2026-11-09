"""calendar.event._grove_website_domain (GOL-3842, phase F).

The website tier = ``_grove_public_domain`` AND the Website tag. Asserts the
three distinguishing cases the spec calls out — tagged public (in), tagged
private (out), untagged public (out) — plus the shared publisher switch.
Runs under Odoo's install-smoke-test, not pytest.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("grove_calendar", "post_install", "-at_install")
class GroveWebsiteDomainCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.CalendarEvent = self.env["calendar.event"]
        self.website_tag = self.env.ref("grove_calendar.tag_website")
        self.now = fields.Datetime.now()
        # Events default their organizer (user_id) to the acting user; give it the
        # publish switch so the baseline "public" cases qualify.
        self.env.user.grove_discord_publisher = True

    def _event(self, name, *, privacy="public", tagged_website=True, days=3):
        vals = {
            "name": name,
            "start": self.now + timedelta(days=days),
            "stop": self.now + timedelta(days=days, hours=1),
            "privacy": privacy,
        }
        if tagged_website:
            vals["categ_ids"] = [(4, self.website_tag.id)]
        return self.CalendarEvent.create(vals)

    def _matches(self, window_days=90):
        domain = self.CalendarEvent._grove_website_domain(window_days)
        return self.CalendarEvent.search(domain)

    def test_tagged_public_event_is_included(self):
        event = self._event("Tagged public")
        self.assertIn(event, self._matches())

    def test_tagged_private_event_is_excluded(self):
        event = self._event("Tagged private", privacy="private")
        self.assertNotIn(event, self._matches())

    def test_untagged_public_event_is_excluded(self):
        # Reaches Discord (it is in _grove_public_domain) but NOT the website.
        event = self._event("Untagged public", tagged_website=False)
        self.assertIn(event, self.CalendarEvent.search(self.CalendarEvent._grove_public_domain(90)))
        self.assertNotIn(event, self._matches())

    def test_publisher_switch_off_excludes_tagged_event(self):
        event = self._event("Tagged but organizer off")
        self.assertIn(event, self._matches())
        event.user_id.grove_discord_publisher = False
        self.assertNotIn(event, self._matches())

    def test_tagged_event_beyond_horizon_is_excluded(self):
        event = self._event("Tagged far out", days=120)
        self.assertNotIn(event, self._matches(window_days=90))
        self.assertIn(event, self._matches(window_days=150))
