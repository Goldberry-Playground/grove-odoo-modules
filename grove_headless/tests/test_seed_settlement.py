"""Seed pre-orders settle at ship through the existing predicate (GOL-3257 §7).

A seed order takes a $1 deposit at checkout (``deposit_paid``) and is never
quoted shipping, so it reaches ship-time settlement with NO GROVE-SHIP line. The
settlement engine must create that line from the actual label cost + the flat
shipping_handling_fee, then charge the deferred balance off-session on the same
path the tree pre-orders use (GOL-2053/2895) — no new charge code.

Acceptance (spec Testing): a chinquapin 50-pack reserved for $1, then shipped
with a real label, settles to ``$29 + label + handling + tax``.

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest in conftest.py (GOL-1936).
"""

from unittest import mock

from odoo.addons.grove_headless.controllers.main import settle_order_at_ship
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestSeedSettlement(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Seed Customer", "email": "seed@example.com", "company_id": self.company.id}
        )
        # Allegheny Chinquapin seed nuts, 50-pack = $30 (spec Launch catalog).
        self.seed = self.env["product.product"].create(
            {
                "name": "Allegheny Chinquapin seed nuts (50-pack)",
                "type": "consu",
                "is_storable": True,
                "list_price": 30.0,
                "grove_shipping_tier": "seed",
                "grove_seed_pack_lb": 0.125,
            }
        )

    def _deposit_seed_order(self, **vals):
        """A seed SHIP order that took only the $1 deposit (deposit_paid), with a
        saved card and a known actual label cost, ready for ship-time settlement.
        No GROVE-SHIP line: seeds are not quoted at checkout, so settlement is
        where the label cost first lands."""
        base = {
            "partner_id": self.partner.id,
            "partner_shipping_id": self.partner.id,
            "company_id": self.company.id,
            "grove_fulfillment": "ship",
            "grove_checkout_status": "deposit_paid",
            "grove_amount_charged_today": 1.0,
            "grove_actual_shipping_cost": 8.0,
            "grove_stripe_customer": "cus_test",
            "grove_stripe_payment_method": "pm_test",
            "order_line": [(0, 0, {"product_id": self.seed.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def test_seed_balance_is_goods_minus_deposit_plus_label_and_handling(self):
        """The acceptance math: $30 goods, $1 deposit, $8 label, $5 handling and
        no tax (out-of-state, Stripe Tax off) settle to $42 = $29 + $8 + $5.
        The charge is the single off-session capture; no second charge code."""
        order = self._deposit_seed_order()
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_seed_settle", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            result = settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settled")
        self.assertEqual(order.grove_checkout_status, "settled")
        # Settlement created the GROVE-SHIP line from actual label + handling.
        ship = order.order_line.filtered(lambda ol: ol.product_id.default_code == "GROVE-SHIP")
        self.assertEqual(len(ship), 1, "settlement must add one shipping line for a seed order")
        self.assertAlmostEqual(ship.price_unit, 13.0, places=2)  # $8 label + $5 handling
        # One off-session charge for the whole deferred balance.
        self.assertEqual(len(charges), 1)
        # $29 goods balance ($30 - $1 deposit) + $8 label + $5 handling + $0 tax.
        self.assertEqual(charges[0]["amount_cents"], stripe_gateway.to_cents(42.00))
        # And it is exactly amount_total minus what was taken today — the shared
        # settlement predicate, not a seed-specific charge code.
        self.assertEqual(
            charges[0]["amount_cents"],
            stripe_gateway.to_cents(order.amount_total - order.grove_amount_charged_today),
        )

    def test_a_paid_seed_order_has_nothing_to_settle(self):
        """A seed order already charged in full / settled is not re-charged."""
        order = self._deposit_seed_order(grove_checkout_status="paid")
        with mock.patch.object(stripe_gateway, "create_payment_intent") as pi:
            result = settle_order_at_ship(self.env, order)
        self.assertEqual(result, "not_applicable")
        pi.assert_not_called()
