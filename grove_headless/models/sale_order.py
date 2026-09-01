import logging
import os

from odoo import fields, models
from odoo.exceptions import UserError

from . import shippo_client
from .shipping_boxes import packing_mode
from .shipping_zones import pack_for_state, unshippable_reason

_logger = logging.getLogger(__name__)

# Terminal delivery state written by the operator "Mark shipped" signal
# (GOL-1980). Sits at the end of the grove_delivery_status lifecycle
# (label_purchased / partial_purchase → shipped).
GROVE_SHIPPED_STATUS = "shipped"

# XML id of the branded customer shipment email (Phase 3, GOL-1975). Resolved
# defensively so mark-shipped works before the template lands.
_SHIPMENT_EMAIL_TEMPLATE = "grove_headless.mail_template_order_shipped"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    grove_tracking_numbers = fields.Text(readonly=True, copy=False)
    grove_label_urls = fields.Text(readonly=True, copy=False)
    grove_delivery_status = fields.Char(readonly=True, copy=False)
    # Which carrier + ground service actually shipped each packed box, newline-
    # joined and index-aligned with grove_tracking_numbers (GOL-1906). Labels are
    # bought least-cost across UPS Ground / USPS Ground Advantage, so the carrier
    # can differ per box and per order; fulfilment and cost reconciliation read
    # these to audit the live label cost against the quoted rate table.
    grove_shipping_carriers = fields.Text(readonly=True, copy=False)
    grove_shipping_services = fields.Text(readonly=True, copy=False)

    # Fulfilment intent resolved at draft creation (GOL-1057/GOL-1933). Persisted
    # so the post-purchase chain has an unambiguous source of truth instead of
    # re-inferring "did this ship?" from the presence of a shipping line: the
    # new-order Discord/merchant alert words itself off it, and the auto-label
    # gate (GOL-1906) skips "pickup" orders (no label is ever owed on pickup).
    grove_fulfillment = fields.Selection(
        [("ship", "Shipping"), ("pickup", "Farm pickup")],
        readonly=True,
        copy=False,
    )

    # Fulfilment intent resolved at draft creation (GOL-1057/GOL-1933). Persisted
    # so the post-purchase chain has an unambiguous source of truth instead of
    # re-inferring "did this ship?" from the presence of a shipping line: the
    # new-order Discord/merchant alert words itself off it, and the auto-label
    # gate (GOL-1906) skips "pickup" orders (no label is ever owed on pickup).
    grove_fulfillment = fields.Selection(
        [("ship", "Shipping"), ("pickup", "Farm pickup")],
        readonly=True,
        copy=False,
    )

    # Stripe Checkout linkage (GOL-642). Written when a checkout session is
    # created; read by the webhook to reconcile session.completed/expired back
    # to this order. copy=False so a duplicated order never inherits a payment.
    grove_stripe_session_id = fields.Char(readonly=True, copy=False, index=True)
    grove_stripe_payment_intent = fields.Char(readonly=True, copy=False)
    # Variant ids charged as a preorder deposit at session creation, comma-
    # separated. The webhook reads this to tell a legitimate preorder (stock
    # was always short → deposit taken) apart from a true oversell (a line we
    # charged in full can no longer be fulfilled), so it only refunds the latter.
    grove_preorder_variant_ids = fields.Char(readonly=True, copy=False)
    grove_checkout_status = fields.Selection(
        [
            ("pending", "Awaiting payment"),
            ("paid", "Paid"),
            ("deposit_paid", "Deposit paid (balance due at ship)"),
            ("expired", "Checkout expired"),
            ("refunded_oversell", "Refunded (oversold)"),
        ],
        readonly=True,
        copy=False,
    )

    def _persist_label_result(self, vals):
        """Write label results through an independent cursor so they survive
        the request-transaction rollback that follows a raised UserError.
        Money spent at Shippo must never be unrecorded in Odoo."""
        self.ensure_one()
        with self.env.registry.cursor() as cr:
            self.with_env(self.env(cr=cr)).write(vals)

    def action_buy_shipping_labels(self):
        """Buy one least-cost ground label per PACKED BOX via Shippo (Box Engine
        v2: the same packer that priced the order plans the labels, so the boxes
        bought are the boxes charged). Each box races UPS Ground vs USPS Ground
        Advantage and buys the cheaper, transit-guarded (GOL-1906); the carrier
        that won is persisted per box. Idempotent-ish: refuses to run twice on
        an order that already has tracking numbers."""
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

            # ── Pass 1: validate all lines and pack BEFORE buying anything ─
            # Build the purchase plan up front so a bad quantity on line N
            # never causes a partial purchase on a single order.
            items: list[tuple[str, int, float]] = []  # (tier, length_class, qty)
            for line in order.order_line:
                if line.display_type or not line.product_id:
                    continue
                tmpl = line.product_id.product_tmpl_id
                if tmpl.type == "service":  # skip the shipping-charge line itself
                    continue
                tier = line.product_id.grove_effective_shipping_tier or "potted"
                qty = line.product_uom_qty
                if qty != int(qty):
                    raise UserError(
                        f"{order.name}: line '{line.product_id.display_name}' has "
                        f"non-integer quantity {qty}; trees pack per whole unit."
                    )
                items.append((tier, int(tmpl.grove_tree_length or "20"), qty))
            reason = unshippable_reason(items)
            if reason:
                raise UserError(f"{order.name}: {reason}")
            mode = packing_mode(fields.Date.context_today(order))
            plan = pack_for_state(address["state"], items, mode)
            if plan is None:
                raise UserError(
                    f"{order.name}: cannot plan boxes for '{address['state']}' — "
                    "destination or a line is outside the configured rate table."
                )
            purchase_plan: list[tuple[dict, str]] = []  # (payload, box_id) per box
            for pb in plan:
                purchase_plan.append(
                    (shippo_client.build_shipment_payload(address, pb.box_id, pb.count, mode), pb.box_id)
                )

            # ── Pass 2: buy labels, persisting after each success ──────────
            # Each label is committed through an independent cursor immediately
            # after purchase, so money spent at Shippo is recorded even if a
            # subsequent label fails and the request transaction rolls back.
            tracking, labels, carriers, services = [], [], [], []
            try:
                for payload, _box_id in purchase_plan:
                    result = shippo_client.buy_cheapest_ground_label(api_key, payload)
                    tracking.append(result["tracking_number"])
                    labels.append(result["label_url"])
                    carriers.append(result.get("carrier") or "")
                    services.append(result.get("servicelevel") or "")
                    order._persist_label_result(
                        {
                            "grove_tracking_numbers": "\n".join(tracking),
                            "grove_label_urls": "\n".join(labels),
                            "grove_shipping_carriers": "\n".join(carriers),
                            "grove_shipping_services": "\n".join(services),
                            "grove_delivery_status": "label_purchased",
                        }
                    )
            except shippo_client.ShippoError as exc:
                if tracking:
                    # Labels already bought (and individually persisted above);
                    # mark partial so the idempotency guard surfaces the problem.
                    _logger.error(
                        "Shippo partial purchase on %s: bought tracking numbers %s before failure: %s",
                        order.name,
                        tracking,
                        exc,
                    )
                    order._persist_label_result(
                        {
                            "grove_tracking_numbers": "\n".join(tracking),
                            "grove_label_urls": "\n".join(labels),
                            "grove_shipping_carriers": "\n".join(carriers),
                            "grove_shipping_services": "\n".join(services),
                            "grove_delivery_status": "partial_purchase",
                        }
                    )
                raise UserError(
                    f"{order.name}: label purchase failed after {len(tracking)} "
                    f"label(s) bought (recorded on the order): {exc}"
                ) from exc

        return True

    def _grove_mark_shipped(self, actor=None):
        """Idempotently mark this order shipped and fire the customer shipment
        email EXACTLY ONCE (GOL-1980).

        "Shipped" is a terminal delivery state: a repeat call — an operator
        double-click, a retried request — is a safe no-op that neither
        re-transitions nor re-sends the customer email. The row is locked
        ``FOR UPDATE`` so two concurrent requests serialise and only the first
        crosses the transition (the second blocks, then reads ``shipped`` and
        no-ops), so "double-click ≠ double-send" holds under real concurrency,
        not just at human speed (GOL-1975 guard).

        ``actor`` is the operator's Discord id, stamped into the chatter for the
        audit trail. Returns ``{"newly_shipped": bool}`` — True only when THIS
        call performed the transition.
        """
        self.ensure_one()
        # Serialise concurrent mark-shipped requests on this exact order. The
        # lock is read INSIDE this transaction; a second request blocks here
        # until we commit, then sees the terminal state below and no-ops.
        self.env.cr.execute(
            "SELECT grove_delivery_status FROM sale_order WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        current = row[0] if row else self.grove_delivery_status
        if current == GROVE_SHIPPED_STATUS:
            return {"newly_shipped": False}

        self.write({"grove_delivery_status": GROVE_SHIPPED_STATUS})
        if actor:
            # Audit: who signalled the shipment (Discord operator id). This is an
            # internal chatter note, not a channel post, so mentions stay inert.
            self.message_post(body=f"Marked shipped by operator {actor} (Discord bridge).")
        # Phase 3 (GOL-1975): the branded "your order has shipped" email. Fired
        # only on the real transition, so a double-click never double-emails.
        self._grove_send_shipment_email()
        return {"newly_shipped": True}

    def _grove_send_shipment_email(self):
        """Send the branded customer shipment email (Phase 3 integration point,
        GOL-1975).

        The Phase-3 sibling owns the template body; this is the single call site
        that fires it on the shipped transition. Best-effort and guarded: the
        template is looked up by xml id and skipped-with-a-log when it is not yet
        installed, so marking an order shipped (and its Odoo write) can never
        fail because Phase 3 has not landed. A send failure is likewise swallowed
        — the shipment state is the source of truth; the email is a notification.
        """
        self.ensure_one()
        template = self.env.ref(_SHIPMENT_EMAIL_TEMPLATE, raise_if_not_found=False)
        if not template:
            _logger.info(
                "grove_headless: %s marked shipped but the Phase-3 shipment email "
                "template (%s) is not installed yet; skipping customer email.",
                self.name,
                _SHIPMENT_EMAIL_TEMPLATE,
            )
            return
        try:
            template.sudo().send_mail(self.id, force_send=False)
        except Exception:  # noqa: BLE001 — notification is best-effort
            _logger.warning("grove_headless: shipment email send failed for %s", self.name, exc_info=True)
