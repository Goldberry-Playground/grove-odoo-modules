from odoo import api, models
from odoo.exceptions import AccessError

from .account_bank_statement_line import _grove_is_agent


class AccountReconcileModel(models.Model):
    _inherit = "account.reconcile.model"

    def _grove_check_agent_trigger(self, vals):
        if _grove_is_agent(self.env) and vals.get("trigger") == "auto_reconcile":
            raise AccessError(self.env._("Grove Agents cannot create or enable auto-reconcile models."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._grove_check_agent_trigger(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._grove_check_agent_trigger(vals)
        return super().write(vals)
