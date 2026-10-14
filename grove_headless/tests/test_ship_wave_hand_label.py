"""Ship-wave assignment + hand-bought label + Odoo mark-shipped (GOL-2895).

The deposit-ship hotfix: a sold-out bareroot SHIP order takes only the flat $10
deposit (GOL-2233) and sits at ``deposit_paid``. On current main that order can
never be waved, labelled or settled — ``action_grove_assign_wave`` had no caller,
and the batch label import rejects an un-waved order. This covers the three new
seams and the full state walk the acceptance exercises:

    deposit_paid → wave_assigned → label_purchased (hand-entered) → shipped

What must hold:
  * assign-wave advances deposit_paid → wave_assigned, is idempotent, and
    refuses farm pickup (pickup settles on the collect path, GOL-2893);
  * a hand-bought label records tracking/carrier/ACTUAL cost and advances to
    label_purchased, inferring UPS from a 1Z prefix, and does NOT settle (that is
    the ship event's job — mirrors import_tracking);
  * the Odoo Mark-shipped model entry runs the SAME _operator_mark_shipped path
    (shipped transition + ship-time settlement + exactly one shipment email), so
    the flow does not depend on the Discord button;
  * a non-waveable / not-awaiting-a-label order fails VISIBLY, never silently.

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest in conftest.py (GOL-1936).
"""

import os
from datetime import date
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin

# Jennifer Scott's real hand-bought UPS label from the issue (S00349).
UPS_TRACKING = "1Z21W6C10310589105"
USPS_TRACKING = "9400100000000000000000"


@tagged("post_install", "-at_install")
class TestShipWaveHandLabel(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Deposit Ship Customer", "email": "deposit@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Sold-out Bareroot Plum", "type": "consu", "is_storable": True, "list_price": 56.0}
        )

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _deposit_ship_order(self, **vals):
        """A sold-out deposit SHIP order sitting at deposit_paid — the state the
        hotfix starts from (GOL-2233 applied the $10 deposit, nothing waved it)."""
        v = {
            "grove_fulfillment": "ship",
            "grove_checkout_status": "deposit_paid",
            "grove_amount_charged_today": 10.0,
        }
        v.update(vals)
        return self._order(**v)

    # ── assign to ship wave (GOL-2895 item 1) ─────────────────────────────

    def test_assign_wave_advances_deposit_order(self):
        order = self._deposit_ship_order()
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        self.assertTrue(order.action_grove_assign_wave(wave_ref="Fall-2026-W1"))
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")
        # The wave ref lands in the chatter audit trail.
        self.assertTrue(order.message_ids.filtered(lambda m: "Fall-2026-W1" in (m.body or "")))

    def test_assign_wave_is_idempotent(self):
        order = self._deposit_ship_order()
        self.assertTrue(order.action_grove_assign_wave())
        self.assertFalse(order.action_grove_assign_wave())  # already there → no-op
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_assign_wave_refuses_pickup(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="deposit_paid")
        self.assertFalse(order.action_grove_assign_wave())
        # Pickup stays on the collect path — never pulled onto the ship path.
        self.assertNotEqual(order.grove_fulfillment_stage, "wave_assigned")

    # ── record a hand-bought label (GOL-2895 item 3) ──────────────────────

    def test_hand_label_advances_waved_order_and_infers_ups(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        self.assertTrue(order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79))
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertEqual(order.grove_tracking_numbers, UPS_TRACKING)
        self.assertEqual(order.grove_shipping_carriers, "UPS")  # inferred from 1Z
        self.assertEqual(order.grove_shipping_services, "ups_ground")
        self.assertEqual(order.grove_actual_shipping_cost, 8.79)
        self.assertEqual(order.grove_delivery_status, "label_purchased")
        # NOT settled here — settlement is the ship event's job (GOL-2053).
        self.assertEqual(order.grove_checkout_status, "deposit_paid")

    def test_hand_label_uses_operator_carrier_when_not_1z(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        order.action_grove_record_hand_label(USPS_TRACKING, carrier="usps", actual_cost=7.10)
        self.assertEqual(order.grove_shipping_carriers, "USPS")
        self.assertEqual(order.grove_shipping_services, "usps_ground_advantage")

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_hand_label_rejects_unwaved_deposit_order(self):
        order = self._deposit_ship_order()  # deposit_paid, NOT waved
        with self.assertRaises(UserError):
            order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79)
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

    def test_hand_label_rejects_invalid_tracking(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        with self.assertRaises(UserError):
            order.action_grove_record_hand_label("bad tracking!", carrier="UPS")

    def test_hand_label_rejects_unknown_carrier_for_non_1z(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        with self.assertRaises(UserError):
            # Non-1Z number with no operator carrier → we must not guess.
            order.action_grove_record_hand_label(USPS_TRACKING, carrier=None, actual_cost=7.10)

    def test_hand_label_refuses_re_record(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79)
        with self.assertRaises(UserError):
            order.action_grove_record_hand_label("1Z999AA10123456784", actual_cost=9.99)

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_hand_label_refuses_pickup(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        with self.assertRaises(UserError):
            order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79)

    # ── the wizard wires through to the model (GOL-2895 item 3 UI) ────────

    def test_wizard_infers_carrier_and_records(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        wizard = (
            self.env["grove.hand.label"]
            .with_context(active_id=order.id)
            .new({"tracking_number": UPS_TRACKING, "actual_cost": 8.79})
        )
        self.assertEqual(wizard.order_id, order)
        wizard._onchange_tracking_number()
        self.assertEqual(wizard.carrier, "UPS")  # 1Z → UPS inferred on the wizard
        # Persist + run the record action.
        wizard = (
            self.env["grove.hand.label"]
            .with_context(active_id=order.id)
            .create({"tracking_number": UPS_TRACKING, "carrier": "UPS", "actual_cost": 8.79})
        )
        wizard.action_record()
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertEqual(order.grove_actual_shipping_cost, 8.79)

    # ── Odoo Mark-shipped runs the shared settle+notify path (item 4) ─────

    def test_mark_shipped_and_settle_runs_operator_path(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79)
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            result = order._grove_mark_shipped_and_settle(operator="josh")

        self.assertTrue(result["newly_shipped"])
        self.assertEqual(result["settlement"], "settled")
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        settle.assert_called_once()
        notify.assert_called_once()  # exactly one branded shipment email
        self.assertEqual(order.grove_delivery_status, "transit")
        # The Odoo button records the chatter source as the operator, not Discord.
        self.assertTrue(order.message_ids.filtered(lambda m: "operator" in (m.body or "")))

    def test_mark_shipped_and_settle_is_idempotent(self):
        order = self._deposit_ship_order()
        order.action_grove_assign_wave()
        order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79)
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled") as settle,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            first = order._grove_mark_shipped_and_settle(operator="josh")
            second = order._grove_mark_shipped_and_settle(operator="josh")

        self.assertTrue(first["newly_shipped"])
        self.assertFalse(second["newly_shipped"])
        settle.assert_called_once()
        notify.assert_called_once()

    # ── the full acceptance state walk at unit level ──────────────────────

    def test_full_deposit_to_shipped_walk(self):
        """deposit_paid → wave_assigned → label_purchased → shipped, ending with
        settlement run and exactly one shipment email — the acceptance flow."""
        order = self._deposit_ship_order()
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

        self.assertTrue(order.action_grove_assign_wave(wave_ref="Fall-2026-W1"))
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")

        self.assertTrue(order.action_grove_record_hand_label(UPS_TRACKING, actual_cost=8.79))
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")

        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled"),
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            result = order._grove_mark_shipped_and_settle(operator="josh")

        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertEqual(result["settlement"], "settled")
        notify.assert_called_once()


@tagged("post_install", "-at_install")
class TestShipHandlingFee(GroveTaxFixtureMixin, TransactionCase):
    """GOL-2895 item 2 (Josh ruling 2026-10-02): a flat shipping & handling fee rides
    on the GROVE-SHIP line at settlement, on top of the raw Pirate Ship label cost.
    ``grove_actual_shipping_cost`` stays the raw carrier spend for reporting; only the
    customer-facing line carries the fee, so ``amount_total`` and the Stripe Tax line
    items both include it. The fee is Odoo-editable
    (``grove_headless.shipping_handling_fee``, default $5.00), applied PER ORDER."""

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.icp = self.env["ir.config_parameter"].sudo()
        self.partner = self.env["res.partner"].create(
            {"name": "Fee Customer", "email": "fee@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Sold-out Bareroot Pawpaw", "type": "consu", "is_storable": True, "list_price": 48.0}
        )

    def _ship_order_with_label(self, actual=8.79):
        """A deposit SHIP order with a stale checkout shipping estimate on its
        GROVE-SHIP line and the ACTUAL label cost recorded — the state settlement
        rewrites."""
        ship_product = grove_main._get_shipping_product(self.env, self.company)
        return (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "grove_fulfillment": "ship",
                    "grove_checkout_status": "deposit_paid",
                    "grove_amount_charged_today": 10.0,
                    "grove_actual_shipping_cost": actual,
                    "order_line": [
                        (0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0, "price_unit": 48.0}),
                        # Stale checkout estimate — settlement must rewrite this line.
                        (0, 0, {"product_id": ship_product.id, "product_uom_qty": 1.0, "price_unit": 99.0}),
                    ],
                }
            )
        )

    def test_recompute_adds_flat_fee_to_actual_cost(self):
        order = self._ship_order_with_label(actual=8.79)
        grove_main._recompute_ship_total(self.env, order)
        ship_line = grove_main._settlement_shipping_line(order)
        self.assertEqual(ship_line.price_unit, 13.79)  # 8.79 label + 5.00 default fee
        # The raw carrier spend is left untouched for true-cost reporting.
        self.assertEqual(order.grove_actual_shipping_cost, 8.79)

    def test_fee_is_odoo_editable(self):
        self.icp.set_param("grove_headless.shipping_handling_fee", "4.00")
        self.assertEqual(grove_main._shipping_handling_fee(self.env), 4.00)
        order = self._ship_order_with_label(actual=8.79)
        grove_main._recompute_ship_total(self.env, order)
        self.assertEqual(grove_main._settlement_shipping_line(order).price_unit, 12.79)

    def test_fee_defaults_and_bad_values_fall_back(self):
        self.icp.set_param("grove_headless.shipping_handling_fee", "")
        self.assertEqual(grove_main._shipping_handling_fee(self.env), 5.00)
        self.icp.set_param("grove_headless.shipping_handling_fee", "not-a-number")
        self.assertEqual(grove_main._shipping_handling_fee(self.env), 5.00)
        self.icp.set_param("grove_headless.shipping_handling_fee", "-1")
        self.assertEqual(grove_main._shipping_handling_fee(self.env), 5.00)  # negative rejected

    def test_settlement_charges_actual_plus_fee(self):
        """Worked example shape (Josh): trees $48 + label $8.79 + fee $5.00 - deposit
        $10 = $51.79 before tax. Stripe Tax is left OFF so the balance is Odoo's
        amount_total minus the deposit; the fee is proven present two ways — the
        settled ship line is actual+fee, and the captured balance tracks the
        fee-bearing amount_total."""
        order = self._ship_order_with_label(actual=8.79)
        order.grove_stripe_customer = "cus_fee"
        order.grove_stripe_payment_method = "pm_fee"
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_fee", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.dict(os.environ, {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            status = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(status, "settled")
        self.assertEqual(len(charges), 1)
        # The GROVE-SHIP line settled at actual label + flat fee.
        self.assertEqual(grove_main._settlement_shipping_line(order).price_unit, 13.79)
        # The captured balance is the fee-bearing total minus the deposit.
        expected_cents = stripe_gateway.to_cents(order.amount_total - 10.0)
        self.assertEqual(charges[0]["amount_cents"], expected_cents)


@tagged("post_install", "-at_install")
class TestSeasonalLabelGate(GroveTaxFixtureMixin, TransactionCase):
    """GOL-2895 item 2 (Josh ruling 2026-10-02): the bareroot label gate is keyed to
    the ORDER date and the season cutover (``grove_headless.deposit_cutover_md``,
    default Oct 15), not to today's dormancy window alone. An order placed ON OR
    BEFORE the cutover ships now as peat-and-bagged (leafed) even outside the window;
    only orders placed AFTER the cutover are held for the next dormant wave.

    ``can_ship_bareroot`` is stubbed so the test fixes the SEASON (in/out of the
    dormancy window) deterministically rather than coupling to today's date; the
    packer is stubbed so this isolates the gate decision, not box planning."""

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {
                "name": "Gate Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "zip": "26651",
                "email": "gate@example.com",
            }
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Sold-out Bareroot Hazelnut",
                "type": "consu",
                "list_price": 48.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )

    def _order(self, order_date):
        return (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "date_order": order_date,
                    "grove_fulfillment": "ship",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0, "price_unit": 48.0})],
                }
            )
        )

    def _stub_pack(self):
        return (
            patch.object(sale_order_module, "pack_for_state", return_value=[SimpleNamespace(box_id="BR_S", count=1)]),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
        )

    def test_pre_cutover_order_ships_now_outside_dormancy(self):
        order = self._order("2026-10-01 12:00:00")  # on/before the Oct 15 cutover
        packer, reason = self._stub_pack()
        with (
            packer,
            reason,
            # Leafed season: outside the dormancy window today.
            patch.object(sale_order_module, "can_ship_bareroot", return_value=False),
        ):
            _address, plan, _mode = order._grove_pack_for_label()
        self.assertTrue(plan)  # the gate did NOT refuse a pre-cutover order in the leafed season

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_post_cutover_order_held_for_dormant_wave(self):
        order = self._order("2026-11-20 12:00:00")  # after the Oct 15 cutover
        packer, reason = self._stub_pack()
        with (
            packer,
            reason,
            patch.object(sale_order_module, "can_ship_bareroot", return_value=False),  # outside the window
        ):
            with self.assertRaisesRegex(UserError, "after the season cutover"):
                order._grove_pack_for_label()

    def test_post_cutover_order_ships_inside_window(self):
        order = self._order("2026-11-20 12:00:00")  # after cutover, but the dormant wave is open
        packer, reason = self._stub_pack()
        with (
            packer,
            reason,
            patch.object(sale_order_module, "can_ship_bareroot", return_value=True),  # inside the dormancy window
        ):
            _address, plan, _mode = order._grove_pack_for_label()
        self.assertTrue(plan)  # the November dormant wave ships normally


@tagged("post_install", "-at_install")
class TestWaveOrderIgnoresCutover(GroveTaxFixtureMixin, TransactionCase):
    """A wave order placed ON/BEFORE the Oct 15 cutover must not take the legacy
    'ships now as peat-and-bagged' path: its stored wave decides."""

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        partner = self.env["res.partner"].create(
            {
                "name": "Cutover Wave",
                "street": "1 Grove Way",
                "city": "Summersville",
                "zip": "26651",
                "email": "c@example.com",
            }
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Cutover Tree",
                "type": "consu",
                "list_price": 48.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )
        self.order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "date_order": "2026-10-07 12:00:00",
                    "grove_fulfillment": "ship",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0, "price_unit": 48.0})],
                }
            )
        )

    def test_pre_cutover_wave_order_held_outside_its_window(self):
        from odoo import fields

        self.order.grove_ship_wave = "spring"
        with (
            patch.object(fields.Date, "context_today", return_value=date(2026, 11, 15)),
            patch.object(sale_order_module, "can_ship_bareroot", return_value=True),
            self.assertRaisesRegex(UserError, "spring wave"),
        ):
            self.order._grove_pack_for_label()
