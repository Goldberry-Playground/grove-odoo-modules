"""5-day and day-of spring zone warnings.

DB-backed, so listed in tests/__init__.py AND conftest.py's collect_ignore_glob.
Zone 6 spring ships Apr 5 to Apr 15 by default.
"""

import json
from datetime import date, datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger
from psycopg2 import IntegrityError

from .common import GroveTaxFixtureMixin

POST = "odoo.addons.grove_headless.models.sale_order_ops.SaleOrder._grove_post_notice_status"
CAL_PARAM = "grove_headless.shipping_calendar"


def _at(day):
    """09:00 New York (EDT) on ``day`` as naive UTC."""
    return datetime(day.year, day.month, day.day, 13, 0)


T5 = date(2027, 3, 31)  # zone 6 start minus 5
START = date(2027, 4, 5)


@tagged("post_install", "-at_install")
class TestZoneAlerts(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.Alert = cls.env["grove.zone.alert"]
        cls.product = cls.env["product.product"].create({"name": "Zone tree", "list_price": 25.0, "type": "consu"})
        cls.partner = cls.env["res.partner"].create(
            {"name": "Zone Buyer", "zip": "26651", "country_id": cls.env.ref("base.us").id}
        )

    def _orders(self, count, zone="6", held=0, **vals):
        orders = self.env["sale.order"]
        for i in range(count):
            order = self.env["sale.order"].create(
                {
                    "partner_id": self.partner.id,
                    "partner_shipping_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})],
                }
            )
            order.grove_fulfillment = "ship"
            extra = {"grove_ship_wave": "spring", "grove_usda_zone": zone, **vals}
            if i < held:
                extra["grove_hold_until"] = date(2027, 4, 10)
            order.sudo().write(extra)
            orders |= order
        return orders

    def _run(self, day=T5, ret="ok", side_effect=None):
        with patch(POST, return_value=ret, side_effect=side_effect) as post:
            self.env["sale.order"]._cron_grove_zone_alerts(now=_at(day))
        return post

    def _texts(self, post):
        return [" ".join(c.args[2]) for c in post.call_args_list]

    def _override(self, zones):
        self.env["ir.config_parameter"].sudo().set_param(CAL_PARAM, json.dumps({"zones": zones}))

    def test_t5_text(self):
        self._orders(14, held=3)
        post = self._run(T5)
        self.assertEqual(self._texts(post), ["Zone 6 spring shipping starts in 5 days: 14 orders, 3 on hold."])
        company, title, lines, buttons = post.call_args.args
        self.assertEqual(company, self.company)
        self.assertEqual(buttons, [])

    def test_t5_with_no_holds_still_says_zero_on_hold(self):
        self._orders(2)
        self.assertEqual(self._texts(self._run(T5)), ["Zone 6 spring shipping starts in 5 days: 2 orders, 0 on hold."])

    def test_day_of_text_excludes_held_orders(self):
        self._orders(14)
        self._orders(2, held=2)
        self.assertEqual(
            self._texts(self._run(START)), ["Zone 6 spring shipping starts today: 14 orders ready to batch."]
        )

    def test_orders_that_are_out_the_door_do_not_count(self):
        self._orders(2)
        self._orders(1, grove_tracking_numbers="1Z999")
        cancelled = self._orders(1)
        cancelled.sudo().write({"state": "cancel"})
        shipped = self._orders(1)
        shipped.sudo().write({"grove_fulfillment_stage": "shipped"})
        self._orders(1, zone="7")  # another zone, never counted toward zone 6
        self.assertEqual(
            [t for t in self._texts(self._run(START)) if t.startswith("Zone 6")],
            ["Zone 6 spring shipping starts today: 2 orders ready to batch."],
        )

    def test_zone_without_orders_posts_nothing(self):
        post = self._run(T5)
        post.assert_not_called()
        self.assertFalse(self.Alert.search([("company_id", "=", self.company.id)]))
        # A later order can still trigger it inside the window.
        self._orders(1)
        self.assertEqual(self._run(date(2027, 4, 2)).call_count, 1)

    def test_nothing_before_8am_new_york(self):
        self._orders(1)
        with patch(POST, return_value="ok") as post:
            self.env["sale.order"]._cron_grove_zone_alerts(now=datetime(2027, 3, 31, 11, 0))
        post.assert_not_called()

    def test_alert_sends_once_per_zone_and_year(self):
        self._orders(1)
        self.assertEqual(self._run(T5).call_count, 1)
        self.assertEqual(self._run(T5).call_count, 0)
        self.assertEqual(self._run(date(2027, 4, 2)).call_count, 0)
        alert = self.Alert.search([("company_id", "=", self.company.id)])
        self.assertEqual((alert.zone, alert.year, alert.kind), ("6", 2027, "t5"))
        # The day-of alert is a separate kind and still goes out.
        self.assertEqual(self._run(START).call_count, 1)
        self.assertEqual(self._run(START).call_count, 0)
        self.assertEqual(
            sorted(self.Alert.search([("company_id", "=", self.company.id)]).mapped("kind")), ["day_of", "t5"]
        )

    def test_unique_per_company_zone_year_kind(self):
        vals = {"company_id": self.company.id, "zone": "6", "year": 2027, "kind": "t5"}
        self.Alert.create(vals)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.Alert.create(vals)

    def test_missed_day_catches_up_with_true_day_count(self):
        self._orders(1)
        self.assertEqual(
            self._texts(self._run(date(2027, 4, 3))),
            ["Zone 6 spring shipping starts in 2 days: 1 order, 0 on hold."],
        )

    def test_one_day_out_is_singular(self):
        self._orders(2)
        self.assertEqual(
            self._texts(self._run(date(2027, 4, 4))),
            ["Zone 6 spring shipping starts in 1 day: 2 orders, 0 on hold."],
        )

    def test_missed_start_day_catches_up_after_the_start(self):
        self._orders(14)
        self.assertEqual(
            self._texts(self._run(date(2027, 4, 7))),
            ["Zone 6 spring shipping started 2 days ago: 14 orders ready to batch."],
        )
        self.assertEqual(self._texts(self._run(date(2027, 4, 8))), [])  # already sent for the year

    def test_started_one_day_ago_is_singular(self):
        self._orders(1)
        self.assertEqual(
            self._texts(self._run(date(2027, 4, 6))),
            ["Zone 6 spring shipping started 1 day ago: 1 order ready to batch."],
        )

    def test_missed_t5_sends_only_day_of(self):
        self._orders(1)
        post = self._run(START)
        self.assertEqual(len(self._texts(post)), 1)
        self.assertIn("starts today", self._texts(post)[0])
        self.assertEqual(self.Alert.search([("company_id", "=", self.company.id)]).mapped("kind"), ["day_of"])

    def test_uses_admin_calendar_override(self):
        self._orders(1)
        # Default zone 6 starts Apr 5 (day-of today). The override pushes it to Apr 10.
        self._override({"6": {"spring": {"ship": [[4, 10], [4, 20]]}}})
        self.assertEqual(
            self._texts(self._run(START)), ["Zone 6 spring shipping starts in 5 days: 1 order, 0 on hold."]
        )
        alert = self.Alert.search([("company_id", "=", self.company.id)])
        self.assertEqual((alert.year, alert.kind), (2027, "t5"))

    def test_failed_post_records_nothing(self):
        self._orders(1)
        self._run(T5, ret="failed")
        self.assertFalse(self.Alert.search([("company_id", "=", self.company.id)]))
        self.assertEqual(self._run(T5).call_count, 1)  # next run retries

    def test_one_zone_failing_does_not_stop_the_others(self):
        self._orders(1)
        self._orders(1, zone="7")
        self._override({"7": {"spring": {"ship": [[4, 5], [4, 15]]}}})
        post = self._run(START, side_effect=[RuntimeError("boom"), "ok"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(self.Alert.search([("company_id", "=", self.company.id)])), 1)

    def test_unmapped_company_is_skipped_for_the_rest_of_the_run(self):
        self._orders(1)
        self._orders(1, zone="7")
        self._override({"7": {"spring": {"ship": [[4, 5], [4, 15]]}}})
        post = self._run(START, ret="unmapped")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(self.Alert.search([("company_id", "=", self.company.id)]))

    def test_hourly_job_runs_zone_alerts_even_if_nudges_fail(self):
        self._orders(1)
        Order = self.env["sale.order"]
        with (
            patch.object(type(Order), "_cron_grove_order_nudges", side_effect=RuntimeError("boom")),
            patch(POST, return_value="ok") as post,
            mute_logger("odoo.addons.grove_headless.models.sale_order_ops"),
        ):
            Order._cron_grove_order_ops(now=_at(T5))
        self.assertEqual(post.call_count, 1)

    def test_cron_record_calls_the_hourly_job(self):
        cron = self.env.ref("grove_headless.ir_cron_grove_order_nudges")
        self.assertIn("_cron_grove_order_ops", cron.code)
