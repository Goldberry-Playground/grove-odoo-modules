from odoo import api, models
from odoo.exceptions import AccessError

from .account_bank_statement_line import _grove_is_agent

AUTO = "auto_reconcile"


class AccountReconcileModel(models.Model):
    _inherit = "account.reconcile.model"

    @api.model_create_multi
    def create(self, vals_list):
        if _grove_is_agent(self.env):
            default_trigger = self.env.context.get("default_trigger")
            if any(vals.get("trigger", default_trigger) == AUTO for vals in vals_list):
                raise AccessError(self.env._("Grove Agents cannot create or enable auto-reconcile models."))
        return super().create(vals_list)

    def write(self, vals):
        if _grove_is_agent(self.env) and (vals.get("trigger") == AUTO or "auto_reconcile" in self.mapped("trigger")):
            raise AccessError(self.env._("Grove Agents cannot change auto-reconcile models."))
        return super().write(vals)

    def unlink(self):
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot delete reconcile models."))
        return super().unlink()


class AccountReconcileModelLine(models.Model):
    _inherit = "account.reconcile.model.line"

    def _grove_check_agent(self, models_):
        if _grove_is_agent(self.env) and AUTO in models_.mapped("trigger"):
            raise AccessError(self.env._("Grove Agents cannot change lines of auto-reconcile models."))

    @api.model_create_multi
    def create(self, vals_list):
        default_model_id = self.env.context.get("default_model_id")
        ids = {vals.get("model_id", default_model_id) for vals in vals_list} - {False, None}
        self._grove_check_agent(self.env["account.reconcile.model"].browse(ids))
        return super().create(vals_list)

    def write(self, vals):
        target = self.model_id
        if vals.get("model_id"):
            target |= self.env["account.reconcile.model"].browse(vals["model_id"])
        self._grove_check_agent(target)
        return super().write(vals)

    def unlink(self):
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot delete reconcile model lines."))
        return super().unlink()
