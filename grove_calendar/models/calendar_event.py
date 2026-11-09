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

    @api.model
    def _grove_website_domain(self, window_days):
        """Public events (``_grove_public_domain``) that ALSO carry the Website tag.

        The third publication tier: private = team only, public = Discord,
        public + the ``grove_calendar.tag_website`` tag = the website. The
        public feed route ``GET /grove/api/v1/events`` (phase F, GOL-3842) and
        the "Website" calendar search filter share this one definition, so the
        tagged subset reads as its own public calendar. The tag is resolved by
        xmlid; if the seed record is ever missing the domain collapses to an
        impossible clause rather than falling back to every public event, so a
        broken install cannot leak the whole calendar to the open web.
        """
        tag = self.env.ref("grove_calendar.tag_website", raise_if_not_found=False)
        if not tag:
            return [("id", "=", False)]
        return self._grove_public_domain(window_days) + [("categ_ids", "in", tag.ids)]
