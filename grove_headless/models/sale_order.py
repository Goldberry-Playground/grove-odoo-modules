import logging
import os
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from . import carrier_tracking, shippo_client
from .shipment_email import normalize_carrier
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
    # The open/exported Pirate Ship label batch this order was rolled into
    # (GOL-2271). Set when the batch CSV is built; the round-trip tracking import
    # matches an order's rows back to it. Cleared to nothing on a cancelled batch.
    grove_label_batch_id = fields.Many2one(
        "grove.label.batch", readonly=True, copy=False, index=True, ondelete="set null"
    )

    # Carrier-tracking poll bookkeeping (GOL-2272, Pirate Ship C). Set when the
    # label milestone lands (see _grove_advance_state) so the 2-hourly poll can
    # stop chasing an order 30 days after its label was bought. The two booleans
    # keep the poll's Discord notes once-only: the cap note fires once, then the
    # order drops out of the poll domain, and the exception note fires once per
    # order rather than every 2 hours a carrier keeps reporting a problem.
    grove_label_purchased_at = fields.Datetime(readonly=True, copy=False)
    grove_carrier_poll_stopped = fields.Boolean(default=False, readonly=True, copy=False)
    grove_carrier_exception_noted = fields.Boolean(default=False, readonly=True, copy=False)
    # Customer shipment notices already sent, one "<event>:<tracking set>" key
    # per line (GOL-2429). The once-only ledger the carrier poll, Shippo webhook
    # and operator button all check, so a re-delivered event never re-emails.
    grove_shipment_notices_sent = fields.Text(readonly=True, copy=False)

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
        # Stamp the label-purchase time once, at the single chokepoint every
        # label path advances through (Shippo buy today, Pirate Ship reconcile
        # under B). The carrier-tracking poll reads this to stop chasing an order
        # 30 days after its label was bought (GOL-2272).
        if target == "label_purchased" and not self.grove_label_purchased_at:
            self.grove_label_purchased_at = fields.Datetime.now()
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

    def _grove_pack_for_label(self):
        """Carrier-neutral shipment plan for ONE order: ``(address, plan, mode)``.

        The eligibility gate (preorder wave still closed → skip those lines;
        dormancy window; unpriceable destination; non-integer qty) plus the Box
        Engine v2 packing that used to live inline in ``action_buy_shipping_labels``
        (Shippo), factored out so the Pirate Ship label batch (GOL-2271) and the
        legacy Shippo buy plan the SAME boxes at the SAME weights — the boxes
        bought are always the boxes the order was charged for. Raises ``UserError``
        with a human reason when the order cannot ship a label yet; the batch
        builder catches that to SKIP the order, the Shippo action re-raises it to
        the operator. ``address`` is Shippo-shaped (``street1``/``street2``/``zip``)
        because ``build_shipment_payload`` consumes it verbatim; the batch CSV
        builder maps those keys to its own column names."""
        self.ensure_one()
        # Preorder lines owe no label UNTIL their ship wave opens.
        # grove_preorder_variant_ids is the permanent order-time record of
        # which variants were charged as a deposit — it is never cleared, so
        # skipping on it unconditionally would strand a preorder forever (it
        # could never ship). action_grove_assign_wave (-> wave_assigned) is
        # the signal the wave has opened and those lines rejoin the ship path
        # (label_purchased is a legal move from wave_assigned). So exclude
        # preorder lines only while the wave is still closed; once it is open
        # they pack.
        skip_preorder_ids = (
            set() if self.grove_fulfillment_stage == "wave_assigned" else self._preorder_variant_id_set()
        )
        partner = self.partner_shipping_id
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
            # partner's phone so a USPS label isn't left thin on delivery-contact
            # info (GOL-1906). NB: res.partner has no `mobile` field in Odoo 19
            # (removed in 18.0), so referencing it AttributeErrors on any
            # phone-less partner — `phone` only.
            "phone": partner.phone or "",
        }

        # Validate all lines and pack BEFORE the caller buys/exports anything, so
        # a bad quantity on line N never causes a partial batch on one order.
        items: list[tuple[str, int, float]] = []  # (tier, length_class, qty)
        for line in self.order_line:
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
                    f"{self.name}: line '{line.product_id.display_name}' has "
                    f"non-integer quantity {qty}; trees pack per whole unit."
                )
            items.append((tier, int(tmpl.grove_tree_length or "20"), qty))
        if not items:
            # Nothing shippable remains — the order is all-preorder with its
            # wave still closed (or has no real product lines). No on-hand pool
            # is decremented and no label is owed; refuse rather than silently
            # "succeed" with zero labels. Once the wave opens (wave_assigned) the
            # preorder lines pack and this branch is not reached.
            raise UserError(
                f"{self.name}: no shippable lines — all preorder (wave not open) or pickup, no label is owed."
            )
        reason = unshippable_reason(items)
        if reason:
            raise UserError(f"{self.name}: {reason}")
        today = fields.Date.context_today(self)
        # Dormancy window is Odoo-editable (GOL-1906, Josh 2026-09-07) — read it
        # from config here and inject, so the label gate tracks the same dates
        # the storefront quotes. A malformed param raises out of
        # `dormancy_window`, failing closed rather than shipping an
        # underpriced/heavier-than-quoted label off a bad window.
        window = dormancy_window(self.env)
        # Seasonal gate (GOL-1906): bareroot ships ONLY in the nursery dormancy
        # window. `unshippable_reason` above already cleared any pickup-only
        # (potted) line, so every remaining line here is bareroot — a label
        # outside the window would be a leafed bareroot parcel, impossible by
        # policy. Fail CLOSED: such an order is a preorder that ships in the next
        # dormant wave (the deposit path routed it there at checkout).
        if not can_ship_bareroot(today, window):
            raise UserError(
                f"{self.name}: bareroot shipping labels can only be bought inside "
                "the nursery dormancy window. This order is a preorder and ships in "
                "the next dormant wave — assign it to that wave and buy the label then."
            )
        mode = packing_mode(today, window)
        plan = pack_for_state(address["state"], items, mode)
        if plan is None:
            raise UserError(
                f"{self.name}: cannot plan boxes for '{address['state']}' — "
                "destination or a line is outside the configured rate table."
            )
        return address, plan, mode

    def action_buy_shipping_labels(self):
        """Buy one least-cost ground label per PACKED BOX via Shippo (Box Engine
        v2: the same packer that priced the order plans the labels, so the boxes
        bought are the boxes charged). Each box races UPS Ground vs USPS Ground
        Advantage and buys the cheaper, transit-guarded (GOL-1906); the carrier
        that won is persisted per box. Idempotent-ish: refuses to run twice on
        an order that already has tracking numbers.

        NB (GOL-2271): the Pirate Ship batch flow supersedes this Shippo buy path
        as the label channel; this action + ``SHIPPO_API_KEY`` are removed when
        sub-project C retires Shippo entirely (spec §C). Kept meanwhile so the
        tested Shippo path stays live during the transition."""
        api_key = os.environ.get("SHIPPO_API_KEY", "")
        if not api_key:
            raise UserError("SHIPPO_API_KEY is not configured on this server.")
        for order in self:
            if order.grove_tracking_numbers:
                raise UserError(f"{order.name} already has labels; clear fields to re-buy.")
            address, plan, mode = order._grove_pack_for_label()
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
                    # mode-aware transit ceiling (Josh 2026-09-09): leafed trees
                    # tolerate <=3 transit days, dormant <=7 — see MAX_TRANSIT_DAYS.
                    result = shippo_client.buy_cheapest_ground_label(api_key, payload, mode=mode)
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

    # ── Carrier-tracking poll (GOL-2272, Pirate Ship C) ─────────────────────

    # How long to keep polling a shipped order before giving up and asking ops
    # to check it by hand. A ground package that has not delivered in 30 days is
    # lost or stuck, not in transit.
    _CARRIER_POLL_MAX_AGE = timedelta(days=30)

    def _grove_tracking_pairs(self):
        """(carrier_key, tracking_number) pairs for this order's boxes, carrier
        folded to the canonical "UPS"/"USPS"/"" key. Index-aligned reads of
        grove_shipping_carriers / grove_tracking_numbers (GOL-1906), same pairing
        the shipment email uses — a box with no carrier yields "" and is skipped
        by the poll (no client to pick)."""
        self.ensure_one()
        trackings = (self.grove_tracking_numbers or "").splitlines()
        carriers = (self.grove_shipping_carriers or "").splitlines()
        pairs = []
        for i, number in enumerate(trackings):
            number = number.strip()
            if not number:
                continue
            raw = carriers[i] if i < len(carriers) else ""
            pairs.append((normalize_carrier(raw), number))
        return pairs

    def _cron_poll_carrier_tracking(self):
        """Cron entry point (grove_headless.poll_carrier_tracking, every 2 h).
        Never raises: any unexpected failure is logged and swallowed so a bad
        carrier response or a schema surprise can never wedge the scheduler."""
        try:
            self._poll_carrier_tracking()
        except Exception:  # noqa: BLE001 — the cron must never raise
            _logger.exception("Carrier-tracking poll crashed; swallowed so the cron survives")

    def _poll_carrier_tracking(self):
        """Poll UPS/USPS for every order still in flight and fold the carrier's
        status into the existing delivery-status email path (GOL-2272).

        Orders in `label_purchased`/`shipped` with tracking, not yet delivered,
        not past the poll cap. For each: pick the per-box client from the stored
        carrier, map the carrier status, and apply the LEAST-advanced box status
        through `_apply_delivery_status`, which emails each shipped/out-for-
        delivery/delivered notice once and advances the order to shipped then
        the terminal delivered (GOL-2429).
        An exception status posts a silent Discord ops note (once per order); the
        30-day cap posts one note and drops the order from the poll. Per-call
        errors are logged and skipped; three consecutive auth failures for a
        carrier post one ops alert and pause that carrier until the next run."""
        from ..controllers.main import _apply_delivery_status, _notify_discord

        clients = carrier_tracking.build_clients()
        if not any(clients.values()):
            _logger.info("poll_carrier_tracking: no UPS/USPS credentials configured; nothing to poll")
            return

        orders = self.sudo().search(
            [
                ("grove_fulfillment_stage", "in", ("label_purchased", "shipped")),
                ("grove_tracking_numbers", "!=", False),
                ("grove_delivery_status", "!=", "delivered"),
                ("grove_carrier_poll_stopped", "=", False),
            ]
        )
        now = fields.Datetime.now()
        auth_failures = {"UPS": 0, "USPS": 0}
        paused = set()

        for order in orders:
            purchased_at = order.grove_label_purchased_at
            if purchased_at and (now - purchased_at) > self._CARRIER_POLL_MAX_AGE:
                order.grove_carrier_poll_stopped = True
                _notify_discord(
                    f"Tracking poll stopped for {order.name}: 30 days since the label was bought with no "
                    f"delivery scan. Please check the shipment manually."
                )
                continue

            statuses = []
            for carrier_key, tracking in order._grove_tracking_pairs():
                if carrier_key in paused:
                    continue
                client = clients.get(carrier_key)
                if client is None:
                    # Unknown carrier, or credentials for it not provisioned this
                    # stage — skip the box, not the whole order.
                    continue
                try:
                    status = client.track(tracking)
                    auth_failures[carrier_key] = 0
                except carrier_tracking.CarrierAuthError:
                    auth_failures[carrier_key] += 1
                    _logger.warning(
                        "Carrier auth failure #%s for %s on %s (%s)",
                        auth_failures[carrier_key],
                        carrier_key,
                        order.name,
                        tracking,
                    )
                    if auth_failures[carrier_key] >= 3:
                        paused.add(carrier_key)
                        _notify_discord(
                            f"Carrier tracking paused: {carrier_key} auth failed 3 times this run. "
                            f"Check {carrier_key}_CLIENT_ID / {carrier_key}_CLIENT_SECRET."
                        )
                    continue
                except Exception:  # noqa: BLE001 — one bad call never stops the sweep
                    _logger.warning(
                        "Carrier track failed for %s on %s (%s)", order.name, carrier_key, tracking, exc_info=True
                    )
                    continue
                if status:
                    statuses.append(status)

            if carrier_tracking.STATUS_FAILURE in statuses and not order.grove_carrier_exception_noted:
                order.grove_carrier_exception_noted = True
                _notify_discord(
                    f"Shipment exception on {order.name}: a carrier reported a delivery problem "
                    f"(tracking {order.grove_tracking_numbers or ''}). Please review."
                )

            new_status = carrier_tracking.least_advanced_status(statuses)
            if new_status:
                _apply_delivery_status(
                    order.env, order, new_status, order.grove_tracking_numbers or "", source="carrier_poll"
                )
