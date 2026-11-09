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
        help=(
            "Snowflake of the Discord scheduled event for this series. Empty "
            "until the native-events sync (phase D) creates one."
        ),
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
    discord_alerted_exceptions = fields.Char(
        string="Discord Alerted Exceptions",
        readonly=True,
        copy=False,
        help=(
            "JSON list of ISO dates of this series' original occurrences already "
            "announced as skipped/moved in the events channel (phase D, spec D.7). "
            "The dedupe for one-alert-per-exception; past dates are pruned each run."
        ),
    )
    discord_fallback_warned = fields.Char(
        string="Discord Fallback Warned On",
        readonly=True,
        copy=False,
        help=(
            "ISO date of the last ops warning that this series repeats in a way "
            "Discord cannot express (phase D, spec D.3). Dedupes the warning to "
            "once per series per day."
        ),
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

    def unlink(self):
        """Delete the series' Discord event at once, so a removed series does not
        linger for up to 15 minutes before the sweep (spec D.6 step 5)."""
        discord = self.env["grove.calendar.discord"]
        for recurrence in self:
            if recurrence.discord_event_id:
                discord._delete_discord_event(recurrence.discord_event_id)
        return super().unlink()
