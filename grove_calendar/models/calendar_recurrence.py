"""`calendar.recurrence` additions for the Grove Discord integration (GOL-3830).

A recurring Gmail meeting becomes one Discord event per *series*, never one per
occurrence (ruled 2026-11-08), so the Discord bookkeeping for a series lives on
``calendar.recurrence`` rather than on each expanded ``calendar.event`` row. The
fields mirror those on ``calendar.event``; they stay empty in phase B (the weekly
digest) and are filled by the native-events sync in phase D (Train #6).
"""

import os

from odoo import api, fields, models


class CalendarRecurrence(models.Model):
    _inherit = "calendar.recurrence"

    discord_event_id = fields.Char(
        string="Discord Event ID",
        index=True,
        readonly=True,
        copy=False,
        help="Snowflake of the Discord scheduled event for this series. Empty until the native-events sync (phase D) creates one.",
    )
    discord_payload_hash = fields.Char(
        string="Discord Payload Hash",
        readonly=True,
        copy=False,
        help=(
            "SHA-256 of the last payload pushed to Discord for this series, "
            "compared instead of write_date. Filled by the native-events sync (phase D)."
        ),
    )
    discord_url = fields.Char(
        string="Discord Event URL",
        compute="_compute_discord_url",
        help="Link to the Discord scheduled event for this series, when one exists.",
    )

    @api.depends("discord_event_id")
    def _compute_discord_url(self):
        # DISCORD_GUILD_ID is a phase-D env var; until it (and an event id) exist
        # the URL is empty.
        guild = os.environ.get("DISCORD_GUILD_ID", "")
        for recurrence in self:
            if recurrence.discord_event_id and guild:
                recurrence.discord_url = f"https://discord.com/events/{guild}/{recurrence.discord_event_id}"
            else:
                recurrence.discord_url = False
