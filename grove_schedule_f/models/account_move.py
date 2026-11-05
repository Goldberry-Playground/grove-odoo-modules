from odoo import api, models
from odoo.exceptions import AccessError

from .account_bank_statement_line import _grove_is_agent

AGENT_BLOCKED_MOVE_FIELDS = ("state", "auto_post", "auto_post_until")


class AccountMove(models.Model):
    _inherit = "account.move"

    def _grove_agent_locked(self) -> bool:
        """True when any record is posted or armed for the auto-post cron (agents must not touch either)."""
        return any(move.state == "posted" or move.auto_post != "no" for move in self)

    @api.model_create_multi
    def create(self, vals_list):
        if _grove_is_agent(self.env):
            # State is forced to draft and the auto-post cron is disarmed, so only a human can ever post.
            vals_list = [{**vals, "state": "draft", "auto_post": "no", "auto_post_until": False} for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if _grove_is_agent(self.env):
            if any(key in vals for key in AGENT_BLOCKED_MOVE_FIELDS) or self._grove_agent_locked():
                raise AccessError(self.env._("Grove Agents cannot change posted entries or post entries."))
        return super().write(vals)

    def action_post(self):
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot post entries."))
        return super().action_post()
