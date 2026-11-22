# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = "project.project"

    grove_is_aspect = fields.Boolean(
        string="Aspect of the business",
        help=(
            "A recurring aspect of a business (watering, packing, orchard, shop, ...). "
            "Every hour and dollar lands on the aspect's analytic account. Seeded, "
            "one per company, by scripts/setup_ops.py."
        ),
        default=False,
    )
    grove_is_build = fields.Boolean(
        string="Build project",
        help=(
            "A time-boxed build with milestones and a budget (the footpad regrade). "
            "Distinct from an aspect, which is ongoing."
        ),
        default=False,
    )
