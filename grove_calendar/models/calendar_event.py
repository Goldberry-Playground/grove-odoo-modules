"""`calendar.event` additions for the Grove Discord integration (GOL-3830).

Adds the shared "which events are public" domain plus the three Discord
bookkeeping fields (also carried on ``calendar.recurrence``, for series). The
fields stay empty in phase B (the weekly digest); the native-events sync in
phase D (Train #6) is what fills ``discord_event_id`` / ``discord_payload_hash``.
"""

import os
from datetime import timedelta

from odoo import api, fields, models


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    discord_event_id = fields.Char(
        string="Discord Event ID",
        index=True,
        readonly=True,
        copy=False,
        help="Snowflake of the Discord scheduled event. Empty until the native-events sync (phase D) creates one.",
    )
    discord_payload_hash = fields.Char(
        string="Discord Payload Hash",
        readonly=True,
        copy=False,
        help=(
            "SHA-256 of the last payload pushed to Discord, compared instead of "
            "write_date (Google sync rewrites write_date on every row each run). "
            "Filled by the native-events sync (phase D)."
        ),
    )
    discord_url = fields.Char(
        string="Discord Event URL",
        compute="_compute_discord_url",
        help="Link to the Discord scheduled event, when one exists.",
    )
    organizer_publishes_to_discord = fields.Boolean(
        string="Organizer Publishes to Discord",
        related="user_id.grove_discord_publisher",
        readonly=True,
        help="Whether this event's organizer has the Discord publish switch on.",
    )

    @api.depends("discord_event_id", "recurrence_id.discord_event_id")
    def _compute_discord_url(self):
        # DISCORD_GUILD_ID is a phase-D env var; until it (and an event id, on the
        # event or its series) exist the URL is empty and the form link is hidden.
        guild = os.environ.get("DISCORD_GUILD_ID", "")
        for event in self:
            event_id = event.discord_event_id or event.recurrence_id.discord_event_id
            if event_id and guild:
                event.discord_url = f"https://discord.com/events/{guild}/{event_id}"
            else:
                event.discord_url = False

    def unlink(self):
        """Delete a one-off/fallback's Discord event at once on removal, so a
        deleted meeting does not wait for the 15-minute sweep (spec D.6 step 5).
        Series events are handled on ``calendar.recurrence``; here only rows that
        themselves carry a ``discord_event_id`` are forwarded."""
        discord = self.env["grove.calendar.discord"]
        for event in self:
            if event.discord_event_id:
                discord._delete_discord_event(event.discord_event_id)
        return super().unlink()

    @api.model
    def _grove_public_domain(self, window_days):
        """Domain selecting the public events that may reach Discord.

        One definition shared by the weekly digest, the phase-D native-events
        sync and the tests. An event qualifies when it is active, marked
        ``privacy == 'public'`` (settable from Gmail or Odoo once Google sync is
        on), its organizer (``user_id``) has the ``grove_discord_publisher``
        switch on, has not yet ended (``stop`` in the future) and starts within
        the horizon (``window_days`` from now — the coming Monday-to-Sunday week
        for the digest, ``GROVE_EVENTS_WINDOW_DAYS`` for native events). The
        "not yet ended" guard is the near edge of the window, so an all-day or
        already-running event that is still ongoing today is included; a
        finished event is dropped even if it started inside the horizon.
        """
        now = fields.Datetime.now()
        horizon = now + timedelta(days=window_days)
        return [
            ("active", "=", True),
            ("privacy", "=", "public"),
            ("user_id.grove_discord_publisher", "=", True),
            ("stop", ">", now),
            ("start", "<=", horizon),
        ]
