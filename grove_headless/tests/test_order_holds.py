"""Holds on the order: record, release, snooze, and the suggested date.

A hold never ships or charges anything; every change writes chatter naming the
actor. DB-backed, so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob.
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

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
        for state in ("shipped", "delivered", "collected", "cancelled"):
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

    def test_snooze_validates_days_and_stage(self):
        order = self._held()
        for bad in (0, 366, -1, "7", 1.5, True, None):
            with self.assertRaises(UserError, msg=repr(bad)):
                order.action_grove_snooze_hold("Wes", days=bad)
        order.action_grove_snooze_hold("Wes", days=365)
        order.sudo().grove_fulfillment_state = "shipped"
        with self.assertRaises(UserError):
            order.action_grove_snooze_hold("Wes")

    def test_hold_methods_need_order_operator(self):
        order = self._held()
        before = (order.grove_hold_until, order.grove_hold_reason, order.grove_hold_set_by)
        msgs = len(order.message_ids)
        today = fields.Date.context_today(order)
        users = [
            new_test_user(self.env, login="hold_portal", groups="base.group_portal"),
            new_test_user(self.env, login="hold_internal", groups="base.group_user"),
        ]
        for user in users:
            as_user = order.with_user(user)
            with self.assertRaises(AccessError):
                as_user.action_grove_set_hold(today, "Forged", "Someone")
            with self.assertRaises(AccessError):
                as_user.action_grove_release_hold("Someone")
            with self.assertRaises(AccessError):
                as_user.action_grove_snooze_hold("Someone")
        self.assertEqual((order.grove_hold_until, order.grove_hold_reason, order.grove_hold_set_by), before)
        self.assertEqual(len(order.message_ids), msgs)
        # sudo (controllers, crons) still works.
        self.assertTrue(order.sudo().action_grove_release_hold("Bridge"))

    def test_suggested_hold_zone_missing_from_calendar_falls_back(self):
        order = self._order()
        self.assertTrue(order.grove_usda_zone)
        with patch("odoo.addons.grove_headless.models.sale_order_ops.preorder_waves", side_effect=KeyError(6)):
            self.assertEqual(self._suggest(order, date(2027, 8, 1)), date(2027, 11, 1))

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

    # Releasing a spring order after its ship window has closed (GOL-3937): nothing
    # else warns until next season, so the release itself must say so, once.

    def _set_spring_window(self, zone, window=((3, 1), (4, 15))):
        """Pin the zone's spring ship window via the calendar override param."""
        (sm, sd), (em, ed) = window
        override = {"zones": {str(zone): {"spring": [[sm, sd], [em, ed]], "spring_order_deadline": [2, 15]}}}
        self.env["ir.config_parameter"].sudo().set_param("grove_headless.shipping_calendar", json.dumps(override))

    def _release_at(self, order, today, hold_until):
        """Hold then release the order with ``today`` frozen and the Discord notice
        stubbed. Returns the notice mock."""
        notice = patch.object(type(order), "_grove_post_notice_status", return_value="ok")
        with patch.object(fields.Date, "context_today", return_value=today), notice as notice_mock:
            order.action_grove_set_hold(hold_until, "Waiting for stock", "Ada")
            order.action_grove_release_hold("Wes")
        return notice_mock

    def test_release_into_closed_spring_window_warns_once(self):
        order = self._order(grove_ship_wave="spring")
        self.assertEqual(order.grove_lane, "spring")
        self._set_spring_window(order.grove_usda_zone)
        notice = self._release_at(order, date(2026, 7, 1), date(2026, 8, 1))
        body = " ".join(order.message_ids.mapped("body"))
        self.assertEqual(body.count("ship window had closed"), 1)
        self.assertIn("Mar 1, 2027", body)  # next spring ship start
        notice.assert_called_once()
        self.assertEqual(notice.call_args.args[0], order.company_id)

    def test_release_into_open_spring_window_is_quiet(self):
        order = self._order(grove_ship_wave="spring")
        self._set_spring_window(order.grove_usda_zone)
        notice = self._release_at(order, date(2026, 3, 20), date(2026, 3, 25))
        self.assertNotIn("ship window had closed", " ".join(order.message_ids.mapped("body")))
        notice.assert_not_called()

    def test_release_before_spring_window_opens_is_quiet(self):
        order = self._order(grove_ship_wave="spring")
        self._set_spring_window(order.grove_usda_zone)
        notice = self._release_at(order, date(2026, 1, 15), date(2026, 2, 10))
        self.assertNotIn("ship window had closed", " ".join(order.message_ids.mapped("body")))
        notice.assert_not_called()

    def test_release_of_individual_order_never_warns(self):
        order = self._order()  # fall/individual lane
        self.assertEqual(order.grove_lane, "individual")
        self._set_spring_window(order.grove_usda_zone)
        notice = self._release_at(order, date(2026, 7, 1), date(2026, 8, 1))
        self.assertNotIn("ship window had closed", " ".join(order.message_ids.mapped("body")))
        notice.assert_not_called()
