"""grove.planner.tester — the token that turns on tester mode (GOL-3549,
planner winter beta spec B.6).

Tester mode is a flag, not a login: Josh creates ~10 records by hand, each with
a random token shown once. ``GET /plan?tester=<token>`` (storefront) sets a
90-day HttpOnly cookie when the token is active; the BFF forwards the token on
dispute/propose calls and Odoo resolves the tester name here. There is no
tester UI in Odoo beyond the list view.
"""

import secrets

from odoo import api, fields, models


class GrovePlannerTester(models.Model):
    _name = "grove.planner.tester"
    _description = "Planner tester token"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    email = fields.Char(string="Email")
    token = fields.Char(
        string="Token",
        required=True,
        copy=False,
        index=True,
        default=lambda self: secrets.token_urlsafe(24),
        help="Random; put it in a /plan?tester=<token> link. Shown once.",
    )
    active = fields.Boolean(string="Active", default=True)

    # Odoo 19 dropped the `_sql_constraints` list attribute (it warns and never
    # creates the constraint); declared with the modern `models.Constraint`.
    _token_uniq = models.Constraint(
        "unique(token)",
        "A tester token must be unique.",
    )

    @api.model
    def name_for_token(self, token):
        """The active tester's name for a token, or ``None`` — the identity the
        dispute/propose routes stamp onto evidence/proposals (spec B.6)."""
        if not token:
            return None
        tester = self.sudo().search([("token", "=", token), ("active", "=", True)], limit=1)
        return tester.name if tester else None
