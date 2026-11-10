"""Record of a spring zone warning already sent, so each goes out once.

One row per company, zone, year and kind. ``kind`` is a Char: the spring warnings
use ``t5`` and ``day_of``, and the daily late-orders post stores ``late:YYYY-MM-DD``.
"""

from odoo import fields, models


class GroveZoneAlert(models.Model):
    _name = "grove.zone.alert"
    _description = "Spring zone warning sent"
    _order = "id desc"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    zone = fields.Char(required=True)
    year = fields.Integer(required=True)
    kind = fields.Char(required=True)

    # Odoo 19 ignores `_sql_constraints`; models.Constraint actually creates it.
    _company_zone_year_kind_uniq = models.Constraint(
        "unique(company_id, zone, year, kind)",
        "This zone warning has already been recorded.",
    )
