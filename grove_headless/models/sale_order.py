import os

from odoo import fields, models
from odoo.exceptions import UserError

from . import shippo_client


class SaleOrder(models.Model):
    _inherit = "sale.order"

    grove_tracking_numbers = fields.Text(readonly=True, copy=False)
    grove_label_urls = fields.Text(readonly=True, copy=False)
    grove_delivery_status = fields.Char(readonly=True, copy=False)

    def action_buy_shipping_labels(self):
        """Buy one UPS Ground label per tree box via Shippo (each tree = its
        own box, per the shipping design). Idempotent-ish: refuses to run
        twice on an order that already has tracking numbers."""
        api_key = os.environ.get("SHIPPO_API_KEY", "")
        if not api_key:
            raise UserError("SHIPPO_API_KEY is not configured on this server.")
        for order in self:
            if order.grove_tracking_numbers:
                raise UserError(f"{order.name} already has labels; clear fields to re-buy.")
            partner = order.partner_shipping_id
            address = {
                "name": partner.name,
                "street1": partner.street or "",
                "street2": partner.street2 or "",
                "city": partner.city or "",
                "state": partner.state_id.code or "",
                "zip": partner.zip or "",
                "country": "US",
                "email": partner.email or "",
            }

            # ── Pass 1: validate all lines BEFORE buying anything ──────────
            # Build the purchase plan up front so a bad quantity on line N
            # never causes a partial purchase on a single order.
            purchase_plan: list[tuple[dict, str]] = []  # (payload, tier) per unit
            for line in order.order_line:
                if line.display_type or not line.product_id:
                    continue
                tmpl = line.product_id.product_tmpl_id
                if tmpl.type == "service":  # skip the shipping-charge line itself
                    continue
                tier = tmpl.grove_shipping_tier or "potted"
                qty = line.product_uom_qty
                if qty != int(qty):
                    raise UserError(
                        f"{order.name}: line '{line.product_id.display_name}' has "
                        f"non-integer quantity {qty}; trees ship one label per whole unit."
                    )
                for _ in range(int(qty)):
                    purchase_plan.append((shippo_client.build_shipment_payload(address, tier), tier))

            # ── Pass 2: buy labels, persisting after each success ──────────
            tracking, labels = [], []
            try:
                for payload, _tier in purchase_plan:
                    result = shippo_client.buy_ups_ground_label(api_key, payload)
                    tracking.append(result["tracking_number"])
                    labels.append(result["label_url"])
            except shippo_client.ShippoError as exc:
                if tracking:
                    # Persist what was ALREADY BOUGHT so money spent is never
                    # unrecorded. The idempotency guard then blocks blind
                    # retries; operator reconciles the partial purchase.
                    order.write(
                        {
                            "grove_tracking_numbers": "\n".join(tracking),
                            "grove_label_urls": "\n".join(labels),
                            "grove_delivery_status": "partial_purchase",
                        }
                    )
                raise UserError(
                    f"{order.name}: label purchase failed after {len(tracking)} "
                    f"label(s) bought (recorded on the order): {exc}"
                ) from exc

            order.write(
                {
                    "grove_tracking_numbers": "\n".join(tracking),
                    "grove_label_urls": "\n".join(labels),
                    "grove_delivery_status": "label_purchased" if tracking else False,
                }
            )
        return True
