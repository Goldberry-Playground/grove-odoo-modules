"""grove.consult.area — a design area on a client's property (spec C.3).

One row per distinct area the client wants designed, each tied to a practice and
sized in acres, with the site facts (wet, poor soil, shade, aspect) the design
depends on. ``farmos_land_uuid`` is set in phase 3 when the polygon is drawn.
"""

from odoo import fields, models


class GroveConsultArea(models.Model):
    _name = "grove.consult.area"
    _description = "Grove consult design area"
    _order = "sequence, id"

    lead_id = fields.Many2one("crm.lead", string="Lead", required=True, index=True, ondelete="cascade")
    name = fields.Char(string="Name", required=True)
    practice_id = fields.Many2one("grove.consult.practice", string="Practice", ondelete="restrict")
    acres = fields.Float(string="Acres")
    wet = fields.Boolean(string="Wet")
    poor_soil = fields.Boolean(string="Poor soil")
    shade = fields.Boolean(string="Shade")
    aspect = fields.Selection(
        [("N", "North"), ("E", "East"), ("S", "South"), ("W", "West"), ("flat", "Flat")],
        string="Aspect",
    )
    farmos_land_uuid = fields.Char(string="farmOS land UUID", help="Set in phase 3 when the polygon is drawn.")
    sequence = fields.Integer(string="Sequence", default=10)
