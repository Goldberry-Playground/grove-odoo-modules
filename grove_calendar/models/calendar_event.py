"""`calendar.event` additions for the Grove Discord integration (GOL-3830).

Adds the shared "which events are public" domain plus the three Discord
bookkeeping fields. The fields stay empty in phase B (the weekly digest); the
native-events sync in phase D (Train #6) is what fills ``discord_event_id`` /
``discord_synced_at``.
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
    discord_synced_at = fields.Datetime(
        string="Discord Synced At",
        readonly=True,
        copy=False,
        help="When the Discord scheduled event last matched this row.",
    )
    discord_url = fields.Char(
        string="Discord Event URL",
        compute="_compute_discord_url",
        help="Link to the Discord scheduled event, when one exists.",
    )

    @api.depends("discord_event_id")
    def _compute_discord_url(self):
        # DISCORD_GUILD_ID is a phase-D env var; until it (and a synced event id)
        # exist the URL is empty and the form group stays hidden.
        guild = os.environ.get("DISCORD_GUILD_ID", "")
        for event in self:
            if event.discord_event_id and guild:
                event.discord_url = f"https://discord.com/events/{guild}/{event.discord_event_id}"
            else:
                event.discord_url = False

    @api.model
    def _grove_public_domain(self, window_days):
        """Domain selecting the public events that may reach Discord.

        One definition shared by the weekly digest, the phase-D native-events
        sync and the tests. An event qualifies when it is active, marked
        ``privacy == 'public'`` (settable from Gmail or Odoo once Google sync is
        on), has not yet ended (``stop`` in the future) and starts within the
        horizon (``window_days`` from now). The "not yet ended" guard is the
        near edge of the window, so an all-day or already-running event that is
        still ongoing today is included; a finished event is dropped even if it
        started inside the horizon.
        """
        now = fields.Datetime.now()
        horizon = now + timedelta(days=window_days)
        return [
            ("active", "=", True),
            ("privacy", "=", "public"),
            ("stop", ">", now),
            ("start", "<=", horizon),
        ]
