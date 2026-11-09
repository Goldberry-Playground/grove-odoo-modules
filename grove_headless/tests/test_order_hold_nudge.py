"""Daily nudges for due order holds.

A due hold posts one notice per New York day to the order channel until someone
answers it. DB-backed, so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin

# 2026-11-09 14:00 UTC is 09:00 in New York (EST), 11:00 UTC is 06:00.
MORNING_UTC = datetime(2026, 11, 9, 14, 0)
BEFORE_8_UTC = datetime(2026, 11, 9, 12, 0)  # 07:00 EST
NY_TODAY = date(2026, 11, 9)

POST = "odoo.addons.grove_headless.models.sale_order_ops.SaleOrder._grove_post_notice"


@tagged("post_install", "-at_install")
class TestOrderHoldNudge(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create({"name": "Nudge tree", "list_price": 25.0, "type": "consu"})

    def _held_order(self, until=NY_TODAY, **vals):
        partner = self.env["res.partner"].create(
            {"name": "Nudge Buyer", "zip": "26651", "country_id": self.env.ref("base.us").id}
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
        order.sudo().write(
            {
                "grove_hold_until": until,
                "grove_hold_reason": "Waiting for dormancy",
                "grove_hold_set_by": "Ada",
                "grove_usda_zone": "6",
                **vals,
            }
        )
        return order

    def _run(self, now=MORNING_UTC, ret=True):
        with patch(POST, return_value=ret) as post:
            self.env["sale.order"]._cron_grove_order_nudges(now=now)
        return post

    def _posts_for(self, post, order):
        return [c for c in post.call_args_list if f":{order.id}" in c.args[3][0]["customId"]]

    def test_due_hold_posts_once_a_day(self):
        order = self._held_order()
        post = self._run()
        calls = self._posts_for(post, order)
        self.assertEqual(len(calls), 1)
        company, title, lines, buttons = calls[0].args
        self.assertEqual(company, order.company_id)
        self.assertIn(order.name, title)
        text = "\n".join(lines)
        self.assertIn("Nudge Buyer", text)
        self.assertIn("Zone 6", text)
        self.assertIn("Waiting for dormancy", text)
        self.assertIn("Ada", text)
        self.assertIn("2026-11-09", text)
        self.assertEqual(
            [(b["label"], b["customId"]) for b in buttons],
            [
                ("Ready to ship", f"ord:ready:{order.id}"),
                ("Snooze a week", f"ord:snooze:{order.id}"),
                ("Change date", f"ord:holddate:{order.id}:2026-11-09"),
            ],
        )
        self.assertEqual(order.grove_hold_last_nudge, NY_TODAY)
        # A second run the same day posts nothing for this order.
        again = self._run(now=MORNING_UTC + timedelta(hours=1))
        self.assertEqual(self._posts_for(again, order), [])

    def test_nudge_repeats_next_day_until_answered(self):
        order = self._held_order(until=NY_TODAY - timedelta(days=2))
        self._run()
        self.assertEqual(order.grove_hold_last_nudge, NY_TODAY)
        tomorrow = self._run(now=MORNING_UTC + timedelta(days=1))
        self.assertEqual(len(self._posts_for(tomorrow, order)), 1)
        self.assertEqual(order.grove_hold_last_nudge, NY_TODAY + timedelta(days=1))
        # Snoozing clears the stamp and moves the date out: quiet.
        order.action_grove_snooze_hold("Ada")
        later = self._run(now=MORNING_UTC + timedelta(days=2))
        self.assertEqual(self._posts_for(later, order), [])

    def test_future_hold_is_quiet(self):
        order = self._held_order(until=NY_TODAY + timedelta(days=1))
        post = self._run()
        self.assertEqual(self._posts_for(post, order), [])
        self.assertFalse(order.grove_hold_last_nudge)

    def test_before_8am_new_york_is_quiet(self):
        order = self._held_order()
        post = self._run(now=BEFORE_8_UTC)
        self.assertEqual(self._posts_for(post, order), [])
        self.assertFalse(order.grove_hold_last_nudge)

    def test_new_york_date_not_utc_date_decides(self):
        # 03:00 UTC on Nov 10 is 22:00 on Nov 9 in New York, so a hold due Nov 10
        # is not due yet (the UTC date would wrongly say it is).
        order = self._held_order(until=date(2026, 11, 10))
        post = self._run(now=datetime(2026, 11, 10, 3, 0))
        self.assertEqual(self._posts_for(post, order), [])

    def test_failed_post_does_not_stamp_last_nudge(self):
        order = self._held_order()
        post = self._run(ret=False)
        self.assertEqual(len(self._posts_for(post, order)), 1)
        self.assertFalse(order.grove_hold_last_nudge)
        retry = self._run(now=MORNING_UTC + timedelta(hours=1))
        self.assertEqual(len(self._posts_for(retry, order)), 1)
        self.assertEqual(order.grove_hold_last_nudge, NY_TODAY)

    def test_finished_order_never_nudges(self):
        shipped = self._held_order(grove_fulfillment_state="shipped")
        cancelled = self._held_order()
        cancelled.sudo().write({"state": "cancel"})
        post = self._run()
        self.assertEqual(self._posts_for(post, shipped), [])
        self.assertEqual(self._posts_for(post, cancelled), [])

    def test_one_order_failing_does_not_stop_the_others(self):
        first = self._held_order()
        second = self._held_order()
        calls = []

        def flaky(this, company, title, lines, buttons):
            calls.append(buttons[0]["customId"])
            if len(calls) == 1:
                raise RuntimeError("boom")
            return True

        with patch(POST, flaky):
            self.env["sale.order"]._cron_grove_order_nudges(now=MORNING_UTC)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len((first | second).filtered("grove_hold_last_nudge")), 1)

    def test_post_notice_never_raises(self):
        order = self._held_order()
        mod = "odoo.addons.grove_headless.models.sale_order_ops"
        with patch.dict("os.environ", {"ORDER_ALERT_SECRET": "s", "DISCORD_BRIDGE_BASE_URL": "http://bridge"}):
            with patch(f"{mod}.requests.post", side_effect=OSError("down")):
                self.assertFalse(order._grove_post_notice(order.company_id, "t", ["l"], []))
            with patch(f"{mod}.requests.post") as ok:
                ok.return_value.status_code = 200
                self.assertTrue(order._grove_post_notice(order.company_id, "t", ["l"], []))
                self.assertEqual(ok.call_args.args[0], "http://bridge/orders/notice")
                self.assertEqual(ok.call_args.kwargs["json"]["companyId"], order.company_id.id)
                self.assertEqual(ok.call_args.kwargs["headers"], {"X-Order-Alert-Secret": "s"})
            with patch(f"{mod}.requests.post") as bad:
                bad.return_value.status_code = 422
                self.assertFalse(order._grove_post_notice(order.company_id, "t", ["l"], []))
        with patch.dict("os.environ", {"ORDER_ALERT_SECRET": "", "DISCORD_BRIDGE_BASE_URL": ""}):
            self.assertFalse(order._grove_post_notice(order.company_id, "t", ["l"], []))
