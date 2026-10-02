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

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
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
