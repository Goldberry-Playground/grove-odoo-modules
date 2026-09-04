import logging
import os

from odoo import api, fields, models
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

    # ── Terminal fulfilment state machine (GOL-1981) ────────────────────────
    # Odoo is the system of record for "what is outstanding". Payment
    # (grove_checkout_status) and raw label substatus (grove_delivery_status)
    # answer *how far along the money/label is*; neither reaches a terminal
    # answer for all three fulfilment modes (a pickup order never buys a label;
    # a preorder waits on a wave). This field is the single, mode-spanning
    # lifecycle each order walks to its own terminal event:
    #   ship     : awaiting_label -> label_purchased -> shipped -> delivered
    #   pickup   : reserved -> collected
    #   preorder : deposit_paid -> wave_assigned -> label_purchased -> shipped
    #              -> delivered   (once its wave opens it follows the ship path)
    # Terminal = {delivered, collected, cancelled}. `grove_fulfillment_stage`
    # is the queryable answer; this raw field is the operator/event WATERMARK,
    # written only by the transition methods below. While it is unset the stage
    # is DERIVED from payment + mode + label substatus, so the pre-fulfilment
    # stages need no webhook wiring — the first real event (label bought,
    # shipped, collected, wave-assigned) sets the watermark and drives it after.
    grove_fulfillment_state = fields.Selection(
        [
            ("awaiting_payment", "Awaiting payment"),
            ("deposit_paid", "Deposit paid (preorder)"),
            ("wave_assigned", "Assigned to ship wave"),
            ("awaiting_label", "Paid — awaiting label"),
            ("reserved", "Reserved for farm pickup"),
            ("label_purchased", "Label purchased"),
            ("shipped", "Shipped (in transit)"),
            ("delivered", "Delivered"),
            ("collected", "Collected at farm"),
            ("cancelled", "Cancelled / refunded"),
        ],
        readonly=True,
        copy=False,
        help="Operator/event watermark for the fulfilment lifecycle. Unset "
        "until the first real event; the queryable state is grove_fulfillment_stage.",
    )
    # The single queryable lifecycle state: the watermark if set, otherwise
    # derived from payment + mode + label substatus. Stored + indexed so
    # "what is outstanding" is a one-line domain query.
    grove_fulfillment_stage = fields.Selection(
        selection=lambda self: self._fields["grove_fulfillment_state"].selection,
        compute="_compute_grove_fulfillment_stage",
        store=True,
        index=True,
        readonly=True,
        copy=False,
    )
    grove_is_outstanding = fields.Boolean(
        compute="_compute_grove_fulfillment_stage",
        store=True,
        index=True,
        readonly=True,
        copy=False,
        help="True until the order reaches its terminal state "
        "(delivered / collected / cancelled). The order-ops outstanding query.",
    )

    # Terminal states — an order here is no longer outstanding and no further
    # transition is legal.
    _GROVE_TERMINAL_STATES = ("delivered", "collected", "cancelled")
    # Which prior states each transition may legally advance FROM. A transition
    # requested from any other state is rejected (logged, no write) so a stray
    # signal can never skip the machine (e.g. delivered before shipped).
    _GROVE_TRANSITIONS = {
        "wave_assigned": ("deposit_paid",),
        "label_purchased": ("awaiting_label", "wave_assigned"),
        "shipped": ("awaiting_label", "label_purchased"),
        "delivered": ("shipped",),
        "collected": ("reserved",),
    }

    @api.depends(
        "grove_fulfillment_state",
        "grove_checkout_status",
        "grove_fulfillment",
        "grove_delivery_status",
    )
    def _compute_grove_fulfillment_stage(self):
        for order in self:
            stage = order.grove_fulfillment_state or order._grove_derived_stage()
            order.grove_fulfillment_stage = stage
            order.grove_is_outstanding = stage not in order._GROVE_TERMINAL_STATES

    def _grove_derived_stage(self):
        """Pre-fulfilment stage implied by payment + mode + label substatus,
        used while the event watermark (grove_fulfillment_state) is unset."""
        self.ensure_one()
        checkout = self.grove_checkout_status
        if checkout in ("expired", "refunded_oversell"):
            return "cancelled"
        if checkout == "deposit_paid":
            return "deposit_paid"
        if checkout == "paid":
            if self.grove_fulfillment == "pickup":
                return "reserved"
            if self.grove_delivery_status == "label_purchased":
                return "label_purchased"
            return "awaiting_label"
        return "awaiting_payment"

    def _grove_advance_state(self, target, *, source="operator", operator=None, note=None):
        """Advance the fulfilment watermark to ``target`` if the transition is
        legal from the current stage. Returns True only when the state actually
        moved, so callers (Discord signal, Shippo webhook) are idempotent across
        a double-click or a duplicate transit event: a no-op returns False and
        emits no side effect (no duplicate customer email). An illegal jump is
        logged and dropped rather than written, so a stray signal cannot skip
        the machine. Records operator + source on the chatter for the audit
        trail Odoo owns as the system of record."""
        self.ensure_one()
        current = self.grove_fulfillment_stage
        if current == target:
            return False  # idempotent: already there
        allowed = self._GROVE_TRANSITIONS.get(target, ())
        if current not in allowed:
            _logger.warning(
                "Rejected fulfilment transition %s -> %s on %s (source=%s): not a legal move from %s.",
                current,
                target,
                self.name,
                source,
                current,
            )
            return False
        self.grove_fulfillment_state = target
        who = f" by {operator}" if operator else ""
        body = note or (f"Fulfilment: {current} → {target} (via {source}{who}).")
        self.message_post(body=body)
        return True

    def action_grove_mark_shipped(self, operator=None, source="operator"):
        """Mark a ship/preorder order shipped (in transit). The seam the Phase 2
        Discord 'Mark Shipped' button and the Phase 3 shipment email hang off:
        idempotent across the operator signal OR a Shippo transit event, and a
        pickup order is never a legal source here (it collects at the farm, buys
        no label, and must never send the shipment email — GOL-1981 acceptance).
        Returns True only on the real transition so the email fires exactly once."""
        self.ensure_one()
        if self.grove_fulfillment == "pickup":
            _logger.warning(
                "Refused to mark pickup order %s shipped — pickup collects at the farm and sends no shipment email.",
                self.name,
            )
            return False
        return self._grove_advance_state("shipped", source=source, operator=operator)

    def action_grove_mark_delivered(self, source="shippo"):
        """Terminal transition for ship/preorder: Shippo reports delivery."""
        self.ensure_one()
        return self._grove_advance_state("delivered", source=source)

    def action_grove_mark_collected(self, operator=None):
        """Terminal transition for pickup: operator confirms collection at the
        farm. Never buys a label and never emits the shipment email."""
        self.ensure_one()
        if self.grove_fulfillment != "pickup":
            _logger.warning("Refused to mark non-pickup order %s collected.", self.name)
            return False
        return self._grove_advance_state("collected", source="operator", operator=operator)

    def action_grove_assign_wave(self, wave_ref=None):
        """Preorder: assign a deposit-paid order to a ship wave. From here the
        wave's balance charge + label purchase put it back on the ship path."""
        self.ensure_one()
        note = f"Preorder assigned to wave {wave_ref}." if wave_ref else None
        return self._grove_advance_state("wave_assigned", source="wave", note=note)

    def grove_should_send_shipment_email(self):
        """Predicate the Phase 3 shipment notification consumes: a shipment
        email is owed for ship + preorder orders, NEVER for farm pickup. Belt
        and suspenders — a pickup order also never reaches the 'shipped' state
        (action_grove_mark_shipped refuses it), so pickup is doubly guarded."""
        self.ensure_one()
        return self.grove_fulfillment != "pickup"

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

            # All labels bought: advance the fulfilment watermark (GOL-1981).
            # For a preorder the watermark is 'wave_assigned' and this is the
            # step back onto the ship path; for a plain ship order it makes the
            # label milestone authoritative rather than derived from the
            # separately-committed grove_delivery_status. Idempotent + legal
            # from either awaiting_label or wave_assigned.
            order._grove_advance_state("label_purchased", source="shippo")

        return True
