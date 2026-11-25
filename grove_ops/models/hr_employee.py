# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    grove_aspect_project_ids = fields.Many2many(
        comodel_name="project.project",
        relation="grove_employee_aspect_project_rel",
        column1="employee_id",
        column2="project_id",
        string="Aspects covered",
        domain=[("grove_is_aspect", "=", True)],
        help=(
            "The aspect projects this person covers. The planner (Phase 3) reads this "
            "to spread recurring work across the crew. Only aspect projects are selectable."
        ),
    )
