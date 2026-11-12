"""grove.consult.practice — the seven seeded syntropic practices (spec C.2).

A practice is a planting pattern: which strata it plants, how far apart the rows
and plants sit, what the site needs, the infrastructure it assumes, and the
retail track it maps to. Phase 2 ships the model and its keys; the full table
(strata lists, spacings, site needs, infrastructure defaults) comes from the
draft's section 6.7 with the seed data (GOL-3575 follow-up). Silvopasture lists
no shrub stratum; forest farming and mycoforestry force shade-tolerant species.
"""

from odoo import fields, models


class GroveConsultPractice(models.Model):
    _name = "grove.consult.practice"
    _description = "Grove consult practice"
    _order = "name, id"

    key = fields.Char(string="Key", required=True, index=True)
    name = fields.Char(string="Name", required=True, translate=True)
    strata = fields.Json(string="Strata", help="List of the stratum keys this practice plants.")
    row_spacing_m = fields.Float(string="Row spacing (m)")
    in_row_spacing_m = fields.Float(string="In-row spacing (m)")
    site_needs = fields.Text(string="Site needs")
    infrastructure_defaults = fields.Json(
        string="Infrastructure defaults",
        help="List of infrastructure keys with unit and a placeholder rate.",
    )
    retail_track = fields.Char(string="Retail track")
    retail_subgoal = fields.Char(string="Retail subgoal")
    notes = fields.Text(string="Notes")
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        ("grove_consult_practice_key_uniq", "unique(key)", "A consult practice key must be unique."),
    ]
