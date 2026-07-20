"""Integration tests for the Stripe checkout line-item builder and webhook
handlers (GOL-642). Runs under Odoo's --test-enable runner (needs a DB for
sale.order / stock), so it is excluded from pytest collection in conftest.py.

Network is never touched: create_refund is monkeypatched and no live secret
keys are read (the handlers take the env, not the HTTP request).
"""

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
        product.invalidate_recordset(["qty_available"])

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
