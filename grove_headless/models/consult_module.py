"""grove.consult.module — the twelve seeded goal modules (consult phase 2, spec C.1).

A module is a reusable chunk of a syntropic design: an objective, the steps that
reach it, the results to expect, and a section template the phase-3 generator
fills in. Phase 2 ships the model and its seed keys; the section_template prose
and the consortium_rules are drafted later (the draft's section 6.6 copy lands
with the seed data, GOL-3575 follow-up).
"""

from odoo import fields, models


class GroveConsultModule(models.Model):
    _name = "grove.consult.module"
    _description = "Grove consult goal module"
    _order = "name, id"

    key = fields.Char(string="Key", required=True, index=True)
    name = fields.Char(string="Name", required=True, translate=True)
    objective = fields.Text(string="Objective")
    approach = fields.Text(string="Approach", help="The steps that reach the objective.")
    expected_results = fields.Text(string="Expected results")
    section_title = fields.Char(string="Section title")
    section_template = fields.Text(
        string="Section template",
        help="Prose with {placeholders} the phase-3 generator fills in.",
    )
    consortium_rules = fields.Json(string="Consortium rules", help="Phase-3 rules; empty in phase 2.")
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        ("grove_consult_module_key_uniq", "unique(key)", "A consult module key must be unique."),
    ]
