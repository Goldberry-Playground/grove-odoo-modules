import logging
import os

from odoo import fields, models
from odoo.exceptions import UserError

from . import shippo_client
from .shipping_boxes import packing_mode
from .shipping_zones import pack_for_state, unshippable_reason

_logger = logging.getLogger(__name__)


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
            ("settled", "Settled (balance charged at ship)"),
            ("settlement_failed", "Shipped — settlement failed"),
            ("expired", "Checkout expired"),
            ("refunded_oversell", "Refunded (oversold)"),
        ],
        readonly=True,
        copy=False,
    )

    # Ship-time settlement linkage (GOL-2053). A deposit-only preorder charges
    # ONLY grove_amount_charged_today at checkout; the balance (tree prices +
    # ACTUAL shipping + recomputed WV tax) is captured off-session at ship.
    #
    # grove_amount_charged_today  — dollars actually taken by the checkout
    #   session (persisted at session creation) so settlement charges exactly
    #   order.amount_total − this, never a re-derived guess.
    # grove_actual_shipping_cost  — summed cost of the labels actually bought
    #   (Shippo `amount` per box), so settlement bills the REAL packed cost, not
    #   the stale quoted rate table.
    # grove_stripe_customer / grove_stripe_payment_method — the saved card the
    #   off-session charge runs against (customer from the checkout session, the
    #   method resolved from the deposit intent at settlement).
    # grove_settlement_payment_intent — the off-session balance charge, for audit
    #   and so a re-trigger references the same object.
    # grove_settlement_attempts — settlement tries so far; the retry cron stops
    #   auto-charging a declined card after grove_headless.settlement_max_retries.
    grove_amount_charged_today = fields.Monetary(readonly=True, copy=False)
    grove_actual_shipping_cost = fields.Monetary(readonly=True, copy=False)
    grove_stripe_customer = fields.Char(readonly=True, copy=False)
    grove_stripe_payment_method = fields.Char(readonly=True, copy=False)
    grove_settlement_payment_intent = fields.Char(readonly=True, copy=False)
    grove_settlement_attempts = fields.Integer(readonly=True, copy=False, default=0)

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
            actual_cost = 0.0  # summed Shippo label `amount` — the REAL shipping cost
            try:
                for payload, _box_id in purchase_plan:
                    result = shippo_client.buy_cheapest_ground_label(api_key, payload)
                    tracking.append(result["tracking_number"])
                    labels.append(result["label_url"])
                    carriers.append(result.get("carrier") or "")
                    services.append(result.get("servicelevel") or "")
                    # Shippo returns `amount` as a decimal string; treat a
                    # missing/garbage amount as 0 rather than crash a purchased
                    # label — settlement under-bills, never fails, on bad data.
                    try:
                        actual_cost += float(result.get("amount") or 0.0)
                    except (TypeError, ValueError):
                        _logger.warning(
                            "Shippo label on %s returned unparseable amount %r; treating as 0",
                            order.name,
                            result.get("amount"),
                        )
                    order._persist_label_result(
                        {
                            "grove_tracking_numbers": "\n".join(tracking),
                            "grove_label_urls": "\n".join(labels),
                            "grove_shipping_carriers": "\n".join(carriers),
                            "grove_shipping_services": "\n".join(services),
                            "grove_actual_shipping_cost": actual_cost,
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
                            "grove_actual_shipping_cost": actual_cost,
                            "grove_delivery_status": "partial_purchase",
                        }
                    )
                raise UserError(
                    f"{order.name}: label purchase failed after {len(tracking)} "
                    f"label(s) bought (recorded on the order): {exc}"
                ) from exc

            # Every box is bought and the ACTUAL shipping cost is known, so the
            # deferred balance (tree prices + real shipping + recomputed WV tax)
            # can settle off-session now (GOL-2053). Best-effort by contract: a
            # decline or gateway error must NEVER roll back the labels we just
            # paid Shippo for — settle_order_at_ship swallows its own failures
            # into grove_checkout_status + a dunning path.
            order._grove_settle_at_ship()

        return True

    def _grove_settle_at_ship(self):
        """Capture the deferred preorder balance off-session at ship time.

        Thin model entry point over the controller's settlement engine (which
        owns the Stripe/tenant/tax/notify helpers). Deferred import breaks the
        controller→models load cycle. Never raises: the caller has already
        shipped, so a settlement failure is recorded, not fatal."""
        self.ensure_one()
        from ..controllers.main import settle_order_at_ship

        try:
            return settle_order_at_ship(self.env, self)
        except Exception:  # noqa: BLE001 — settlement must never fail the ship
            _logger.exception("Ship-time settlement crashed for %s", self.name)
            return "settlement_error"

    def _cron_retry_settlements(self):
        """Re-attempt every shipped-but-unsettled order whose card can still be
        auto-charged (GOL-2053 retry policy). Orders that have exhausted
        grove_headless.settlement_max_retries are left for manual re-trigger and
        stay in the ops queue via their Discord escalation."""
        max_retries = int(self.env["ir.config_parameter"].sudo().get_param("grove_headless.settlement_max_retries", 3))
        stuck = self.sudo().search(
            [
                ("grove_checkout_status", "=", "settlement_failed"),
                ("grove_settlement_attempts", "<", max_retries),
            ]
        )
        for order in stuck:
            order._grove_settle_at_ship()
        return True
