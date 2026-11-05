from odoo import api, fields, models


class CloseNote(models.Model):
    _name = "grove.close.note"
    _description = "Month close note"
    _order = "month desc"

    name = fields.Char(compute="_compute_name")
    month = fields.Date(required=True, help="First day of the closed month.")
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    summary = fields.Text(required=True)

    @api.depends("month", "company_id")
    def _compute_name(self):
        for note in self:
            month = note.month.strftime("%Y-%m") if note.month else ""
            note.name = f"{month} {note.company_id.name or ''}".strip()
