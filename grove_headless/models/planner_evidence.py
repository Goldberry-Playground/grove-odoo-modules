"""grove.planner.evidence — evidence for or against a planner fact (GOL-3549,
planner winter beta spec B.3).

Written by ``POST /grove/api/v1/planner/dispute`` (a tester, token-gated) and,
in spring, by the post-season check-in email (``survival`` source) — the model
accepts survival reports today so that email lands without a schema change
(spec non-goals). The nightly job counts un-reviewed contradictions per product
and fact; three or more flags the product and clears its reviewed sign-off
(spec B.4). Staff mark a row reviewed from the list view.
"""

from odoo import fields, models

from ..services.planner.rules import EVIDENCE_FACTS


class GrovePlannerEvidence(models.Model):
    _name = "grove.planner.evidence"
    _description = "Planner fact evidence (dispute / survival report)"
    _order = "create_date desc, id desc"

    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Product",
        required=True,
        ondelete="cascade",
        index=True,
    )
    fact = fields.Selection(
        [(f, f) for f in EVIDENCE_FACTS],
        string="Fact",
        required=True,
        index=True,
    )
    contradicts = fields.Boolean(
        string="Contradicts",
        default=True,
        help="True when this evidence says the recorded fact is wrong.",
    )
    source = fields.Selection(
        [
            ("tester", "Tester"),
            ("survival", "Survival report"),
            ("staff", "Staff"),
            ("log", "Log"),
        ],
        string="Source",
        required=True,
        default="tester",
    )
    note = fields.Text(string="Note")
    by = fields.Char(string="By", help="Tester name from the token, or the staff user.")
    zone = fields.Integer(string="USDA Zone")
    reviewed = fields.Boolean(string="Reviewed", index=True)
    reviewed_by = fields.Many2one("res.users", string="Reviewed By", readonly=True)
    reviewed_at = fields.Datetime(string="Reviewed At", readonly=True)

    def action_mark_reviewed(self):
        """Staff sign-off: mark the selected evidence reviewed with who/when so
        the nightly contradiction tally stops counting it (spec B.3/B.4)."""
        self.write(
            {
                "reviewed": True,
                "reviewed_by": self.env.user.id,
                "reviewed_at": fields.Datetime.now(),
            }
        )
