"""Surface the order's fulfilment intent on the transfer (GOL-1933 follow-up).

Warehouse staff work pickings, not sale orders — the transfer screen is where
"is this a farm pickup or a shipment?" must be answered at a glance, so the
sale order's persisted ``grove_fulfillment`` is mirrored here and rendered as
a badge/column/filter by views/fulfillment_views.xml.
"""

from odoo import fields, models


class StockPicking(models.Model):
    _inherit = "stock.picking"

    # Stored (not just related-read) so the transfers list can filter and group
    # by it. Safe to store: the webhook writes the sale order's value before the
    # order is confirmed, and confirmation is what spawns the picking, so the
    # source is always populated first. Pickings with no sale order (receipts,
    # internal moves) and legacy orders that predate the field stay empty.
    grove_fulfillment = fields.Selection(
        related="sale_id.grove_fulfillment",
        store=True,
        string="Fulfilment",
    )
