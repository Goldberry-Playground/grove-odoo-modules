"""grove.planner.rule — the scoring weights the planner engine reads from the
feed (GOL-3549, planner winter beta spec B.1).

The shipped order-builder engine used eight hard-coded constants. This model
holds them as editable, tracked rows so an operator (or a tester proposal that
a human accepts) can tune a weight without a deploy; the feed serves them as
``rules: {key: value}`` for scope ``retail`` and the engine keeps the constants
as the fallback for any missing key. Seeded (data/planner_rules.xml) from the
exact constants so the first feed changes nothing — ``value`` is ``tracking``
so its chatter is the changelog.
"""

from odoo import api, fields, models

from ..services.planner.rules import PLANNER_RULE_KEYS

# Human labels for the Selection display; the tester-facing label/hint live in
# the ``label``/``hint`` columns (seeded per row), this is just the admin UI.
_KEY_LABELS = {
    "priority_weight": "Priority weight",
    "years_weight": "Years-to-fruit weight",
    "marginal_penalty": "Marginal-slot penalty",
    "season_bonus": "In-season bonus",
    "wildlife_bonus": "Wildlife bonus",
    "nitrogen_bonus": "Nitrogen-fixer bonus",
    "timing_fast": "Fast-timing bonus",
    "timing_patient": "Patient-timing bonus",
}


class GrovePlannerRule(models.Model):
    _name = "grove.planner.rule"
    _description = "Planner scoring rule (engine weight served by the feed)"
    _inherit = ["mail.thread"]
    _order = "scope, key"
    _rec_name = "label"

    key = fields.Selection(
        [(k, _KEY_LABELS[k]) for k in PLANNER_RULE_KEYS],
        string="Key",
        required=True,
    )
    scope = fields.Selection(
        [("retail", "Retail"), ("consult", "Consult")],
        string="Scope",
        required=True,
        default="retail",
    )
    value = fields.Float(string="Value", required=True, tracking=True)
    label = fields.Char(string="Label", help="Shown to testers on the what-if slider.")
    hint = fields.Char(string="Hint", help="One line of guidance shown to testers.")

    # Odoo 19 dropped the `_sql_constraints` list attribute (it warns and never
    # creates the constraint); declared with the modern `models.Constraint` so
    # the UNIQUE is created on `-u grove_headless`.
    _key_scope_uniq = models.Constraint(
        "unique(key, scope)",
        "A planner rule key must be unique within a scope.",
    )

    @api.model
    def rules_for_scope(self, scope="retail"):
        """``{key: value}`` for a scope, for the planner catalog feed (spec A.3).

        Only keys that exist as rows are returned; the storefront engine falls
        back to its own constants (== the seed, PLANNER_RULE_DEFAULTS) for any
        missing key, so a half-populated table never breaks scoring.
        """
        rows = self.sudo().search([("scope", "=", scope)])
        return {row.key: row.value for row in rows}
