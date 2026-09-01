"""Terminal fulfilment state machine (GOL-1981).

Each of the three fulfilment modes must reach its own terminal state on its own
event, queryable from Odoo alone, and a farm-pickup order must never be markable
shipped (the trigger the shipment email hangs off). Runs under Odoo's
--test-enable runner (needs a DB for sale.order), so it is excluded from pytest
collection in conftest.py.
"""

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestFulfillmentState(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "State Customer", "email": "state@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Pawpaw 'Test'", "type": "consu", "is_storable": True, "list_price": 25.0}
        )

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    # ── derived pre-fulfilment stages (watermark unset) ──────────────────

    def test_derived_stage_awaiting_payment(self):
        order = self._order()
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_payment")
        self.assertTrue(order.grove_is_outstanding)

    def test_derived_stage_ship_paid_awaiting_label(self):
        order = self._order(grove_fulfillment="ship", grove_checkout_status="paid")
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")
        self.assertTrue(order.grove_is_outstanding)

    def test_derived_stage_ship_label_purchased(self):
        order = self._order(
            grove_fulfillment="ship",
            grove_checkout_status="paid",
            grove_delivery_status="label_purchased",
        )
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")

    def test_derived_stage_pickup_reserved(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        self.assertEqual(order.grove_fulfillment_stage, "reserved")
        self.assertTrue(order.grove_is_outstanding)

    def test_derived_stage_preorder_deposit_paid(self):
        order = self._order(grove_fulfillment="ship", grove_checkout_status="deposit_paid")
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

    def test_derived_stage_cancelled_on_refund_or_expiry(self):
        for status in ("expired", "refunded_oversell"):
            order = self._order(grove_checkout_status=status)
            self.assertEqual(order.grove_fulfillment_stage, "cancelled")
            self.assertFalse(order.grove_is_outstanding)

    # ── ship terminal path ───────────────────────────────────────────────

    def test_ship_path_to_delivered(self):
        order = self._order(
            grove_fulfillment="ship",
            grove_checkout_status="paid",
            grove_delivery_status="label_purchased",
        )
        self.assertTrue(order.action_grove_mark_shipped(operator="josh"))
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertTrue(order.grove_is_outstanding)
        self.assertTrue(order.action_grove_mark_delivered())
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        self.assertFalse(order.grove_is_outstanding)

    def test_mark_shipped_is_idempotent_across_signals(self):
        """Operator double-click OR a duplicate Shippo transit event must not
        double-fire: only the first transition returns True."""
        order = self._order(
            grove_fulfillment="ship",
            grove_checkout_status="paid",
            grove_delivery_status="label_purchased",
        )
        self.assertTrue(order.action_grove_mark_shipped(source="operator"))
        self.assertFalse(order.action_grove_mark_shipped(source="operator"))
        self.assertFalse(order.action_grove_mark_shipped(source="shippo"))
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_delivered_rejects_illegal_skip(self):
        """delivered is only legal from shipped — a stray transit=delivered on a
        still-labelled order is dropped, not written."""
        order = self._order(
            grove_fulfillment="ship",
            grove_checkout_status="paid",
            grove_delivery_status="label_purchased",
        )
        self.assertFalse(order.action_grove_mark_delivered())
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")

    # ── pickup terminal path + email guard ───────────────────────────────

    def test_pickup_path_to_collected(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        self.assertTrue(order.action_grove_mark_collected(operator="josh"))
        self.assertEqual(order.grove_fulfillment_stage, "collected")
        self.assertFalse(order.grove_is_outstanding)

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_pickup_is_never_markable_shipped(self):
        order = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        self.assertFalse(order.action_grove_mark_shipped())
        self.assertEqual(order.grove_fulfillment_stage, "reserved")
        self.assertFalse(order.grove_should_send_shipment_email())

    def test_ship_and_preorder_send_shipment_email(self):
        ship = self._order(grove_fulfillment="ship")
        self.assertTrue(ship.grove_should_send_shipment_email())

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_collect_rejects_non_pickup(self):
        order = self._order(grove_fulfillment="ship", grove_checkout_status="paid")
        self.assertFalse(order.action_grove_mark_collected())

    # ── preorder terminal path (deposit -> wave -> ship path) ─────────────

    def test_preorder_path_wave_to_delivered(self):
        order = self._order(grove_fulfillment="ship", grove_checkout_status="deposit_paid")
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        self.assertTrue(order.action_grove_assign_wave(wave_ref="2027-Spring"))
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")
        # Wave opens: balance charged + label bought puts it back on the ship path.
        self.assertTrue(order._grove_advance_state("label_purchased", source="shippo"))
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertTrue(order.action_grove_mark_shipped())
        self.assertTrue(order.action_grove_mark_delivered())
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        self.assertFalse(order.grove_is_outstanding)

    # ── the outstanding query answers from Odoo alone ────────────────────

    def test_outstanding_domain_query(self):
        outstanding = self._order(grove_fulfillment="ship", grove_checkout_status="paid")
        done = self._order(grove_fulfillment="pickup", grove_checkout_status="paid")
        done.action_grove_mark_collected()
        found = self.env["sale.order"].search(
            [("id", "in", (outstanding | done).ids), ("grove_is_outstanding", "=", True)]
        )
        self.assertIn(outstanding, found)
        self.assertNotIn(done, found)
