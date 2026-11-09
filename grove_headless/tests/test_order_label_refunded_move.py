"""Label refunded and Move between seasons on the order.

Label refunded sends a bought-then-refunded label back to needing a label and
moves no money. Move changes the ship wave only; it never emails the customer or
touches deposits. DB-backed, so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import GroveTaxFixtureMixin

UPS = "1Z999AA10123456784"
UPS_2 = "1Z999AA10123456791"


@tagged("post_install", "-at_install")
class TestOrderLabelRefundedMove(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create({"name": "Move test tree", "list_price": 25.0, "type": "consu"})

    def _order(self, zip_code="26651", **vals):
        partner = self.env["res.partner"].create(
            {"name": "Move Buyer", "zip": zip_code, "country_id": self.env.ref("base.us").id}
        )
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "partner_shipping_id": partner.id,
                "company_id": self.company.id,
                "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})],
            }
        )
        order.grove_fulfillment = "ship"
        order.sudo().write({"grove_checkout_status": "paid", **vals})
        return order

    def _labelled(self, wave=None, **vals):
        order = self._order(**vals)
        if wave:
            order.sudo().write({"grove_ship_wave": wave, "grove_fulfillment_state": "wave_assigned"})
        order.action_grove_record_hand_label(UPS, actual_cost=9.0)
        order.sudo().write({"grove_label_batch_id": self.env["grove.label.batch"].create({"name": "T4 batch"}).id})
        return order

    def _body(self, order):
        return " ".join(order.message_ids.mapped("body"))

    # Label refunded

    def test_refund_clears_label_keeps_history(self):
        order = self._labelled()
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertTrue(order.grove_label_purchased_at)
        self.assertTrue(order.action_grove_label_refunded("Wes"))
        for field in (
            "grove_tracking_numbers",
            "grove_shipping_carriers",
            "grove_shipping_services",
            "grove_actual_shipping_cost",
            "grove_label_purchased_at",
            "grove_label_batch_id",
            "grove_delivery_status",
        ):
            self.assertFalse(order[field], field)
        body = self._body(order)
        self.assertIn(UPS, body)
        self.assertIn("Wes", body)

    def test_refund_returns_wave_order_to_wave_assigned(self):
        order = self._labelled(wave="fall")
        order.action_grove_label_refunded("Wes")
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")

    def test_refund_returns_other_order_to_awaiting_label(self):
        order = self._labelled()
        order.action_grove_label_refunded("Wes")
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")

    def test_refund_refused_outside_label_purchased(self):
        for state in ("shipped", "delivered", "collected", "cancelled", "awaiting_label", "wave_assigned"):
            order = self._order(grove_fulfillment_state=state)
            with self.assertRaises(UserError, msg=state):
                order.action_grove_label_refunded("Wes")

    def test_refund_refused_once_shipped(self):
        order = self._labelled()
        order.sudo().grove_fulfillment_state = "shipped"
        with self.assertRaises(UserError) as ctx:
            order.action_grove_label_refunded("Wes")
        self.assertIn("Josh", str(ctx.exception))
        self.assertEqual(order.grove_tracking_numbers, UPS)

    def test_refund_does_not_touch_money(self):
        order = self._labelled()
        order.action_grove_label_refunded("Wes")
        self.assertEqual(order.grove_checkout_status, "paid")
        order = self._labelled()
        order.sudo().grove_checkout_status = "settled"
        order.action_grove_label_refunded("Wes")
        self.assertEqual(order.grove_checkout_status, "settled")

    def test_refunded_order_can_be_labelled_again(self):
        for wave in (None, "fall"):
            order = self._labelled(wave=wave)
            order.action_grove_label_refunded("Wes")
            order.action_grove_record_hand_label(UPS_2, actual_cost=7.0)
            self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
            self.assertEqual(order.grove_tracking_numbers, UPS_2)

    # Move between seasons

    def test_move_changes_wave_and_lane(self):
        order = self._order(grove_ship_wave="fall", grove_checkout_status="deposit_paid")
        self.assertEqual(order.grove_lane, "individual")
        self.assertTrue(order.action_grove_move_wave("spring", "Wes"))
        self.assertEqual(order.grove_ship_wave, "spring")
        self.assertEqual(order.grove_lane, "spring")
        self.assertEqual(order.grove_checkout_status, "deposit_paid")
        self.assertIn("Wes", self._body(order))
        order.action_grove_move_wave("fall", "Wes")
        self.assertEqual(order.grove_lane, "individual")

    def test_move_to_same_wave_is_a_quiet_no_op(self):
        order = self._order(grove_ship_wave="spring")
        msgs = len(order.message_ids)
        self.assertFalse(order.action_grove_move_wave("spring", "Wes"))
        self.assertEqual(len(order.message_ids), msgs)

    def test_move_refused_with_tracking(self):
        order = self._labelled(wave="fall")
        with self.assertRaises(UserError) as ctx:
            order.action_grove_move_wave("spring", "Wes")
        self.assertIn("Label refunded", str(ctx.exception))
        self.assertEqual(order.grove_ship_wave, "fall")

    def test_move_refused_when_shipped(self):
        for state in ("shipped", "delivered"):
            order = self._order(grove_ship_wave="fall", grove_fulfillment_state=state)
            with self.assertRaises(UserError, msg=state):
                order.action_grove_move_wave("spring", "Wes")
            self.assertEqual(order.grove_ship_wave, "fall")

    def test_move_refused_when_cancelled(self):
        order = self._order(grove_ship_wave="fall", grove_fulfillment_state="cancelled")
        with self.assertRaises(UserError):
            order.action_grove_move_wave("spring", "Wes")
        order = self._order(grove_ship_wave="fall")
        order.action_cancel()
        with self.assertRaises(UserError):
            order.action_grove_move_wave("spring", "Wes")

    def test_move_refused_without_zone(self):
        order = self._order(grove_ship_wave="fall")
        order.sudo().grove_usda_zone = False
        with self.assertRaises(UserError):
            order.action_grove_move_wave("spring", "Wes")
        self.assertEqual(order.grove_ship_wave, "fall")

    def test_move_refuses_unknown_wave(self):
        order = self._order(grove_ship_wave="fall")
        for bad in ("summer", "", None, "Spring"):
            with self.assertRaises(UserError, msg=repr(bad)):
                order.action_grove_move_wave(bad, "Wes")

    def test_move_sends_no_email(self):
        order = self._order(grove_ship_wave="fall", grove_checkout_status="deposit_paid")
        before = self.env["mail.mail"].search_count([])
        order.action_grove_move_wave("spring", "Wes")
        self.assertEqual(self.env["mail.mail"].search_count([]), before)

    # Access

    def test_methods_need_order_operator(self):
        order = self._labelled(wave="fall")
        msgs = len(order.message_ids)
        for login, group in (("move_portal", "base.group_portal"), ("move_internal", "base.group_user")):
            as_user = order.with_user(new_test_user(self.env, login=login, groups=group))
            with self.assertRaises(AccessError):
                as_user.action_grove_label_refunded("Someone")
            with self.assertRaises(AccessError):
                as_user.action_grove_move_wave("spring", "Someone")
        self.assertEqual(order.grove_tracking_numbers, UPS)
        self.assertEqual(order.grove_ship_wave, "fall")
        self.assertEqual(len(order.message_ids), msgs)
        # sudo (controllers, crons) still works.
        self.assertTrue(order.sudo().action_grove_label_refunded("Bridge"))
