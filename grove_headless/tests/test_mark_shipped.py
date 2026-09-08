"""Operator Mark-Shipped orchestration (GOL-1980, Phase 2 of GOL-1975).

The Discord-bridge endpoint ``POST /grove/api/v1/orders/<id>/mark-shipped``
delegates to ``_operator_mark_shipped(env, order, actor)`` — the module-level
seam these tests drive directly (the house idiom: ``settle_order_at_ship`` /
``_notify_shipping_status`` are likewise env-taking module functions, not
request-bound). What must hold, per the 2026-09-07 CEO directive coupling this
to the merged GOL-2053 settlement:

  * marking shipped ADVANCES the canonical GOL-1981 state machine (not the old
    raw grove_delivery_status write) and is pickup-guarded;
  * ship TRIGGERS ship-time settlement, so a deposit-only order can never be
    marked shipped yet left uncaptured;
  * a double-click re-runs NEITHER settlement NOR the customer email;
  * the branded shipment email fires exactly once and a later Shippo transit
    scan is then a no-op.

Runs under Odoo's --test-enable runner (needs a DB for sale.order / stock), so
it is listed in tests/__init__.py AND excluded from pytest in conftest.py — the
pattern every TransactionCase here follows so it is not double-skipped (GOL-1936).
"""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestMarkShipped(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Ship Customer", "email": "ship@example.com", "company_id": self.company.id}
        )
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        self.product = self.env["product.product"].create(
            {"name": "American Plum", "type": "consu", "is_storable": True, "list_price": 22.0}
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _ship_order(self, **vals):
        """A paid ship order sitting at 'label_purchased' — the normal state a
        Mark-Shipped click lands on (labels bought, now physically shipped)."""
        v = {
            "grove_fulfillment": "ship",
            "grove_checkout_status": "paid",
            "grove_delivery_status": "label_purchased",
        }
        v.update(vals)
        return self._order(**v)

    def _add_shipping_line(self, order, price=12.5):
        ship_product = grove_main._get_shipping_product(self.env, self.company)
        order.write(
            {"order_line": [(0, 0, {"product_id": ship_product.id, "product_uom_qty": 1, "price_unit": price})]}
        )

    def _deposit_order_at_label(self, **vals):
        """A deposit-only preorder whose fulfilment watermark reached
        ``label_purchased`` but whose PAYMENT is still ``deposit_paid`` — i.e.
        the label milestone landed without settlement running. This is the
        strand case mark-shipped must capture: without the watermark set, a
        deposit_paid order derives to stage ``deposit_paid`` (settlement runs at
        label purchase in the normal flow, so this only bites a manual/edge
        label path). Marking it shipped is where the balance must finally land."""
        v = {
            "grove_fulfillment": "ship",
            "grove_checkout_status": "deposit_paid",
            "grove_fulfillment_state": "label_purchased",
            "grove_amount_charged_today": 10.0,
            "grove_actual_shipping_cost": 9.0,
            "grove_stripe_customer": "cus_test",
            "grove_stripe_payment_method": "pm_test",
        }
        v.update(vals)
        order = self._order(**v)
        self._add_shipping_line(order)
        return order

    # ── transition + coupling on the real ship ───────────────────────────

    def test_ship_advances_state_and_fires_settlement_and_email_once(self):
        order = self._ship_order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="not_applicable") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="208085380262526976")

        self.assertTrue(result["newly_shipped"])
        # Canonical GOL-1981 lifecycle, not the old raw status write.
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        settle.assert_called_once()
        # Branded shipment email fired once, via the shared transit path so a
        # later Shippo scan is a no-op.
        notify.assert_called_once()
        self.assertEqual(order.grove_delivery_status, "transit")

    def test_double_click_reruns_neither_settlement_nor_email(self):
        order = self._ship_order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="not_applicable") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            first = grove_main._operator_mark_shipped(self.env, order, actor="1")
            second = grove_main._operator_mark_shipped(self.env, order, actor="1")

        self.assertTrue(first["newly_shipped"])
        self.assertFalse(second["newly_shipped"])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        # Exactly one settle + one email across the double-click (GOL-1975 guard).
        settle.assert_called_once()
        notify.assert_called_once()

    def test_operator_id_recorded_in_chatter(self):
        order = self._ship_order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="not_applicable"),
            mock.patch.object(grove_main, "_notify_shipping_status"),
        ):
            grove_main._operator_mark_shipped(self.env, order, actor="998877")
        stamped = order.message_ids.filtered(lambda m: "998877" in (m.body or ""))
        self.assertTrue(stamped, "operator id should be stamped into the chatter for the audit trail")

    # ── deposit-only order genuinely settles at ship (CEO coupling) ───────

    def test_deposit_only_order_is_captured_at_ship(self):
        """The substantive coupling: a shipped deposit-only order must not be
        left uncaptured. Marking it shipped charges the deferred balance
        off-session against the saved card, exactly once."""
        self._seed_wv_tax()
        order = self._deposit_order_at_label()
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_ship_settle", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="42")

        self.assertTrue(result["newly_shipped"])
        self.assertEqual(result["settlement"], "settled")
        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(order.grove_settlement_payment_intent, "pi_ship_settle")
        self.assertEqual(len(charges), 1, "the deferred balance is captured exactly once")

    def test_double_click_on_deposit_order_never_double_charges(self):
        self._seed_wv_tax()
        order = self._deposit_order_at_label()
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_once", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            grove_main._operator_mark_shipped(self.env, order, actor="1")
            second = grove_main._operator_mark_shipped(self.env, order, actor="1")

        self.assertFalse(second["newly_shipped"])
        self.assertEqual(len(charges), 1, "a double-click must never re-charge the saved card")

    def test_settlement_failure_never_rolls_back_the_shipped_state(self):
        """A declined balance keeps the order SHIPPED (the plant is gone) and
        flags settlement_failed for the dunning/retry path — the ship signal
        never fails because the money didn't land (GOL-2053 acceptance 4)."""
        self._seed_wv_tax()
        order = self._deposit_order_at_label()

        def fake_decline(secret_key, **kwargs):
            raise stripe_gateway.StripeCardError(
                "declined", code="card_declined", decline_code="do_not_honor", payment_intent="pi_bad"
            )

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_decline),
            mock.patch.object(stripe_gateway, "create_checkout_session", return_value={"url": "https://pay.example/x"}),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
            mute_logger("odoo.addons.mail.models.mail_mail"),
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="7")

        self.assertTrue(result["newly_shipped"])
        self.assertEqual(result["settlement"], "settlement_failed")
        self.assertEqual(order.grove_fulfillment_stage, "shipped")  # stays shipped
        self.assertEqual(order.grove_checkout_status, "settlement_failed")

    # ── pickup is never shipped ──────────────────────────────────────────

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_pickup_order_never_ships_settles_or_emails(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        with (
            mock.patch.object(grove_main, "settle_order_at_ship") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="1")
        self.assertFalse(result["newly_shipped"])
        self.assertEqual(order.grove_fulfillment_stage, "reserved")
        settle.assert_not_called()
        notify.assert_not_called()

    # ── non-shippable state is a rejection, not a false "already shipped" ──

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_non_shippable_state_rejects_without_side_effects(self):
        """An order not yet at a shippable stage (awaiting_payment) is an illegal
        transition: newly_shipped is False AND the stage stays put (not
        'shipped'), so the endpoint distinguishes it from an idempotent
        double-click and returns a 409 error rather than a silent 'already
        shipped' ack (GOL-1975 no-silent-ack guard). Nothing settles or emails."""
        order = self._order(grove_fulfillment="ship")  # no payment → awaiting_payment
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_payment")
        with (
            mock.patch.object(grove_main, "settle_order_at_ship") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="1")
        self.assertFalse(result["newly_shipped"])
        self.assertIsNone(result["settlement"])
        # The distinguishing signal the endpoint keys its 409 on: still not shipped.
        self.assertNotIn(order.grove_fulfillment_stage, ("shipped", "delivered"))
        settle.assert_not_called()
        notify.assert_not_called()

    # ── fully-paid ship order has nothing to settle ──────────────────────

    def test_fully_paid_order_ships_with_no_settlement_charge(self):
        order = self._ship_order()  # grove_checkout_status="paid"
        with (
            mock.patch.object(stripe_gateway, "create_payment_intent") as pi,
            mock.patch.object(grove_main, "_notify_shipping_status"),
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="9")
        self.assertTrue(result["newly_shipped"])
        self.assertEqual(result["settlement"], "not_applicable")
        pi.assert_not_called()

    # ── helper (mirrors the settlement fixture) ──────────────────────────

    def _seed_wv_tax(self):
        wv_group = self.env["account.tax"].search(
            [("name", "=", "WV Sales Tax 7%"), ("amount_type", "=", "group")], limit=1
        )
        self.assertTrue(wv_group, "WV group tax must exist (post_init_hook)")
        self.product.product_tmpl_id.taxes_id = [(6, 0, wv_group.ids)]
        return wv_group
