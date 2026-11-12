"""crm.lead consult extensions (spec C.5).

The consult pipeline rides the existing CRM lead. These fields carry the consult
stage, the service step in use, how hands-on the client wants us, their own words
on goals and what they already have, the goal modules and design areas, the
property and per-client stock location, and the cached travel estimate (C.7).
"""

from odoo import fields, models


class CrmLead(models.Model):
    _inherit = "crm.lead"

    grove_consult_stage = fields.Selection(
        [
            ("intake", "Intake"),
            ("site", "Site"),
            ("design", "Design"),
            ("quote", "Quote"),
            ("install", "Install"),
            ("stewardship", "Stewardship"),
            ("archived", "Archived"),
        ],
        string="Consult stage",
        index=True,
    )
    grove_consult_step = fields.Many2one(
        "product.product", string="Consult step", help="The service product in use."
    )
    grove_hands_on = fields.Selection(
        [("we_install", "We install"), ("they_install", "They install"), ("plan_only", "Plan only")],
        string="Hands on",
    )
    grove_goals = fields.Text(string="Goals", help="The client's own words.")
    grove_existing = fields.Text(string="Existing", help="What the client already has.")
    grove_module_ids = fields.Many2many(
        "grove.consult.module",
        "grove_consult_lead_module_rel",
        "lead_id",
        "module_id",
        string="Goal modules",
    )
    grove_area_ids = fields.One2many("grove.consult.area", "lead_id", string="Design areas")
    grove_property_uuid = fields.Char(string="Property UUID")
    grove_stock_location_id = fields.Many2one("stock.location", string="Per-client stock location")
    grove_travel_estimate = fields.Json(string="Travel estimate", help="km, hours, billable hours (C.7).")
