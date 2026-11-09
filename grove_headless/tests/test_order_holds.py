"""Holds on the order: record, release, snooze, and the suggested date.

A hold never ships or charges anything; every change writes chatter naming the
actor. DB-backed, so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob.
"""

from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestOrderHolds(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create({"name": "Hold test tree", "list_price": 25.0, "type": "consu"})

    def _order(self, zip_code="26651", **vals):
        partner = self.env["res.partner"].create(
            {"name": "Hold Buyer", "zip": zip_code, "country_id": self.env.ref("base.us").id}
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
        if vals:
            order.sudo().write(vals)
        return order

    def _held(self):
        order = self._order()
        order.action_grove_set_hold(
            fields.Date.context_today(order) + timedelta(days=30), "Waiting for dormancy", "Ada"
        )
        return order

    def test_set_hold_records_and_posts_chatter(self):
        order = self._order()
        until = fields.Date.context_today(order) + timedelta(days=30)
        order.action_grove_set_hold(until, "Chestnuts not dormant", "Josh")
        self.assertEqual(order.grove_hold_until, until)
        self.assertEqual(order.grove_hold_reason, "Chestnuts not dormant")
        self.assertEqual(order.grove_hold_set_by, "Josh")
        body = " ".join(order.message_ids.mapped("body"))
        self.assertIn("Chestnuts not dormant", body)
        self.assertIn("Josh", body)

    def test_set_hold_requires_reason(self):
        order = self._order()
        with self.assertRaises(UserError):
            order.action_grove_set_hold(fields.Date.context_today(order), "   ", "Josh")
        self.assertFalse(order.grove_hold_until)

    def test_set_hold_refuses_past_date(self):
        order = self._order()
        with self.assertRaises(UserError):
            order.action_grove_set_hold(fields.Date.context_today(order) - timedelta(days=1), "Because", "Josh")

    def test_set_hold_refuses_finished_order(self):
        for state in ("shipped", "delivered", "cancelled"):
            order = self._order(grove_fulfillment_state=state)
            with self.assertRaises(UserError, msg=state):
                order.action_grove_set_hold(fields.Date.context_today(order), "Because", "Josh")

    def test_release_clears_all_hold_fields(self):
        order = self._held()
        order.grove_hold_last_nudge = fields.Date.context_today(order)
        order.action_grove_release_hold("Wes")
        self.assertFalse(order.grove_hold_until)
        self.assertFalse(order.grove_hold_reason)
        self.assertFalse(order.grove_hold_set_by)
        self.assertFalse(order.grove_hold_last_nudge)
        self.assertIn("Wes", " ".join(order.message_ids.mapped("body")))

    def test_snooze_moves_date_seven_days_from_today_and_clears_last_nudge(self):
        order = self._held()
        order.grove_hold_last_nudge = fields.Date.context_today(order)
        order.action_grove_snooze_hold("Wes")
        self.assertEqual(order.grove_hold_until, fields.Date.context_today(order) + timedelta(days=7))
        self.assertFalse(order.grove_hold_last_nudge)
        self.assertIn("Wes", " ".join(order.message_ids.mapped("body")))

    def test_lane_follows_fulfillment_and_wave(self):
        self.assertEqual(self._order().grove_lane, "individual")
        self.assertEqual(self._order(grove_ship_wave="spring").grove_lane, "spring")
        self.assertEqual(self._order(grove_fulfillment="pickup").grove_lane, "none")

    def _suggest(self, order, today):
        with patch.object(fields.Date, "context_today", return_value=today):
            return order._grove_suggested_hold()

    def test_suggested_hold_uses_zone_and_dormancy(self):
        # Zone 6: fall ship window opens Nov 9, spring Apr 5; dormancy opens Nov 1.
        today = date(2027, 8, 1)
        fall_default = self._order()
        self.assertEqual(self._suggest(fall_default, today), date(2027, 11, 9))
        spring = self._order(grove_ship_wave="spring")
        self.assertEqual(self._suggest(spring, today), date(2028, 4, 5))

    def test_suggested_hold_without_zone_falls_back_to_dormancy(self):
        order = self._order(zip_code="33101")  # not on the zone matrix
        self.assertFalse(order.grove_usda_zone)
        self.assertEqual(self._suggest(order, date(2027, 8, 1)), date(2027, 11, 1))
