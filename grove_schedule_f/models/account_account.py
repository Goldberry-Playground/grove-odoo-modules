from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    grove_schedule_f_line = fields.Char(string="Schedule F line", index=True)
