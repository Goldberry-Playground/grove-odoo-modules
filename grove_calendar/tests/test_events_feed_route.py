"""Public events feed route GET /grove/api/v1/events (GOL-3842, phase F).

Covers the spec's route checks: response shape, the 5-minute cache header, no
leakage of untagged or private events, and the ?days= cap at 180. HttpCase so
the real controller + domain run end to end against the test server.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged("grove_calendar", "post_install", "-at_install")
class GroveEventsFeedRouteCase(HttpCase):
    def setUp(self):
        super().setUp()
        self.CalendarEvent = self.env["calendar.event"]
        self.website_tag = self.env.ref("grove_calendar.tag_website")
        self.now = fields.Datetime.now()
        # The organizer (acting user) must publish for the public-domain clause.
        self.env.user.grove_discord_publisher = True

    def _event(self, name, *, privacy="public", tagged_website=True, days=3, **extra):
        vals = {
            "name": name,
            "start": self.now + timedelta(days=days),
            "stop": self.now + timedelta(days=days, hours=1),
            "privacy": privacy,
        }
        if tagged_website:
            vals["categ_ids"] = [(4, self.website_tag.id)]
        vals.update(extra)
        return self.CalendarEvent.create(vals)

    def _fetch(self, query=""):
        resp = self.url_open("/grove/api/v1/events" + query)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp

    def _names(self, query=""):
        return [e["name"] for e in self._fetch(query).json()["events"]]

    def test_cache_header_present(self):
        self.assertEqual(self._fetch().headers.get("Cache-Control"), "public, max-age=300")

    def test_body_shape_and_fields(self):
        self._event(
            "Open House",
            location="2291 Armstrong Rd, Summersville",
            description="<p>Rain <b>or</b> shine</p>",
        )
        body = self._fetch().json()
        self.assertEqual(body["tz"], "America/New_York")
        row = next(e for e in body["events"] if e["name"] == "Open House")
        self.assertEqual(
            set(row),
            {"id", "name", "start", "stop", "allday", "location", "description_text"},
        )
        # HTML is stripped; attendees/organizer never appear.
        self.assertEqual(row["description_text"], "Rain or shine")
        self.assertEqual(row["location"], "2291 Armstrong Rd, Summersville")
        self.assertTrue(row["start"].endswith("Z"))
        self.assertNotIn("partner_ids", row)
        self.assertNotIn("user_id", row)

    def test_allday_flag_round_trips(self):
        self._event("Harvest Day", allday=True)
        row = next(e for e in self._fetch().json()["events"] if e["name"] == "Harvest Day")
        self.assertTrue(row["allday"])

    def test_untagged_and_private_events_do_not_leak(self):
        self._event("Tagged Public")
        self._event("Untagged Public", tagged_website=False)
        self._event("Tagged Private", privacy="private")
        names = self._names()
        self.assertIn("Tagged Public", names)
        self.assertNotIn("Untagged Public", names)
        self.assertNotIn("Tagged Private", names)

    def test_days_param_is_capped_at_180(self):
        self._event("Within Default", days=30)
        self._event("Mid Horizon", days=170)
        self._event("Beyond Cap", days=185)

        # Default window is 90 days: only the near event.
        default_names = self._names()
        self.assertIn("Within Default", default_names)
        self.assertNotIn("Mid Horizon", default_names)

        # A wider explicit window reaches the 170-day event.
        self.assertIn("Mid Horizon", self._names("?days=175"))

        # Asking past the cap still tops out at 180, so the 185-day event is out.
        capped_names = self._names("?days=9999")
        self.assertIn("Mid Horizon", capped_names)
        self.assertNotIn("Beyond Cap", capped_names)

    def test_malformed_days_falls_back_to_default(self):
        self._event("Near", days=10)
        self.assertEqual(self._fetch("?days=notanumber").json(), self._fetch().json())
        self.assertIn("Near", self._names("?days=notanumber"))
