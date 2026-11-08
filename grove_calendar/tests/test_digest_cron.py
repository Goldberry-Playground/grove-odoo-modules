"""grove.calendar.digest weekly cron (GOL-3830).

Odoo-runtime TransactionCase (runs under the install-smoke-test, not pytest).
``requests.post`` is monkeypatched: asserts the webhook fallback order, the
disarmed allowed_mentions, and that an empty week posts nothing.
"""

from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_POST = "odoo.addons.grove_calendar.models.calendar_digest.requests.post"


@tagged("grove_calendar", "post_install", "-at_install")
class GroveCalendarDigestCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.digest = self.env["grove.calendar.digest"]
        self.now = fields.Datetime.now()

    def _public_event(self):
        return self.env["calendar.event"].create(
            {
                "name": "Young Farmers meeting",
                "start": self.now + timedelta(days=2),
                "stop": self.now + timedelta(days=2, hours=1),
                "privacy": "public",
            }
        )

    def test_prefers_events_webhook_over_ops(self):
        self._public_event()
        env = {
            "DISCORD_EVENTS_WEBHOOK_URL": "https://discord.example/events",
            "DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch(_POST) as post:
                self.digest._cron_send_weekly_digest()
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://discord.example/events")
        # allowed_mentions disarmed on every post.
        self.assertEqual(post.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})

    def test_falls_back_to_ops_when_events_webhook_unset(self):
        self._public_event()
        with mock.patch.dict("os.environ", {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops"}, clear=True):
            with mock.patch(_POST) as post:
                self.digest._cron_send_weekly_digest()
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://discord.example/ops")

    def test_noop_when_no_webhook_set(self):
        self._public_event()
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch(_POST) as post:
                self.digest._cron_send_weekly_digest()
        post.assert_not_called()

    def test_empty_week_posts_nothing(self):
        # No public events in the window — even with a webhook configured, nothing
        # is posted.
        env = {"DISCORD_EVENTS_WEBHOOK_URL": "https://discord.example/events"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch(_POST) as post:
                self.digest._cron_send_weekly_digest()
        post.assert_not_called()

    def test_posted_content_is_within_discord_limit(self):
        self._public_event()
        env = {"DISCORD_EVENTS_WEBHOOK_URL": "https://discord.example/events"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch(_POST) as post:
                self.digest._cron_send_weekly_digest()
        content = post.call_args.kwargs["json"]["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertIn("Coming up at the Grove", content)
        self.assertIn("Young Farmers meeting", content)
