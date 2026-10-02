"""Pickup collection settles the deferred balance (GOL-2893).

A farm-pickup order that contains a sold-out bareroot tree takes only the flat
$10 deposit (GOL-2233: the sold-out trigger applies to ANY fulfilment). Its
balance was never charged: ship-time settlement (GOL-2053) runs off ship signals
and pickup has none (it buys no label and is refused by mark-shipped). So the
customer could walk away with the trees for $10.

The fix makes *collection* the pickup analogue of a ship event:
``_grove_mark_collected_and_settle`` marks the order collected AND, only on the
real transition, captures the remainder off-session against the saved card. The
contract mirrors mark-shipped exactly — best-effort (a decline flags
``settlement_failed`` but never rolls back ``collected``) and idempotent (a
double collect never double-charges).

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest in conftest.py — the pattern
every TransactionCase here follows so it is not double-skipped (GOL-1936).
"""

from unittest import mock

from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestPickupSettlement(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Pickup Customer", "email": "pickup@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "American Chestnut (bareroot)", "type": "consu", "is_storable": True, "list_price": 45.0}
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

    def _deposit_pickup_order(self, **vals):
        """A sold-out bareroot PICKUP order that took only the flat $10 deposit
        (GOL-2233). Payment is ``deposit_paid`` and the watermark is unset, so the
        order derives to stage ``deposit_paid`` — the strand case: ship-time
        settlement never sees it because pickup fires no ship signal. A saved card
        is on file so the balance can be captured off-session at collection."""
        v = {
            "grove_fulfillment": "pickup",
            "grove_checkout_status": "deposit_paid",
            "grove_amount_charged_today": 10.0,
            "grove_stripe_customer": "cus_test",
            "grove_stripe_payment_method": "pm_test",
        }
        v.update(vals)
        return self._order(**v)

    def _seed_wv_tax(self):
        wv_group = self.env["account.tax"].search(
            [("name", "=", "WV Sales Tax 7%"), ("amount_type", "=", "group")], limit=1
        )
        self.assertTrue(wv_group, "WV group tax must exist (post_init_hook)")
        self.product.product_tmpl_id.taxes_id = [(6, 0, wv_group.ids)]
        return wv_group

    # ── deposit pickup genuinely settles at collection (the hotfix) ───────

    def test_deposit_pickup_settles_balance_on_collection(self):
        """The substantive fix: a deposit-only pickup order marked collected
        captures the deferred balance off-session, lands at ``settled`` with a
        settlement payment-intent and a chatter note of the amount, and ends
        ``collected`` (no longer outstanding)."""
        self._seed_wv_tax()
        order = self._deposit_pickup_order()
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_pickup_settle", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            result = order._grove_mark_collected_and_settle(operator="josh")

        self.assertTrue(result["newly_collected"])
        self.assertEqual(result["settlement"], "settled")
        self.assertEqual(order.grove_fulfillment_stage, "collected")
        self.assertFalse(order.grove_is_outstanding)
        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(order.grove_settlement_payment_intent, "pi_pickup_settle")
        self.assertEqual(len(charges), 1, "the deferred balance is captured exactly once")
        # Chatter records the captured amount (acceptance).
        note = order.message_ids.filtered(lambda m: "settlement captured" in (m.body or "").lower())
        self.assertTrue(note, "a chatter note of the settled amount must be posted")

    # ── fully-paid pickup has nothing to settle ───────────────────────────

    def test_fully_paid_pickup_collects_with_no_charge(self):
        """A fully-paid pickup order (in-stock, charged in full at checkout) is
        ``not_applicable`` at collection and charges nothing."""
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        with mock.patch.object(stripe_gateway, "create_payment_intent") as pi:
            result = order._grove_mark_collected_and_settle(operator="josh")
        self.assertTrue(result["newly_collected"])
        self.assertEqual(result["settlement"], "not_applicable")
        self.assertEqual(order.grove_fulfillment_stage, "collected")
        pi.assert_not_called()

    # ── a double collect never double-charges ─────────────────────────────

    def test_double_collect_never_double_charges(self):
        self._seed_wv_tax()
        order = self._deposit_pickup_order()
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_once", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            first = order._grove_mark_collected_and_settle(operator="josh")
            second = order._grove_mark_collected_and_settle(operator="josh")

        self.assertTrue(first["newly_collected"])
        self.assertFalse(second["newly_collected"])
        self.assertIsNone(second["settlement"], "an idempotent re-collect re-runs no settlement")
        self.assertEqual(len(charges), 1, "a double collect must never re-charge the saved card")
        self.assertEqual(order.grove_fulfillment_stage, "collected")

    # ── a declined card keeps the order collected (best-effort) ───────────

    def test_declined_settlement_keeps_order_collected(self):
        """A declined balance keeps the order COLLECTED (the trees are gone) and
        flags settlement_failed for the dunning/retry path — collection never
        fails because the money didn't land (mirrors the mark-shipped contract)."""
        self._seed_wv_tax()
        order = self._deposit_pickup_order()

        def fake_decline(secret_key, **kwargs):
            raise stripe_gateway.StripeCardError(
                "declined", code="card_declined", decline_code="do_not_honor", payment_intent="pi_bad"
            )

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_decline),
            mock.patch.object(stripe_gateway, "create_checkout_session", return_value={"url": "https://pay.example/x"}),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
            mute_logger("odoo.addons.mail.models.mail_mail"),
        ):
            result = order._grove_mark_collected_and_settle(operator="josh")

        self.assertTrue(result["newly_collected"])
        self.assertEqual(result["settlement"], "settlement_failed")
        self.assertEqual(order.grove_fulfillment_stage, "collected")  # stays collected
        self.assertFalse(order.grove_is_outstanding)
        self.assertEqual(order.grove_checkout_status, "settlement_failed")

    # ── a non-pickup order is refused ─────────────────────────────────────

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_non_pickup_order_is_refused(self):
        """A ship order settles on the ship path, never on collection: the model
        guard refuses it (no transition, no charge)."""
        order = self._order(grove_fulfillment="ship", grove_checkout_status="deposit_paid")
        with mock.patch.object(stripe_gateway, "create_payment_intent") as pi:
            result = order._grove_mark_collected_and_settle(operator="josh")
        self.assertFalse(result["newly_collected"])
        self.assertIsNone(result["settlement"])
        self.assertNotEqual(order.grove_fulfillment_stage, "collected")
        pi.assert_not_called()
