"""crm.lead consult extensions (spec C.5).

The consult pipeline rides the existing CRM lead. These fields carry the consult
stage, the service step in use, how hands-on the client wants us, their own words
on goals and what they already have, the goal modules and design areas, the
property and per-client stock location, the cached travel estimate (C.7), the
date the lead entered its stage and the site-walk checklist (GOL-4093).
"""

from odoo import api, fields, models


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
    grove_consult_step = fields.Many2one("product.product", string="Consult step", help="The service product in use.")
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
    grove_stage_since = fields.Datetime(
        string="Stage since",
        readonly=True,
        help="When the lead last entered its current consult stage. Maintained here "
        "rather than read from the chatter so the pipeline can serve days-in-stage "
        "without a tracking lookup per lead (GOL-4093).",
    )
    grove_walk_checklist = fields.Json(
        string="Site-walk checklist",
        help="The design-tab walk checklist: a flat {key: bool} map keyed by the app's WALK_ITEMS (GOL-4093).",
    )

    # ── stage-since bookkeeping (GOL-4093) ───────────────────────────────────
    # A dedicated stamp, set whenever the consult stage actually changes, is the
    # simplest fit: ``grove_consult_stage`` is not a tracked field, and tracking
    # it just to read the last change back per lead would cost a mail lookup on
    # every pipeline row. This keeps the pipeline read to one query.
    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        for vals in vals_list:
            if vals.get("grove_consult_stage") and not vals.get("grove_stage_since"):
                vals["grove_stage_since"] = now
        return super().create(vals_list)

    def write(self, vals):
        new_stage = vals.get("grove_consult_stage")
        if new_stage and "grove_stage_since" not in vals:
            changing = self.filtered(lambda lead: lead.grove_consult_stage != new_stage)
            res = super().write(vals)
            if changing:
                super(CrmLead, changing).write({"grove_stage_since": fields.Datetime.now()})
            return res
        return super().write(vals)
