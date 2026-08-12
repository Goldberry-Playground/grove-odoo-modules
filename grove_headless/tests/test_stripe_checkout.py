"""Integration tests for the Stripe checkout line-item builder and webhook
handlers (GOL-642). Runs under Odoo's --test-enable runner (needs a DB for
sale.order / stock), so it is excluded from pytest collection in conftest.py.

Network is never touched: create_refund is monkeypatched and no live secret
keys are read (the handlers take the env, not the HTTP request).
"""

import hashlib
import hmac
import time
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger
from psycopg2 import IntegrityError


@tagged("post_install", "-at_install")
class TestStripeCheckout(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Cart Customer", "email": "cart@example.com", "company_id": self.company.id}
        )
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        self.product = self.env["product.product"].create(
            {"name": "Pawpaw 'Shenandoah'", "type": "consu", "is_storable": True, "list_price": 25.0}
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _set_stock(self, product, qty):
        self.env["stock.quant"]._update_available_quantity(product, self.location, qty)
        # free_qty as well as qty_available: the checkout line-item builder reads
        # free_qty (GOL-1036 defect 4), so a stale cache would misclassify stock.
        product.invalidate_recordset(["qty_available", "free_qty"])

    def _make_order(self, qty=1.0):
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})],
                }
            )
        )
        return order

    @staticmethod
    def _sign(secret, body, ts=None):
        """Build a valid Stripe-Signature header for `body` signed with `secret`
        (mirrors stripe_gateway.verify_webhook_signature's scheme)."""
        if ts is None:
            ts = int(time.time())
        signed = f"{ts}.".encode("utf-8") + body
        digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={digest}"

    # ── multi-tenant webhook signature verification (GOL-1020) ────────────

    def test_webhook_secrets_collects_all_tenants_deduped(self):
        """All three tenant env vars plus the legacy single one are collected,
        in order, with duplicates removed."""
        env = {
            "stripe_webhook_secret_nursery": "whsec_nursery",
            "stripe_webhook_secret_ggg": "whsec_ggg",
            "stripe_webhook_secret_goldberry": "whsec_goldberry",
            # Legacy var duplicates nursery's value — must be tried only once.
            "stripe_test_webhook_secret": "whsec_nursery",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                grove_main._configured_webhook_secrets(),
                ["whsec_nursery", "whsec_ggg", "whsec_goldberry"],
            )

    def test_webhook_secrets_skips_empty(self):
        """Empty/absent env vars are ignored so an unprovisioned tenant does not
        introduce an empty secret."""
        env = {
            "stripe_webhook_secret_nursery": "whsec_nursery",
            "stripe_webhook_secret_ggg": "",
            "stripe_webhook_secret_goldberry": "whsec_goldberry",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                grove_main._configured_webhook_secrets(),
                ["whsec_nursery", "whsec_goldberry"],
            )

    def test_webhook_verifies_each_tenant_secret(self):
        """A validly-signed event from EACH of the three tenants verifies
        against the shared secret list — the core GOL-1020 acceptance case."""
        secrets = ["whsec_nursery", "whsec_ggg", "whsec_goldberry"]
        body = b'{"id":"evt_multitenant","type":"checkout.session.completed"}'
        for tenant_secret in secrets:
            sig = self._sign(tenant_secret, body)
            verified, err = grove_main._verify_stripe_webhook(body, sig, secrets)
            self.assertTrue(verified, f"secret {tenant_secret} failed: {err}")
            self.assertIsNone(err)

    def test_webhook_rejects_unknown_secret(self):
        """An event signed by a secret NOT in the configured set is rejected."""
        secrets = ["whsec_nursery", "whsec_ggg", "whsec_goldberry"]
        body = b'{"id":"evt_rogue","type":"checkout.session.completed"}'
        sig = self._sign("whsec_rogue_unconfigured", body)
        verified, err = grove_main._verify_stripe_webhook(body, sig, secrets)
        self.assertFalse(verified)
        self.assertIsInstance(err, stripe_gateway.StripeError)

    def test_webhook_rejects_tampered_body(self):
        """A signature valid for the original body does not verify a mutated
        body under any tenant secret."""
        secrets = ["whsec_nursery", "whsec_ggg"]
        body = b'{"id":"evt_ok","type":"checkout.session.completed"}'
        sig = self._sign("whsec_ggg", body)
        tampered = b'{"id":"evt_HACKED","type":"checkout.session.completed"}'
        verified, _ = grove_main._verify_stripe_webhook(tampered, sig, secrets)
        self.assertFalse(verified)

    # ── line-item builder / charging matrix ──────────────────────────────

    def test_in_stock_line_charges_full_price(self):
        self._set_stock(self.product, 5)
        order = self._make_order(qty=2)
        line_items, preorder_ids, charged = grove_main._build_stripe_line_items(order)
        product_line = next(li for li in line_items if li["name"] == self.product.display_name)
        self.assertEqual(product_line["amount_cents"], stripe_gateway.to_cents(25.0))
        self.assertEqual(product_line["quantity"], 2)
        self.assertEqual(preorder_ids, [])
        self.assertGreater(charged, 0)

    def test_short_stock_line_is_deposit(self):
        self._set_stock(self.product, 0)
        order = self._make_order(qty=1)
        line_items, preorder_ids, _ = grove_main._build_stripe_line_items(order)
        deposit = next(li for li in line_items if li["name"].startswith("Deposit"))
        self.assertEqual(deposit["amount_cents"], stripe_gateway.to_cents(stripe_gateway.PREORDER_DEPOSIT))
        self.assertEqual(deposit["quantity"], 1)
        self.assertEqual(preorder_ids, [self.product.id])
        # No tax line: nothing chargeable-today is taxed on a pure-deposit cart.
        self.assertFalse([li for li in line_items if li["name"] == "Sales tax (WV)"])

    def test_partial_stock_line_splits_in_stock_and_deposit(self):
        """GOL-1036 defect 3: a line with SOME free stock splits — the in-stock
        units bill at full price and each short unit is a per-unit deposit,
        never one flat qty-1 deposit that under-reserves the shortfall."""
        self._set_stock(self.product, 2)
        order = self._make_order(qty=5)
        line_items, preorder_ids, _ = grove_main._build_stripe_line_items(order)
        full = next(li for li in line_items if li["name"] == self.product.display_name)
        self.assertEqual(full["quantity"], 2)
        self.assertEqual(full["amount_cents"], stripe_gateway.to_cents(25.0))
        deposit = next(li for li in line_items if li["name"].startswith("Deposit"))
        self.assertEqual(deposit["quantity"], 3)  # per unit of shortfall, not 1
        self.assertEqual(deposit["amount_cents"], stripe_gateway.to_cents(stripe_gateway.PREORDER_DEPOSIT))
        self.assertEqual(preorder_ids, [self.product.id])

    def test_line_items_carry_kind_for_review_badges(self):
        """GOL-1057: every charged-today line is tagged with a `kind` so the
        review page badges goods vs deposit (vs shipping / tax) without parsing
        the display name — the review renders this exact array, so its math is
        byte-identical to what Stripe charges."""
        self._set_stock(self.product, 2)
        order = self._make_order(qty=5)  # 2 in stock + 3 short → goods + deposit
        line_items, _ids, _charged = grove_main._build_stripe_line_items(order)
        goods = next(li for li in line_items if li["name"] == self.product.display_name)
        self.assertEqual(goods["kind"], "goods")
        deposit = next(li for li in line_items if li["name"].startswith("Deposit"))
        self.assertEqual(deposit["kind"], "deposit")

    # ── ship-to gate: state / potted / $0-shipping breaker (GOL-1036) ─────

    def _website(self):
        return self.env["website"].search([("company_id", "=", self.company.id)], limit=1) or self.env[
            "website"
        ].search([], limit=1)

    def _cart_payload(self, state, **extra):
        payload = {
            "contact": {"name": "Ship Test", "email": "ship@example.com"},
            "items": [{"variant_id": self.product.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "10001"},
        }
        payload.update(extra)
        return payload

    def test_state_gate_rejects_non_green_destination(self):
        """Defect 1: an unsupported ship-to state (FL / any non-green-list state)
        is rejected server-side at session creation, before any payment."""
        order, error = grove_main._create_draft_order(self._website(), self.env, self._cart_payload("FL"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't ship", error.data.decode().lower())
        # No orphan draft persisted.
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    def test_zero_shipping_circuit_breaker(self):
        """Defect 2: a shippable green-state order that cannot resolve a shipping
        charge fails loudly (never reaches Stripe with silent $0 shipping)."""
        self.product.product_tmpl_id.grove_shipping_tier = "bareroot"
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=None):
            order, error = grove_main._create_draft_order(self._website(), self.env, self._cart_payload("WV"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 409)
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    # ── explicit fulfillment: pickup vs ship (GOL-1057) ───────────────────

    def test_pickup_skips_ship_gate_and_adds_no_shipping(self):
        """Farm pickup is the ONE legitimate $0-shipping path. An explicit
        fulfillment='pickup' order clears the ship-to gate even for potted trees
        (pickup-only stock) and never adds a shipping line — without tripping the
        no-$0-ship circuit breaker."""
        self.product.product_tmpl_id.grove_shipping_tier = "potted"
        payload = self._cart_payload("WV", fulfillment="pickup")
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
        ship_lines = order.order_line.filtered(
            lambda ln: ln.product_id.default_code == grove_main.SHIPPING_PRODUCT_CODE
        )
        self.assertFalse(ship_lines, "pickup must not add a shipping line")

    def test_ship_intent_without_state_is_rejected(self):
        """fulfillment='ship' with no ship-to state is a hard error, not a silent
        fall-through to $0-shipping pickup (the collision the breaker guards)."""
        payload = self._cart_payload("", fulfillment="ship")
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("pickup", error.data.decode().lower())
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    def test_pickup_keeps_wv_tax_despite_out_of_state_address(self):
        """GOL-1303: a pickup order carrying a leftover out-of-state address (buyer
        filled the address, then toggled to pickup) must KEEP the WV default tax —
        the transfer happens at the WV farm regardless of the payload address. The
        destination de-tax path must be skipped entirely for pickup."""
        self.product.product_tmpl_id.grove_shipping_tier = "potted"
        # Seed the WV default tax explicitly so the test doesn't hinge on
        # ir.default timing in the transaction — the real product default
        # (hooks.setup_wv_sales_tax) puts this same group on every line.
        wv_group = self.env["account.tax"].search(
            [("name", "=", "WV Sales Tax 7%"), ("company_id", "=", self.company.id), ("amount_type", "=", "group")],
            limit=1,
        )
        self.assertTrue(wv_group, "WV group tax must exist (post_init_hook)")
        self.product.product_tmpl_id.taxes_id = [(6, 0, wv_group.ids)]
        payload = self._cart_payload("OH", fulfillment="pickup")
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertGreater(
            order.amount_tax, 0.0, "pickup must keep WV tax even with an OH address in the payload"
        )

    def test_unrecognized_fulfillment_is_rejected(self):
        """GOL-1303: any explicit fulfillment other than ship/pickup is a hard 400,
        not a silent fall-through to legacy inference (which would bypass the
        green-list / potted / $0-shipping gates for a direct bearer-API caller)."""
        payload = self._cart_payload("WV", fulfillment="delivery")
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("fulfillment", error.data.decode().lower())
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    def test_absent_fulfillment_still_treats_state_as_ship_to(self):
        """Back-compat: a caller that doesn't send `fulfillment` yet keeps the
        pre-1057 inference — a green-list ship-to state routes through the ship
        path (shipping applied), it is NOT treated as a $0-shipping pickup."""
        self.product.product_tmpl_id.grove_shipping_tier = "bareroot"
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=12.5) as apply_ship:
            order, error = grove_main._create_draft_order(self._website(), self.env, self._cart_payload("WV"))
        self.assertIsNone(error)
        self.assertTrue(order)
        apply_ship.assert_called_once()

    # ── oversell detection ───────────────────────────────────────────────

    def test_oversold_excludes_recorded_preorder(self):
        self._set_stock(self.product, 0)
        order = self._make_order(qty=1)
        order.grove_preorder_variant_ids = str(self.product.id)
        self.assertEqual(grove_main._oversold_lines(order), [])

    def test_oversold_flags_depleted_in_stock_line(self):
        self._set_stock(self.product, 0)
        order = self._make_order(qty=1)
        order.grove_preorder_variant_ids = ""  # was charged in full, now short
        oversold = grove_main._oversold_lines(order)
        self.assertEqual(len(oversold), 1)

    def test_in_stock_not_oversold_despite_draft_backlog(self):
        """GOL-711: on-hand stock with a backlog of unconfirmed carts is NOT an
        oversell — draft/sent orders never reserve, so free_qty stays == on-hand.
        Guards against a regression to an on_hand−Σ(open demand) implementation."""
        self._set_stock(self.product, 3)
        drafts = [self._make_order(qty=1) for _ in range(6)]  # accumulated QA test carts
        subject = drafts[0]
        subject.grove_preorder_variant_ids = ""
        self.assertEqual(grove_main._oversold_lines(subject), [])

        subject.grove_stripe_session_id = "cs_backlog"
        calls = {}
        orig = stripe_gateway.create_refund

        def fake_refund(*_a, **_k):
            calls["refunded"] = True
            return {"id": "re_x", "status": "succeeded"}

        stripe_gateway.create_refund = fake_refund
        try:
            result = grove_main._handle_session_completed(
                self.env, {"id": "cs_backlog", "payment_intent": "pi_backlog"}
            )
        finally:
            stripe_gateway.create_refund = orig
        self.assertEqual(result, "paid")
        self.assertNotIn("refunded", calls)

    def test_oversold_reads_stock_in_order_company(self):
        """GOL-711 root cause: availability must be read in the ORDER's company,
        not the ambient one. Stock living in another company's warehouse was
        invisible in the public webhook's default company → a full-stock line was
        refunded as oversold. Pinning with_company(order.company_id) fixes it."""
        other = self.env["res.company"].create({"name": "Grove Nursery Co"})
        other_wh = self.env["stock.warehouse"].search([("company_id", "=", other.id)], limit=1)
        self.assertTrue(other_wh, "a fresh company should auto-provision a warehouse")
        partner = self.env["res.partner"].create({"name": "Branch Customer", "email": "b@example.com"})
        product = self.env["product.product"].create(
            {"name": "American Plum (Potted)", "type": "consu", "is_storable": True, "list_price": 40.0}
        )
        self.env["stock.quant"].with_company(other)._update_available_quantity(product, other_wh.lot_stock_id, 24)
        product.invalidate_recordset(["qty_available", "free_qty"])
        order = (
            self.env["sale.order"]
            .with_company(other)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": other.id,
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1})],
                }
            )
        )
        # Re-read the order the way the webhook does: ambient = the OTHER (main)
        # company, with only that company allowed. Old code read qty_available
        # here and saw 0; the fix reads free_qty in order.company_id and sees 24.
        webhook_view = order.with_company(self.company).with_context(allowed_company_ids=[self.company.id])
        self.assertEqual(grove_main._oversold_lines(webhook_view), [])

    # ── webhook handlers ─────────────────────────────────────────────────

    def test_session_completed_marks_paid_and_confirms(self):
        self._set_stock(self.product, 5)
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_paid"
        session = {"id": "cs_paid", "payment_intent": "pi_paid"}
        result = grove_main._handle_session_completed(self.env, session)
        self.assertEqual(result, "paid")
        self.assertEqual(order.grove_checkout_status, "paid")
        self.assertEqual(order.grove_stripe_payment_intent, "pi_paid")
        self.assertEqual(order.state, "sale")

    def test_session_completed_deposit_paid_for_preorder(self):
        self._set_stock(self.product, 0)
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_dep"
        order.grove_preorder_variant_ids = str(self.product.id)
        result = grove_main._handle_session_completed(self.env, {"id": "cs_dep", "payment_intent": "pi_dep"})
        self.assertEqual(result, "deposit_paid")
        self.assertEqual(order.grove_checkout_status, "deposit_paid")

    def test_session_completed_oversell_refunds(self):
        self._set_stock(self.product, 0)
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_over"
        order.grove_preorder_variant_ids = ""  # charged in full, now unfulfillable

        calls = {}
        orig = stripe_gateway.create_refund

        def fake_refund(secret_key, payment_intent, **kwargs):
            calls["payment_intent"] = payment_intent
            return {"id": "re_1", "status": "succeeded"}

        stripe_gateway.create_refund = fake_refund
        try:
            result = grove_main._handle_session_completed(self.env, {"id": "cs_over", "payment_intent": "pi_over"})
        finally:
            stripe_gateway.create_refund = orig

        self.assertEqual(result, "refunded_oversell")
        self.assertEqual(calls.get("payment_intent"), "pi_over")
        self.assertEqual(order.grove_checkout_status, "refunded_oversell")

    def test_session_expired_marks_expired(self):
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_exp"
        result = grove_main._handle_session_expired(self.env, {"id": "cs_exp"})
        self.assertEqual(result, "expired")
        self.assertEqual(order.grove_checkout_status, "expired")

    def test_unknown_session_is_not_found(self):
        self.assertEqual(grove_main._handle_session_expired(self.env, {"id": "cs_missing"}), "order_not_found")

    # ── idempotency ledger ───────────────────────────────────────────────

    def test_event_id_is_unique(self):
        Event = self.env["grove.stripe.event"]
        Event.create({"event_id": "evt_dup", "event_type": "checkout.session.completed"})
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                Event.create({"event_id": "evt_dup", "event_type": "checkout.session.completed"})

    # ── transactional email (GOL-988) ────────────────────────────────────

    def test_order_confirmation_email_sent_on_paid(self):
        """A paid checkout sends the standard sale confirmation receipt for that
        order, so Odoo's outgoing mail server (Mailgun) delivers a branded email."""
        self._set_stock(self.product, 5)
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_mail"
        sent = {}
        Template = type(self.env["mail.template"])
        orig = Template.send_mail

        def fake_send_mail(self_t, res_id, **kwargs):
            sent["res_id"] = res_id
            sent["xmlid"] = self_t.get_external_id().get(self_t.id)
            return 0

        Template.send_mail = fake_send_mail
        try:
            grove_main._handle_session_completed(self.env, {"id": "cs_mail", "payment_intent": "pi_mail"})
        finally:
            Template.send_mail = orig
        self.assertEqual(sent.get("res_id"), order.id)
        self.assertEqual(sent.get("xmlid"), "sale.mail_template_sale_confirmation")

    def test_order_confirmation_skipped_without_email(self):
        """No partner email → no receipt attempt, and the webhook still succeeds."""
        self._set_stock(self.product, 5)
        order = self._make_order(qty=1)
        order.grove_stripe_session_id = "cs_noemail"
        self.partner.email = False
        sent = {}
        Template = type(self.env["mail.template"])
        orig = Template.send_mail

        def fake_send_mail(self_t, res_id, **kwargs):
            sent["called"] = True

        Template.send_mail = fake_send_mail
        try:
            result = grove_main._handle_session_completed(self.env, {"id": "cs_noemail", "payment_intent": "pi_x"})
        finally:
            Template.send_mail = orig
        self.assertEqual(result, "paid")
        self.assertNotIn("called", sent)

    def test_shipping_status_transition_notifies_once(self):
        """A status change emails the customer; a repeat webhook for the same
        status is a no-op (idempotent). Non-notify statuses update silently."""
        order = self._make_order(qty=1)
        order.grove_delivery_status = "label_purchased"
        calls = []
        orig = grove_main._notify_shipping_status

        def fake_notify(env, notify_order, notify_status, notify_tracking):
            calls.append(notify_status)

        grove_main._notify_shipping_status = fake_notify
        try:
            first = grove_main._apply_delivery_status(self.env, order, "transit", "TRACK123")
            repeat = grove_main._apply_delivery_status(self.env, order, "transit", "TRACK123")
            delivered = grove_main._apply_delivery_status(self.env, order, "delivered", "TRACK123")
        finally:
            grove_main._notify_shipping_status = orig
        self.assertTrue(first)
        self.assertFalse(repeat)
        self.assertTrue(delivered)
        self.assertEqual(calls, ["transit", "delivered"])
        self.assertEqual(order.grove_delivery_status, "delivered")

    def test_shipping_notification_builds_customer_email(self):
        """The shipping notice is a real email to the customer carrying the
        tracking number; a non-notify status produces no email."""
        order = self._make_order(qty=1)
        with mute_logger("odoo.addons.mail.models.mail_mail"):
            grove_main._notify_shipping_status(self.env, order, "transit", "TRACK999")
            grove_main._notify_shipping_status(self.env, order, "pre_transit", "TRACK999")
        shipped = self.env["mail.mail"].search([("subject", "ilike", "has shipped")])
        self.assertEqual(len(shipped), 1)
        self.assertEqual(shipped.email_to, self.partner.email)
        self.assertIn("TRACK999", shipped.body_html or "")
