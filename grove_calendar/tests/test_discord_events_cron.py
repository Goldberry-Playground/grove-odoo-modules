"""grove.calendar.discord native-events sync cron (GOL-3832, phase D).

Odoo-runtime TransactionCase (runs under the install-smoke-test, not pytest). A
stateful fake Discord client records create/patch/delete calls and holds the
guild's events, so a multi-run test sees the same reconciliation the live cron
would. Covers the spec D test matrix for the events cron.
"""

from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_calendar.models import calendar_discord as cd
from odoo.tests.common import TransactionCase, tagged

_ENV = {
    "DISCORD_BOT_TOKEN": "bot-token",
    "DISCORD_GUILD_ID": "9999",
    "GROVE_EVENTS_DEFAULT_ROOM": "grove-chat",
    "GROVE_EVENTS_WINDOW_DAYS": "30",
}

# A guild with one voice room the digest/sync can match on.
_CHANNELS = [
    {"id": "900", "name": "grove-chat", "type": 2},
    {"id": "901", "name": "Game-time", "type": 2},
    {"id": "500", "name": "general", "type": 0},  # text channel: never a room.
]


class FakeDiscordClient:
    """Records calls and holds guild state so sweeps/patches across runs are real."""

    def __init__(self, channels=None, events=None):
        self.channels = channels if channels is not None else list(_CHANNELS)
        self.events = list(events or [])
        self.calls = []  # (method, *args) for assertions
        self._next = 1000
        # Methods set here raise instead of acting, to model 404/429.
        self.raise_on = {}

    def _maybe_raise(self, method):
        exc = self.raise_on.get(method)
        if exc:
            raise exc

    def list_channels(self):
        self.calls.append(("list_channels",))
        return list(self.channels)

    def list_events(self):
        self.calls.append(("list_events",))
        return list(self.events)

    def create_event(self, payload):
        self._maybe_raise("create")
        self._next += 1
        event_id = str(self._next)
        self.events.append(
            {
                "id": event_id,
                "name": payload["name"],
                "description": payload["description"],
                "status": 1,  # SCHEDULED
            }
        )
        self.calls.append(("create", payload))
        return {"id": event_id}

    def patch_event(self, event_id, payload):
        self._maybe_raise("patch")
        for ev in self.events:
            if ev["id"] == event_id:
                ev.update({"name": payload["name"], "description": payload["description"]})
        self.calls.append(("patch", event_id, payload))
        return {"id": event_id}

    def delete_event(self, event_id):
        self._maybe_raise("delete")
        self.events = [e for e in self.events if e["id"] != event_id]
        self.calls.append(("delete", event_id))

    # convenience
    def count(self, method):
        return sum(1 for c in self.calls if c[0] == method)


@tagged("grove_calendar", "post_install", "-at_install")
class GroveDiscordEventsCronCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.sync = self.env["grove.calendar.discord"]
        self.CalendarEvent = self.env["calendar.event"]
        self.now = fields.Datetime.now()
        self.env.user.grove_discord_publisher = True
        self.fake = FakeDiscordClient()

    def _run(self, env=None, fake=None):
        """Run the cron with env vars set and the fake client injected."""
        fake = fake or self.fake
        with mock.patch.dict("os.environ", env or _ENV, clear=True):
            with mock.patch.object(type(self.sync), "_discord_client", return_value=fake):
                self.sync._cron_sync_discord_events()
        return fake

    def _oneoff(self, **over):
        vals = {
            "name": "Future Farmer Grant Review",
            "start": self.now + timedelta(days=3),
            "stop": self.now + timedelta(days=3, hours=1),
            "privacy": "public",
            "location": "grove-chat",
        }
        vals.update(over)
        return self.CalendarEvent.create(vals)

    def _series(self, **over):
        # Anchor the first occurrence far enough out that it is "not yet started"
        # and set the weekday flag that matches the start, so the recurrence maps
        # to a Discord weekly rule rather than relying on an onchange firing.
        start = self.now + timedelta(days=2)
        weekday_flag = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[start.weekday()]
        vals = {
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
        vals.update(over)
        return self.CalendarEvent.create(vals)

    # ------------------------------------------------------------- one-offs
    def test_create_one_off(self):
        event = self._oneoff()
        self._run()
        self.assertEqual(self.fake.count("create"), 1)
        self.assertTrue(event.discord_event_id)
        payload = self.fake.calls[-1][1]
        self.assertEqual(payload["entity_type"], cd._ENTITY_VOICE)
        self.assertEqual(payload["channel_id"], "900")
        self.assertIn(f"odoo:calendar.event:{event.id}", payload["description"])
        self.assertNotIn("recurrence_rule", payload)

    def test_external_location_is_entity_type_3(self):
        self._oneoff(location="2291 Armstrong Rd, Summersville, WV 26651")
        self._run()
        payload = self.fake.calls[-1][1]
        self.assertEqual(payload["entity_type"], cd._ENTITY_EXTERNAL)
        self.assertEqual(payload["entity_metadata"]["location"], "2291 Armstrong Rd, Summersville, WV 26651")

    def test_blank_location_uses_default_room(self):
        self._oneoff(location=False)
        self._run()
        payload = self.fake.calls[-1][1]
        self.assertEqual(payload["entity_type"], cd._ENTITY_VOICE)
        self.assertEqual(payload["channel_id"], "900")  # grove-chat default

    def test_skip_when_default_room_missing(self):
        self._oneoff(location=False)
        fake = FakeDiscordClient(channels=[{"id": "500", "name": "general", "type": 0}])
        self._run(fake=fake)
        self.assertEqual(fake.count("create"), 0)

    def test_no_op_on_second_identical_run(self):
        self._oneoff()
        self._run()
        self._run()  # Google sync bumps write_date; payload hash is unchanged.
        self.assertEqual(self.fake.count("create"), 1)
        self.assertEqual(self.fake.count("patch"), 0)

    def test_patch_after_name_change(self):
        event = self._oneoff()
        self._run()
        event.name = "Future Farmer Grant Review (rescheduled)"
        self._run()
        self.assertEqual(self.fake.count("patch"), 1)

    def test_dst_reanchor_patches_start(self):
        event = self._oneoff()
        self._run()
        event.start = event.start + timedelta(hours=1)
        event.stop = event.stop + timedelta(hours=1)
        self._run()
        self.assertEqual(self.fake.count("patch"), 1)

    def test_delete_on_archive(self):
        event = self._oneoff()
        self._run()
        event.active = False
        self._run()
        self.assertEqual(self.fake.count("delete"), 1)

    def test_delete_on_privacy_change(self):
        event = self._oneoff()
        self._run()
        event.privacy = "private"
        self._run()
        self.assertEqual(self.fake.count("delete"), 1)

    def test_hand_made_event_without_marker_untouched(self):
        hand = {"id": "42", "name": "Movie Night", "description": "no marker here", "status": 1}
        fake = FakeDiscordClient(events=[hand])
        self._run(fake=fake)
        self.assertEqual(fake.count("delete"), 0)
        self.assertIn(hand, fake.events)

    def test_recreate_after_404_on_patch(self):
        event = self._oneoff()
        self._run()
        first_id = event.discord_event_id
        self.fake.raise_on["patch"] = cd.DiscordNotFound("gone")
        event.name = "renamed"
        self._run()
        # 404 cleared the id; the field is forgotten so the next run recreates.
        self.assertFalse(event.discord_event_id)
        del self.fake.raise_on["patch"]
        self._run()
        self.assertTrue(event.discord_event_id)
        self.assertNotEqual(event.discord_event_id, first_id)

    def test_skip_when_token_unset(self):
        self._oneoff()
        self._run(env={"DISCORD_GUILD_ID": "9999"})  # no DISCORD_BOT_TOKEN
        self.assertEqual(self.fake.calls, [])

    def test_stop_on_429(self):
        self._oneoff()
        self.fake.raise_on["create"] = cd.DiscordRateLimited("busy")
        self._run()
        # The run stops; nothing is stored, so the next clean run creates it.
        self.assertEqual(self.fake.count("create"), 0)

    def test_unlink_deletes_discord_event(self):
        event = self._oneoff()
        self._run()
        event_id = event.discord_event_id
        with mock.patch.dict("os.environ", _ENV, clear=True):
            with mock.patch.object(type(self.sync), "_discord_client", return_value=self.fake):
                event.unlink()
        self.assertIn(("delete", event_id), self.fake.calls)

    # -------------------------------------------------------------- series
    def test_create_series_with_recurrence_rule(self):
        event = self._series()
        recurrence = event.recurrence_id
        self.assertTrue(recurrence, "Odoo should create a calendar.recurrence for the series")
        self._run()
        # The series is published once, keyed on the recurrence, with a rule.
        self.assertEqual(self.fake.count("create"), 1)
        payload = self.fake.calls[-1][1]
        self.assertIn("recurrence_rule", payload)
        self.assertEqual(payload["recurrence_rule"]["frequency"], cd.drec.FREQ_WEEKLY)
        self.assertEqual(payload["recurrence_rule"]["start"], payload["scheduled_start_time"])
        self.assertIn(f"odoo:calendar.recurrence:{recurrence.id}", payload["description"])
        self.assertTrue(recurrence.discord_event_id)

    def test_series_second_run_is_noop(self):
        self._series()
        self._run()
        self._run()
        self.assertEqual(self.fake.count("create"), 1)
        self.assertEqual(self.fake.count("patch"), 0)
