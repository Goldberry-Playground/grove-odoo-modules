from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    grove_enterprise_required = fields.Boolean(string="Enterprise tag required")
    grove_is_mirror = fields.Boolean(string="Intercompany mirror journal")
