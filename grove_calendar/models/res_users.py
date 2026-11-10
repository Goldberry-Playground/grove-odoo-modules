"""`res.users` publish switch for the Grove Discord integration (GOL-3830).

An event reaches Discord only when it is Public **and** its organizer has this
switch on (spec section 5). It is an admin-managed preference, off by default, so
other users' Public events — CRM meetings, consult bookings that name customers —
stay team-only even if someone marks one Public, until an admin turns their
switch on. Josh turns it on for his own user after the Train #5 promote; nothing
is seeded, because user records have no stable xmlid on prod.
"""

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    # groups: admins (base.group_system) only — this gates who may publish the
    # whole company's calendar to Discord, not a per-user vanity toggle.
    grove_discord_publisher = fields.Boolean(
        string="Publishes Calendar Events to Discord",
        default=False,
        groups="base.group_system",
        help=(
            "When on, this user's Public calendar events are published to Discord "
            "(the weekly digest, the native events in phase D, and the public site "
            "feed in phase F). Off by default; an admin turns it on per user."
        ),
    )
