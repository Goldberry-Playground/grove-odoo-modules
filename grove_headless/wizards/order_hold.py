"""Operator "Hold order" wizard on the Odoo order.

Asks for a date and a reason (the date is pre-filled with the order's suggested
hold date), then writes through ``action_grove_set_hold`` so its checks (reason
required, not finished, not in the past) and its chatter apply.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class GroveOrderHold(models.TransientModel):
    _name = "grove.order.hold"
    _description = "Put an order on hold"

    order_id = fields.Many2one("sale.order", string="Order", required=True, readonly=True, ondelete="cascade")
    hold_until = fields.Date(string="Hold until", required=True)
    reason = fields.Char(string="Why", required=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if self.env.context.get("active_model") == "sale.order" and self.env.context.get("active_id"):
            order = self.env["sale.order"].browse(self.env.context["active_id"]).exists()
            if order:
                values["order_id"] = order.id
                values["hold_until"] = order._grove_suggested_hold()
        return values

    def action_confirm(self):
        self.ensure_one()
        if not (self.reason or "").strip():
            raise UserError("Enter a reason for the hold.")
        self.order_id.action_grove_set_hold(self.hold_until, self.reason, self.env.user.name)
        return {"type": "ir.actions.act_window_close"}
