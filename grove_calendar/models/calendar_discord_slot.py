"""Per-weekday Discord bookkeeping for a split weekly series.

Discord's weekly ``recurrence_rule`` takes exactly one weekday, so a Gmail series
on several weekdays ("every 2 weeks on Friday and Saturday") is published as one
Discord series per weekday. ``calendar.recurrence`` holds a single
``discord_event_id``, so each weekday's Discord event is tracked on a slot row
instead. A slot carries the same two bookkeeping fields the sync reads on
``calendar.event`` / ``calendar.recurrence``, which lets the push, sweep and
marker-clear code treat it like any other unit record.
"""

from odoo import fields, models


class GroveCalendarDiscordSlot(models.Model):
    _name = "grove.calendar.discord.slot"
    _description = "Discord event for one weekday of a split weekly series"

    recurrence_id = fields.Many2one("calendar.recurrence", required=True, index=True, ondelete="cascade")
    weekday = fields.Integer(required=True, help="Monday 0 .. Sunday 6, in the series' own timezone.")
    discord_event_id = fields.Char(string="Discord Event ID", index=True, readonly=True, copy=False)
    discord_payload_hash = fields.Char(string="Discord Payload Hash", readonly=True, copy=False)

    def unlink(self):
        """Delete the weekday's Discord event at once rather than waiting for the sweep."""
        discord = self.env["grove.calendar.discord"]
        for slot in self:
            if slot.discord_event_id:
                discord._delete_discord_event(slot.discord_event_id)
        return super().unlink()
