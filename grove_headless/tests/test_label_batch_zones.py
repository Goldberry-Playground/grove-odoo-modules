"""Spring Pirate Ship batches per USDA zone (order lanes spec section 5).

Spring orders are batched by zone; fall and other orders are fulfilled one at a
time. The rate table is stubbed, but the ship calendar and dormancy window are
real, so the 5-day build allowance is exercised end to end.

DB-backed, so listed in tests/__init__.py AND conftest.py's collect_ignore_glob.
"""

import csv
import io
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.grove_headless.models import label_batch as label_batch_module
from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.models import shipping_calendar as calendar_module
from odoo.addons.grove_headless.models.shipping_boxes import PackedBox
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tests.common import HttpCase, get_db_name
from odoo.tools import mute_logger
from psycopg2 import IntegrityError

ZONE_6_ZIP = "26651"
ZONE_7_ZIP = "24012"
# Zone 6 spring ships Apr 5 to Apr 15, so its batch opens Mar 31.
ZONE_6_OPENS = date(2027, 3, 31)
INSIDE_WINDOW = date(2027, 4, 1)
_T = ["1Z999AA10123456784", "1Z999AA10123456785", "1Z999AA10123456786"]


def _plan():
    return [PackedBox(box_id="p24x10x4", count=1)]


@tagged("post_install", "-at_install")
class TestLabelBatchZones(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.Batch = self.env["grove.label.batch"]
        self.product = self.env["product.product"].create(
            {
                "name": "Zone Dogwood",
                "type": "consu",
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )

    def _order(self, zip_code=ZONE_6_ZIP, email=None, checkout="deposit_paid", wave="spring"):
        partner = self.env["res.partner"].create(
            {
                "name": "Zone Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "state_id": self.env.ref("base.state_us_48").id,
                "zip": zip_code,
                "country_id": self.env.ref("base.us").id,
                "email": email or f"zone-{self.env['res.partner'].search_count([])}@example.com",
            }
        )
        vals = {
            "partner_id": partner.id,
            "company_id": self.company.id,
            "grove_fulfillment": "ship",
            "grove_checkout_status": checkout,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0, "price_unit": 40.0})],
        }
        if wave:
            vals["grove_ship_wave"] = wave
        order = self.env["sale.order"].with_company(self.company).create(vals)
        if checkout == "deposit_paid":
            order.grove_preorder_variant_ids = str(self.product.id)
        return order

    @contextmanager
    def _on(self, day, plan=None):
        """Pin today and stub the rate table only."""
        with (
            patch.object(fields.Date, "context_today", return_value=day),
            patch.object(sale_order_module, "pack_for_state", return_value=_plan() if plan is None else plan),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
            patch.object(label_batch_module, "box_rate", return_value=24.0),
            patch.object(label_batch_module, "box_service_title", return_value="UPS Ground"),
        ):
            yield

    def _build(self, zone="6", day=INSIDE_WINDOW):
        with self._on(day):
            return self.Batch.build_open_batch(self.company, zone)

    def _open_batches(self):
        return self.Batch.search([("company_id", "=", self.company.id), ("state", "in", ("open", "exported"))])

    def _csv(self, header, rows):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    # eligibility

    def test_zone_batch_takes_only_that_zone_spring_orders(self):
        wanted = self._order()
        paid = self._order(checkout="paid")
        individual = self._order(checkout="paid", wave=None)
        fall = self._order(wave="fall")
        other_zone = self._order(zip_code=ZONE_7_ZIP)
        held = self._order()
        with self._on(INSIDE_WINDOW):
            held.action_grove_set_hold(INSIDE_WINDOW + timedelta(days=30), "Customer away", "Josh")
        cancelled = self._order()
        cancelled.state = "cancel"
        no_zone = self._order(zip_code="00000")
        self.assertFalse(no_zone.grove_usda_zone)

        batch = self._build()["batch"]

        self.assertEqual(batch.zone, "6")
        self.assertEqual(batch.order_ids, wanted | paid)
        self.assertEqual(wanted.grove_label_batch_id, batch)
        for excluded in (individual, fall, other_zone, held, cancelled, no_zone):
            self.assertFalse(excluded.grove_label_batch_id, excluded.name)

    def test_build_assigns_deposit_orders_to_wave(self):
        order = self._order()
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

        batch = self._build()["batch"]

        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")
        self.assertEqual(batch.line_ids.order_id, order)
        self.assertEqual(batch.state, "exported")
        bodies = order.message_ids.mapped("body")
        self.assertTrue(any(batch.name in body for body in bodies), bodies)

    def test_unpackable_order_is_left_out_and_named(self):
        first, second = self._order(), self._order()
        with self._on(INSIDE_WINDOW, plan=[]):
            with patch.object(sale_order_module, "pack_for_state", side_effect=[None, _plan()]):
                result = self.Batch.build_open_batch(self.company, "6")
        batch = result["batch"]
        self.assertEqual(len(batch.order_ids), 1)
        left_out = (first | second) - batch.order_ids
        self.assertEqual(left_out.grove_fulfillment_stage, "deposit_paid")
        self.assertFalse(left_out.grove_label_batch_id)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn(left_out.name, result["skipped"][0])
        self.assertIn(left_out.name, batch.notes)

    def test_nothing_eligible_reports_and_leaves_no_batch(self):
        result = self._build()
        self.assertFalse(result["batch"])
        self.assertIn("Nothing to batch", result["message"])
        self.assertFalse(self._open_batches())

    def test_nothing_packable_names_orders_and_leaves_no_batch(self):
        order = self._order()
        with self._on(INSIDE_WINDOW):
            with patch.object(sale_order_module, "pack_for_state", return_value=None):
                result = self.Batch.build_open_batch(self.company, "6")
        self.assertFalse(result["batch"])
        self.assertIn(order.name, result["message"])
        self.assertFalse(self._open_batches())
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

    def test_rebuild_with_nothing_left_cancels_the_open_batch(self):
        order = self._order()
        batch = self._build()["batch"]
        with self._on(INSIDE_WINDOW):
            order.action_grove_set_hold(INSIDE_WINDOW + timedelta(days=30), "Customer away", "Josh")
        result = self._build()
        self.assertFalse(result["batch"])
        self.assertEqual(batch.state, "cancelled")
        self.assertFalse(order.grove_label_batch_id)

    def test_rebuild_releases_an_order_that_dropped_out(self):
        kept, dropped = self._order(), self._order()
        batch = self._build()["batch"]
        self.assertEqual(batch.order_ids, kept | dropped)
        with self._on(INSIDE_WINDOW):
            dropped.action_grove_set_hold(INSIDE_WINDOW + timedelta(days=30), "Customer away", "Josh")
        again = self._build()["batch"]
        self.assertEqual(again, batch)
        self.assertEqual(batch.order_ids, kept)
        self.assertFalse(dropped.grove_label_batch_id)

    # build window

    def test_build_refused_more_than_5_days_early(self):
        order = self._order()
        with self._on(ZONE_6_OPENS - timedelta(days=1)):
            with self.assertRaises(UserError) as caught:
                self.Batch.build_open_batch(self.company, "6")
        self.assertIn("March 31, 2027", str(caught.exception))
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        self.assertFalse(self._open_batches())

    def test_build_allowed_at_exactly_5_days(self):
        deposit, paid = self._order(), self._order(checkout="paid")
        batch = self._build(day=ZONE_6_OPENS)["batch"]
        self.assertEqual(batch.order_ids, deposit | paid)

    def test_build_refused_after_ship_end_names_next_year(self):
        self._order()
        with self._on(date(2027, 4, 16)):
            with self.assertRaises(UserError) as caught:
                self.Batch.build_open_batch(self.company, "6")
        self.assertIn("March 31, 2028", str(caught.exception))

    def test_single_order_pack_gets_no_early_allowance(self):
        order = self._order(checkout="paid")
        with self._on(ZONE_6_OPENS):
            with self.assertRaises(UserError):
                order._grove_pack_for_label()
            # The allowance is the batch path's, and only inside the build window.
            order._grove_pack_for_label(ignore_ship_window=True)
        with self._on(ZONE_6_OPENS - timedelta(days=1)):
            with self.assertRaises(UserError):
                order._grove_pack_for_label(ignore_ship_window=True)

    def test_build_refuses_blank_or_unknown_zone(self):
        with self._on(INSIDE_WINDOW):
            for zone in ("", False, None, "1", "11", "6a"):
                with self.assertRaises(UserError, msg=repr(zone)):
                    self.Batch.build_open_batch(self.company, zone)
        self.assertFalse(self._open_batches())

    def test_spring_build_window_dates(self):
        calendar = calendar_module.default_calendar()
        self.assertEqual(
            calendar_module.spring_build_window(8, date(2027, 2, 1), calendar),
            (date(2027, 2, 24), date(2027, 4, 15)),
        )
        self.assertEqual(
            calendar_module.spring_build_window(6, date(2026, 11, 9), calendar),
            (date(2027, 3, 31), date(2027, 4, 15)),
        )
        self.assertEqual(
            calendar_module.spring_build_window(6, date(2027, 4, 15), calendar),
            (date(2027, 3, 31), date(2027, 4, 15)),
        )
        self.assertEqual(
            calendar_module.spring_build_window(6, date(2027, 4, 16), calendar),
            (date(2028, 3, 31), date(2028, 4, 15)),
        )

    # one open batch per company and zone

    def test_two_zones_open_at_once(self):
        six = self._order()
        seven = self._order(zip_code=ZONE_7_ZIP)
        batch_six = self._build("6")["batch"]
        batch_seven = self._build("7")["batch"]
        self.assertNotEqual(batch_six, batch_seven)
        self.assertEqual(batch_six.order_ids, six)
        self.assertEqual(batch_seven.order_ids, seven)
        self.assertEqual(self.Batch._open_batch(self.company, "6"), batch_six)
        self.assertEqual(self.Batch._open_batch(self.company, "7"), batch_seven)
        self.assertEqual(self.Batch._open_batches(self.company), batch_six | batch_seven)
        # Building a zone again returns its own batch.
        self.assertEqual(self._build("6")["batch"], batch_six)

    def test_database_refuses_a_second_open_batch_for_a_zone(self):
        self.Batch.create({"name": "LB-TEST-01", "company_id": self.company.id, "zone": "6"})
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.Batch.create({"name": "LB-TEST-02", "company_id": self.company.id, "zone": "6"})
        # A closed batch for the zone, or an older batch with no zone, does not count.
        self.Batch.create({"name": "LB-TEST-03", "company_id": self.company.id, "zone": "6", "state": "purchased"})
        self.Batch.create({"name": "LB-TEST-04", "company_id": self.company.id})
        self.Batch.create({"name": "LB-TEST-05", "company_id": self.company.id})
        self.env.flush_all()

    def _purchase(self, batch, order, tracking):
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [[f"{order.name}/1", tracking, "UPS", "9.10"]],
        )
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"):
            batch.import_tracking(raw, filename="track.csv")

    def test_second_batch_picks_up_late_orders_after_first_is_purchased(self):
        first = self._order()
        batch = self._build()["batch"]
        self._purchase(batch, first, _T[0])
        self.assertEqual(batch.state, "purchased")

        late = self._order()
        second = self._build()["batch"]

        self.assertNotEqual(second, batch)
        self.assertEqual(second.order_ids, late)
        self.assertEqual(first.grove_label_batch_id, batch)

    def test_order_left_unbought_in_purchased_batch_can_be_rebatched(self):
        bought, unbought = self._order(), self._order()
        batch = self._build()["batch"]
        self.assertEqual(batch.order_ids, bought | unbought)
        self._purchase(batch, bought, _T[0])
        self.assertEqual(batch.state, "purchased")
        self.assertFalse(unbought.grove_tracking_numbers)

        second = self._build()["batch"]

        self.assertNotEqual(second, batch)
        self.assertEqual(second.order_ids, unbought)

    # import

    def test_import_matches_rows_across_open_batches(self):
        six = self._order()
        seven = self._order(zip_code=ZONE_7_ZIP)
        stray = self._order(checkout="paid", wave=None)
        batch_six = self._build("6")["batch"]
        batch_seven = self._build("7")["batch"]
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [
                [f"{six.name}/1", _T[0], "UPS", "9.10"],
                [f"{seven.name}/1", _T[1], "UPS", "8.20"],
                [f"{stray.name}/1", _T[2], "UPS", "7.30"],
            ],
        )
        with patch.object(
            type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"
        ) as settle:
            result = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)

        self.assertEqual(six.grove_tracking_numbers, _T[0])
        self.assertEqual(seven.grove_tracking_numbers, _T[1])
        self.assertEqual(batch_six.state, "purchased")
        self.assertEqual(batch_seven.state, "purchased")
        self.assertEqual(batch_six.tracking_import_filename, "export.csv")
        self.assertEqual(batch_seven.tracking_import_filename, "export.csv")
        # Balances are charged per order at import, the batch path's rule (GOL-2901).
        self.assertEqual(settle.call_count, 2)
        # A row for an order in no open batch still takes the single-order path.
        self.assertEqual(stray.grove_tracking_numbers, _T[2])
        self.assertFalse(stray.grove_label_batch_id)
        self.assertEqual(set(result["labelled"]), {six.name, seven.name, stray.name})
        self.assertFalse(result["review"])

    def test_email_row_matching_two_open_batches_goes_to_review(self):
        email = "both-zones@example.com"
        six = self._order(email=email)
        seven = self._order(zip_code=ZONE_7_ZIP, email=email)
        self._build("6")
        self._build("7")
        raw = self._csv(["Email", "Tracking Number", "Carrier", "Cost"], [[email, _T[0], "UPS", "9.10"]])
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"):
            result = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)
        self.assertFalse(six.grove_tracking_numbers)
        self.assertFalse(seven.grove_tracking_numbers)
        self.assertFalse(result["labelled"])
        self.assertTrue(any(email in msg for msg in result["review"]), result["review"])

    # access and the wizard

    def test_plain_internal_user_cannot_build(self):
        user = new_test_user(self.env, login="zone-batch-plain", groups="base.group_user")
        with self._on(INSIDE_WINDOW):
            with self.assertRaises(AccessError):
                self.Batch.with_user(user).build_open_batch(self.company, "6")

    def test_wizard_opens_the_built_batch(self):
        order = self._order()
        wizard = self.env["grove.label.batch.build"].create({"zone": "6", "company_id": self.company.id})
        with self._on(INSIDE_WINDOW):
            action = wizard.action_build()
        self.assertEqual(action["res_model"], "grove.label.batch")
        self.assertEqual(self.Batch.browse(action["res_id"]).order_ids, order)

    def test_wizard_reports_nothing_to_batch(self):
        wizard = self.env["grove.label.batch.build"].create({"zone": "6", "company_id": self.company.id})
        with self._on(INSIDE_WINDOW):
            action = wizard.action_build()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("Nothing to batch", action["params"]["message"])

    def test_wizard_warns_about_left_out_orders_then_opens_the_batch(self):
        self._order()
        self._order()
        wizard = self.env["grove.label.batch.build"].create({"zone": "6", "company_id": self.company.id})
        with self._on(INSIDE_WINDOW):
            with patch.object(sale_order_module, "pack_for_state", side_effect=[None, _plan()]):
                action = wizard.action_build()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("left out", action["params"]["message"])
        self.assertEqual(action["params"]["next"]["res_model"], "grove.label.batch")


@tagged("post_install", "-at_install")
class TestLabelBatchBuildRoute(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-batch-route-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _post(self, body):
        self.env.flush_all()
        response = self.url_open(
            "/grove/api/v1/labels/batch",
            data=body if isinstance(body, bytes) else json.dumps(body).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=60,
        )
        self.env.invalidate_all()
        return response

    def test_runner_build_requires_zone(self):
        for body in ({}, {"zone": ""}, {"zone": "1"}, {"zone": "6a"}, {"zone": None}, {"zone": True}, [], b"not json"):
            response = self._post(body)
            self.assertEqual(response.status_code, 400, f"{body!r}: {response.text}")

    def test_runner_build_reports_a_closed_window(self):
        with (
            patch.object(
                label_batch_module, "spring_build_window", return_value=(date(2027, 3, 31), date(2027, 4, 15))
            ),
            patch.object(fields.Date, "context_today", return_value=date(2027, 3, 1)),
        ):
            response = self._post({"zone": "6"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("March 31, 2027", response.json()["error"])

    def test_runner_build_with_nothing_eligible_returns_no_batch(self):
        with (
            patch.object(label_batch_module, "spring_build_window", return_value=(date.min, date.max)),
            patch.object(label_batch_module.GroveLabelBatch, "_eligible_orders", return_value=self.env["sale.order"]),
        ):
            response = self._post({"zone": 6})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["rows"], 0)
        self.assertIsNone(payload["batch_id"])
        self.assertIn("Nothing to batch", payload["message"])
