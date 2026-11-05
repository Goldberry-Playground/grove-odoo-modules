from odoo import api, models
from odoo.exceptions import AccessError

from .account_bank_statement_line import _grove_is_agent


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model_create_multi
    def create(self, vals_list):
        if _grove_is_agent(self.env):
            vals_list = [{**vals, "state": "draft"} for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if _grove_is_agent(self.env):
            if "state" in vals or any(move.state == "posted" for move in self):
                raise AccessError(self.env._("Grove Agents cannot change posted entries or post entries."))
        return super().write(vals)

    def action_post(self):
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot post entries."))
        return super().action_post()


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        if _grove_is_agent(self.env):
            move_ids = {vals["move_id"] for vals in vals_list if vals.get("move_id")}
            if any(move.state == "posted" for move in self.env["account.move"].browse(move_ids)):
                raise AccessError(self.env._("Grove Agents cannot change posted entries."))
        return super().create(vals_list)

    def write(self, vals):
        if _grove_is_agent(self.env) and any(line.move_id.state == "posted" for line in self):
            raise AccessError(self.env._("Grove Agents cannot change posted entries."))
        return super().write(vals)
