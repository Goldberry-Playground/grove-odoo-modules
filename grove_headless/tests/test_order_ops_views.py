"""Odoo-side order lane actions, the Hold wizard, the "On hold" filter and the
one-off launch repost. DB-backed, so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import GroveTaxFixtureMixin

POST_CARD = "odoo.addons.grove_headless.controllers.main._post_order_alert_card"


@tagged("post_install", "-at_install")
class TestOrderOpsViews(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create(
            {"name": "Views test tree", "list_price": 25.0, "type": "consu"}
        )
        cls.Order = cls.env["sale.order"]

    def _order(self, confirm=True, fulfillment="ship", **vals):
        partner = self.env["res.partner"].create(
            {"name": "Views Buyer", "zip": "26651", "country_id": self.env.ref("base.us").id}
        )
        order = self.Order.create(
            {
                "partner_id": partner.id,
                "partner_shipping_id": partner.id,
                "company_id": self.company.id,
                "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})],
            }
        )
        order.grove_fulfillment = fulfillment
        if confirm:
            order.action_confirm()
        if vals:
            order.sudo().write(vals)
        return order

    def _hold(self, order, days=10):
        order.action_grove_set_hold(fields.Date.context_today(order) + timedelta(days=days), "Customer asked", "Ada")

    # -- filter ----------------------------------------------------------------
    def test_on_hold_filter_finds_held_orders(self):
        held = self._order()
        free = self._order()
        self._hold(held)
        view = self.env.ref("grove_headless.view_sales_order_filter_grove_hold")
        self.assertIn("grove_hold_until", view.arch_db)
        found = self.Order.search([("grove_hold_until", "!=", False), ("id", "in", (held | free).ids)])
        self.assertEqual(found, held)

    # -- hold wizard -----------------------------------------------------------
    def test_hold_wizard_prefills_and_writes_through_the_action(self):
        order = self._order()
        Wizard = self.env["grove.order.hold"].with_context(active_id=order.id, active_model="sale.order")
        defaults = Wizard.default_get(["order_id", "hold_until"])
        self.assertEqual(defaults["order_id"], order.id)
        self.assertEqual(defaults["hold_until"], order._grove_suggested_hold())
        until = fields.Date.context_today(order) + timedelta(days=20)
        wizard = Wizard.create({"order_id": order.id, "hold_until": until, "reason": "  Frost risk  "})
        wizard.action_confirm()
        self.assertEqual(order.grove_hold_until, until)
        self.assertEqual(order.grove_hold_reason, "Frost risk")
        self.assertEqual(order.grove_hold_set_by, self.env.user.name)
        self.assertIn(self.env.user.name, order.message_ids[0].body)

    def test_hold_wizard_refuses_blank_reason_and_past_date(self):
        order = self._order()
        today = fields.Date.context_today(order)
        wizard = self.env["grove.order.hold"].create(
            {"order_id": order.id, "hold_until": today + timedelta(days=5), "reason": "   "}
        )
        with self.assertRaises(UserError):
            wizard.action_confirm()
        wizard.reason = "Real reason"
        wizard.hold_until = today - timedelta(days=1)
        with self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertFalse(order.grove_hold_until)

    def test_plain_internal_user_cannot_run_hold_wizard(self):
        order = self._order()
        user = new_test_user(self.env, login="views_internal", groups="base.group_user")
        today = fields.Date.context_today(order)
        with self.assertRaises(AccessError):
            self.env["grove.order.hold"].with_user(user).create(
                {"order_id": order.id, "hold_until": today + timedelta(days=5), "reason": "Nope"}
            )
        self.assertFalse(order.grove_hold_until)

    # -- server actions --------------------------------------------------------
    def test_server_actions_are_bound_and_limited(self):
        group = self.env.ref("sales_team.group_sale_salesman_all_leads")
        for xmlid in (
            "action_grove_release_hold",
            "action_grove_move_to_spring",
            "action_grove_move_to_fall",
            "action_grove_label_refunded",
        ):
            action = self.env.ref(f"grove_headless.{xmlid}")
            self.assertEqual(action.binding_model_id.model, "sale.order", xmlid)
            self.assertEqual(action.group_ids, group, xmlid)
        hold = self.env.ref("grove_headless.action_grove_hold_order")
        self.assertEqual(hold.res_model, "grove.order.hold")

    def test_release_and_move_server_actions_use_the_user_name(self):
        order = self._order(grove_ship_wave="fall", grove_usda_zone="6")
        self._hold(order)
        self.env.ref("grove_headless.action_grove_release_hold").with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id
        ).run()
        self.assertFalse(order.grove_hold_until)
        self.assertIn(self.env.user.name, order.message_ids[0].body)
        self.env.ref("grove_headless.action_grove_move_to_spring").with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id
        ).run()
        self.assertEqual(order.grove_ship_wave, "spring")

    # -- launch repost ---------------------------------------------------------
    def test_repost_covers_open_ship_orders_once(self):
        open_a = self._order()
        open_b = self._order(grove_fulfillment_stage="label_purchased")
        done = self._order(grove_fulfillment_stage="shipped")
        pickup = self._order(fulfillment="pickup")
        draft = self._order(confirm=False)
        with patch(POST_CARD, return_value=True) as post:
            first = self.Order._grove_repost_open_order_cards()
            second = self.Order._grove_repost_open_order_cards()
        posted_ids = {call.args[0].id for call in post.call_args_list}
        self.assertTrue({open_a.id, open_b.id} <= posted_ids)
        self.assertFalse(posted_ids & {done.id, pickup.id, draft.id})
        self.assertEqual(post.call_count, first["posted"])
        self.assertEqual(second["posted"], 0)
        self.assertGreaterEqual(second["skipped"], 2)
        self.assertTrue(open_a.grove_card_reposted and open_b.grove_card_reposted)

    def test_failed_post_is_not_stamped_and_others_continue(self):
        bad = self._order()
        good = self._order()

        def fake(order, context, is_deposit):
            return order.id != bad.id

        with patch(POST_CARD, side_effect=fake):
            result = self.Order._grove_repost_open_order_cards()
        self.assertFalse(bad.grove_card_reposted)
        self.assertTrue(good.grove_card_reposted)
        self.assertGreaterEqual(result["failed"], 1)
        with patch(POST_CARD, return_value=True):
            again = self.Order._grove_repost_open_order_cards()
        self.assertTrue(bad.grove_card_reposted)
        self.assertGreaterEqual(again["posted"], 1)

    def test_repost_stops_after_three_consecutive_failures(self):
        for _ in range(5):
            self._order()
        with patch(POST_CARD, return_value=False) as post:
            result = self.Order._grove_repost_open_order_cards()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(result["posted"], 0)
