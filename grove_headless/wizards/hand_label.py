"""Record a hand-bought shipping label on an order (GOL-2895).

Josh buys a sold-out deposit order's label in Pirate Ship directly (no Odoo
batch, so there is no ``grove.label.batch`` line to reconcile against). This
wizard — reached from the sale.order *Action* menu — captures the tracking
number, carrier and ACTUAL cost and advances the order to ``label_purchased``
(or, when the order already has a label, replaces it: a reprint or a re-buy
after a Pirate Ship refund)
via ``sale.order.action_grove_record_hand_label``, from where the normal ship
path (mark-shipped → settle → one shipment email) takes over. The carrier is
inferred from the tracking number (a ``1Z`` prefix is UPS) and stays editable.

A validation failure (bad/duplicate tracking, order not awaiting a label) raises
``UserError``, which Odoo shows in the wizard dialog — nothing is written.
"""

from odoo import api, fields, models


class GroveHandLabel(models.TransientModel):
    _name = "grove.hand.label"
    _description = "Record a hand-bought shipping label on an order"

    order_id = fields.Many2one(
        "sale.order",
        required=True,
        readonly=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    tracking_number = fields.Char(required=True)
    carrier = fields.Selection(
        [("UPS", "UPS"), ("USPS", "USPS")],
        required=True,
        help="Inferred from the tracking number (a 1Z prefix is UPS); override if needed.",
    )
    actual_cost = fields.Float(
        string="Actual shipping cost (USD)",
        help="What the label actually cost — recorded for ship-time settlement/reconciliation.",
    )

    @api.onchange("tracking_number")
    def _onchange_tracking_number(self):
        """Default the carrier from the tracking number: a 1Z-prefixed label is
        unambiguously UPS (GOL-2895). Only fills a blank carrier so an operator
        override is never clobbered as they finish typing the number."""
        if not self.carrier and (self.tracking_number or "").strip().upper().startswith("1Z"):
            self.carrier = "UPS"

    def action_record(self):
        self.ensure_one()
        order = self.order_id
        if order.grove_tracking_numbers:
            # The order already has a label: this is a reprint or a re-buy after a
            # Pirate Ship refund. Replace it rather than refusing.
            old = order.grove_tracking_numbers.replace("\n", ", ")
            order.action_grove_replace_label(
                tracking_number=self.tracking_number,
                carrier=self.carrier,
                actual_cost=self.actual_cost,
                source="hand_label",
            )
            message = f"Replaced label {old} with {self.carrier} {self.tracking_number} (${self.actual_cost:.2f})."
        else:
            order.action_grove_record_hand_label(
                tracking_number=self.tracking_number,
                carrier=self.carrier,
                actual_cost=self.actual_cost,
            )
            message = (
                f"Recorded {self.carrier} label {self.tracking_number} "
                f"(${self.actual_cost:.2f}); order advanced to label purchased."
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": order.name,
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
