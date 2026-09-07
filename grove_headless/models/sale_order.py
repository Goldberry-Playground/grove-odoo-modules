import logging
import os

from odoo import api, fields, models
from odoo.exceptions import UserError

from . import shippo_client
from .shipping_boxes import can_ship_bareroot, dormancy_window, packing_mode
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
        # settled / settlement_failed (GOL-2053) are post-payment ship-path
        # statuses: settlement runs after every label is bought, so the
        # watermark is normally already set — but if it is unset (legacy row,
        # partial write) a shipped order must not derive back to
        # "awaiting_payment".
        if checkout in ("paid", "settled", "settlement_failed"):
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
        # Serialise concurrent shipped signals on this exact row (GOL-1980): the
        # operator Discord button and a racing Shippo transit scan can both call
        # in at once. The second caller blocks on this FOR UPDATE until the first
        # commits, then re-reads the committed watermark below and no-ops, so
        # "double-click ≠ double-send / double-settle" holds under real
        # concurrency, not just at human speed. Invalidate the stored compute so
        # the transition check reads the freshly-locked DB value, not ORM cache.
        self.env.cr.execute("SELECT id FROM sale_order WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset(["grove_fulfillment_state", "grove_fulfillment_stage"])
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

    def _preorder_variant_id_set(self):
        """Variant ids on this order charged as a preorder deposit (GOL-1982).

        A preorder consumes a per-variant ``preorder_cap`` (GOL-1671), never
        on-hand stock, and owes no shipping label at order time (GOL-1933 guard).
        Two paths consult this set: the oversell webhook (controllers/main.py),
        which excludes preorder lines from its on-hand check unconditionally, and
        ``action_buy_shipping_labels``, which excludes them only while the ship
        wave is still closed (once wave_assigned they pack — see there). Parsing
        lives here so both paths share one source of truth for "is this a preorder
        line" instead of re-deriving it from the comma-joined field independently.
        """
        self.ensure_one()
        ids = set()
        for raw in (self.grove_preorder_variant_ids or "").split(","):
            raw = raw.strip()
            if raw.isdigit():
                ids.add(int(raw))
        return ids

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
            # Preorder lines owe no label UNTIL their ship wave opens.
            # grove_preorder_variant_ids is the permanent order-time record of
            # which variants were charged as a deposit — it is never cleared, so
            # skipping on it unconditionally would strand a preorder forever (it
            # could never ship). action_grove_assign_wave (-> wave_assigned) is
            # the signal the wave has opened and those lines rejoin the ship path
            # (label_purchased is a legal move from wave_assigned; see the
            # lifecycle comment above, and GOL-2053 settles the deferred balance
            # at the end of THIS method). So exclude preorder lines only while the
            # wave is still closed; once it is open, this method IS the preorder
            # ship + settle path and must pack them.
            skip_preorder_ids = (
                set() if order.grove_fulfillment_stage == "wave_assigned" else order._preorder_variant_id_set()
            )
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
                # USPS Ground Advantage wants a recipient contact too; pass the
                # partner's phone so a USPS label isn't left thin on
                # delivery-contact info (GOL-1906). NB: res.partner has no
                # `mobile` field in Odoo 19 (removed in 18.0), so referencing it
                # AttributeErrors on any phone-less partner — `phone` only.
                "phone": partner.phone or "",
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
                if line.product_id.id in skip_preorder_ids:
                    # Pre-wave preorder line: consumes preorder_cap, not on-hand,
                    # and owes no label yet (GOL-1982 / GOL-1933) — never pack it,
                    # even on a bareroot (shippable-tier) variant. The label is
                    # bought later, when the wave opens and the balance is charged
                    # (at which point skip_preorder_ids is empty and it packs).
                    continue
                tier = line.product_id.grove_effective_shipping_tier or "potted"
                qty = line.product_uom_qty
                if qty != int(qty):
                    raise UserError(
                        f"{order.name}: line '{line.product_id.display_name}' has "
                        f"non-integer quantity {qty}; trees pack per whole unit."
                    )
                items.append((tier, int(tmpl.grove_tree_length or "20"), qty))
            if not items:
                # Nothing shippable remains — the order is all-preorder with its
                # wave still closed (or has no real product lines). No on-hand pool
                # is decremented and no label is owed; refuse rather than silently
                # "succeed" with zero labels (which would falsely flip
                # grove_delivery_status). Once the wave opens (wave_assigned) the
                # preorder lines pack and this branch is not reached.
                raise UserError(
                    f"{order.name}: no shippable lines — all preorder (wave not open) or pickup, no label is owed."
                )
            reason = unshippable_reason(items)
            if reason:
                raise UserError(f"{order.name}: {reason}")
            today = fields.Date.context_today(order)
            # Dormancy window is Odoo-editable (GOL-1906, Josh 2026-09-07) — read
            # it from config here and inject, so the label gate tracks the same
            # dates the storefront quotes. A malformed param raises out of
            # `dormancy_window`, failing the purchase closed rather than buying an
            # underpriced label off a bad window.
            window = dormancy_window(order.env)
            # Seasonal gate (GOL-1906, Josh 2026-09-07): bareroot ships ONLY in
            # the nursery dormancy window. `unshippable_reason` above already
            # cleared any pickup-only (potted) line, so every remaining line here
            # is bareroot — the parcel about to be built (`build_shipment_payload`
            # at the declared `mode`) would be a leafed bareroot label outside the
            # window, which is impossible by policy. Fail CLOSED with a loud error
            # rather than buy a heavier-than-quoted label: such an order is a
            # preorder that ships in the next dormant wave (the deposit path routed
            # it there at checkout), so a leafed-season label attempt is an
            # operator/timing error, not a valid purchase.
            if not can_ship_bareroot(today, window):
                raise UserError(
                    f"{order.name}: bareroot shipping labels can only be bought inside "
                    "the nursery dormancy window. This order is a preorder and ships in "
                    "the next dormant wave — assign it to that wave and buy the label then."
                )
            mode = packing_mode(today, window)
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

            # All labels bought: advance the fulfilment watermark (GOL-1981).
            # For a preorder the watermark is 'wave_assigned' and this is the
            # step back onto the ship path; for a plain ship order it makes the
            # label milestone authoritative rather than derived from the
            # separately-committed grove_delivery_status. Idempotent + legal
            # from either awaiting_label or wave_assigned.
            order._grove_advance_state("label_purchased", source="shippo")

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
