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
        result = self._build()
        self.assertEqual(result["batch"], batch)
        self.assertEqual(batch.order_ids, kept)
        self.assertFalse(dropped.grove_label_batch_id)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn(dropped.name, result["skipped"][0])
        self.assertIn(dropped.name, batch.notes)
        bodies = dropped.message_ids.mapped("body")
        self.assertTrue(any(f"released from {batch.name}" in body for body in bodies), bodies)
        self.assertFalse(any(f"released from {batch.name}" in body for body in kept.message_ids.mapped("body")))

    def test_rebuild_drops_an_order_labelled_since_the_last_build(self):
        kept, tracked = self._order(), self._order()
        batch = self._build()["batch"]
        tracked.grove_tracking_numbers = _T[0]

        result = self._build()

        self.assertEqual(result["batch"], batch)
        self.assertEqual(batch.line_ids.order_id, kept)
        self.assertEqual(tracked.grove_tracking_numbers, _T[0])
        self.assertFalse(tracked.grove_label_batch_id)
        self.assertIn(tracked.name, result["skipped"][0])

    def test_rebuild_leaves_an_order_now_linked_to_another_batch(self):
        kept, moved = self._order(), self._order()
        batch = self._build()["batch"]
        other = self.Batch.create({"name": "LB-TEST-OTHER", "company_id": self.company.id, "zone": "5"})
        moved.grove_label_batch_id = other

        self._build()

        self.assertEqual(batch.order_ids, kept)
        self.assertEqual(moved.grove_label_batch_id, other)

    def test_empty_plan_is_left_out_unchanged_and_named(self):
        order = self._order()
        with self._on(INSIDE_WINDOW, plan=[]):
            result = self.Batch.build_open_batch(self.company, "6")
        self.assertFalse(result["batch"])
        self.assertIn(order.name, result["skipped"][0])
        self.assertIn(order.name, result["message"])
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")

    def test_build_charges_nothing_and_sends_no_mail(self):
        self._order()
        self._order(checkout="paid")
        counts = {
            model: self.env[model].sudo().search_count([])
            for model in ("payment.transaction", "account.payment", "mail.mail")
        }
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship") as settle:
            batch = self._build()["batch"]
        self.assertEqual(len(batch.order_ids), 2)
        settle.assert_not_called()
        for model, before in counts.items():
            self.assertEqual(self.env[model].sudo().search_count([]), before, model)

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

    def _csv_refs(self, batch):
        rows = list(csv.reader(io.StringIO(batch.csv_bytes().decode("utf-8"))))
        return [row[0] for row in rows[1:]]

    def test_incomplete_tracking_order_is_held_out_until_confirmed(self):
        complete, incomplete = self._order(), self._order()
        two_boxes = _plan() + _plan()
        with self._on(INSIDE_WINDOW, plan=two_boxes):
            first = self.Batch.build_open_batch(self.company, "6")["batch"]
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [
                [f"{complete.name}/1", _T[0], "UPS", "9.10"],
                [f"{complete.name}/2", _T[1], "UPS", "9.10"],
                [f"{incomplete.name}/1", _T[2], "UPS", "9.10"],
            ],
        )
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"):
            imported = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)
        self.assertEqual(first.state, "purchased")
        self.assertFalse(incomplete.grove_tracking_numbers)
        self.assertTrue(any("incomplete tracking" in msg for msg in imported["review"]), imported["review"])

        with self._on(INSIDE_WINDOW, plan=two_boxes):
            result = self.Batch.build_open_batch(self.company, "6")

        batch = result["batch"]
        self.assertNotEqual(batch, first)
        self.assertEqual(batch.order_ids, incomplete)
        expected = f"{incomplete.name} was in {first.name} with no label recorded; confirm none was bought"
        self.assertEqual(result["needs_confirmation"], [expected])
        self.assertIn(expected, batch.notes)
        self.assertEqual(batch.line_ids.prior_batch_id, first)
        self.assertEqual(batch.unacked_flagged_count, 2)
        self.assertEqual(self._csv_refs(batch), [])

        batch.line_ids.age_ack = True
        batch._render_csv()
        self.assertEqual(self._csv_refs(batch), [f"{incomplete.name}/1", f"{incomplete.name}/2"])

        # The confirmation survives a rebuild, and the order stops being named.
        with self._on(INSIDE_WINDOW, plan=two_boxes):
            again = self.Batch.build_open_batch(self.company, "6")
        self.assertEqual(again["batch"], batch)
        self.assertEqual(again["needs_confirmation"], [])
        self.assertEqual(self._csv_refs(batch), [f"{incomplete.name}/1", f"{incomplete.name}/2"])

    def test_order_left_out_of_a_purchased_batch_import_needs_confirmation(self):
        bought, unbought = self._order(), self._order()
        batch = self._build()["batch"]
        self.assertEqual(batch.order_ids, bought | unbought)
        self._purchase(batch, bought, _T[0])
        self.assertEqual(batch.state, "purchased")
        self.assertFalse(unbought.grove_tracking_numbers)

        result = self._build()

        self.assertNotEqual(result["batch"], batch)
        self.assertEqual(result["batch"].order_ids, unbought)
        self.assertEqual(self._csv_refs(result["batch"]), [])
        self.assertIn(unbought.name, result["needs_confirmation"][0])

    def test_refunded_label_rebatches_without_confirmation(self):
        order = self._order()
        batch = self._build()["batch"]
        self._purchase(batch, order, _T[0])
        order.action_grove_label_refunded("Josh")
        self.assertFalse(order.grove_tracking_numbers)

        result = self._build()

        self.assertEqual(result["batch"].order_ids, order)
        self.assertEqual(result["needs_confirmation"], [])
        self.assertEqual(self._csv_refs(result["batch"]), [f"{order.name}/1"])

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

    # holds on import

    def _hold(self, order):
        until = INSIDE_WINDOW + timedelta(days=30)
        with self._on(INSIDE_WINDOW):
            order.action_grove_set_hold(until, "Customer away", "Josh")
        return f"{order.name}: on hold until {until.isoformat()}; release the hold and re-import"

    def _export_row(self, order, tracking=_T[0]):
        return self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [[f"{order.name}/1", tracking, "UPS", "9.10"]],
        )

    def test_held_order_row_is_not_recorded_or_charged(self):
        order = self._order()
        batch = self._build()["batch"]
        reason = self._hold(order)
        raw = self._export_row(order)

        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship") as settle:
            result = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)

        self.assertFalse(order.grove_tracking_numbers)
        settle.assert_not_called()
        self.assertIn(reason, result["review"])
        self.assertFalse(result["labelled"])
        self.assertEqual(batch.state, "exported")

        order.action_grove_release_hold("Josh")
        with patch.object(
            type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"
        ) as settle:
            result = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)

        self.assertEqual(order.grove_tracking_numbers, _T[0])
        settle.assert_called_once()
        self.assertEqual(result["labelled"], [order.name])
        self.assertFalse(result["review"])

    def test_held_order_is_not_recorded_on_a_second_import_of_the_file(self):
        """Once another order buys out the batch, the held row no longer matches an
        open batch. Importing the file again must not record it by another route."""
        held, other = self._order(), self._order()
        self._build()
        reason = self._hold(held)
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [[f"{held.name}/1", _T[0], "UPS", "9.10"], [f"{other.name}/1", _T[1], "UPS", "9.10"]],
        )
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"):
            self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)
            again = self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)
        self.assertEqual(other.grove_tracking_numbers, _T[1])
        self.assertFalse(held.grove_tracking_numbers)
        self.assertIn(reason, again["review"])

    def test_held_order_rejects_the_strict_import(self):
        order = self._order()
        batch = self._build()["batch"]
        reason = self._hold(order)
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship") as settle:
            with self.assertRaises(label_batch_module.LabelBatchError) as caught:
                batch.import_tracking(self._export_row(order), filename="track.csv")
        self.assertIn(reason, str(caught.exception))
        settle.assert_not_called()
        self.assertFalse(order.grove_tracking_numbers)

    def test_held_order_in_no_open_batch_goes_to_review(self):
        order = self._order(checkout="paid", wave=None)
        reason = self._hold(order)
        result = self.Batch.import_pirateship_file(self._export_row(order), filename="export.csv", company=self.company)
        self.assertFalse(order.grove_tracking_numbers)
        self.assertIn(reason, result["review"])

    # access and the wizard

    def test_plain_internal_user_cannot_build(self):
        user = new_test_user(self.env, login="zone-batch-plain", groups="base.group_user")
        with self._on(INSIDE_WINDOW):
            with self.assertRaises(AccessError):
                self.Batch.with_user(user).build_open_batch(self.company, "6")

    def _operator_of_main_company(self):
        return new_test_user(
            self.env,
            login="zone-batch-operator",
            groups="sales_team.group_sale_salesman_all_leads",
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )

    def test_operator_cannot_build_for_another_company(self):
        operator = self._operator_of_main_company()
        other = self.env["res.company"].create({"name": "Zone Other Co"})
        with self._on(INSIDE_WINDOW):
            with self.assertRaises(AccessError):
                self.Batch.with_user(operator).build_open_batch(other, "6")
            # Their own company is fine.
            result = self.Batch.with_user(operator).build_open_batch(self.company, "6")
        self.assertFalse(result["batch"])

    def test_wizard_refuses_and_filters_other_companies(self):
        operator = self._operator_of_main_company()
        other = self.env["res.company"].create({"name": "Zone Other Co"})
        Wizard = self.env["grove.label.batch.build"].with_user(operator)
        self.assertEqual(Wizard.fields_get(["company_id"])["company_id"]["domain"], [("id", "in", self.company.ids)])
        wizard = Wizard.create({"zone": "6", "company_id": other.id})
        with self._on(INSIDE_WINDOW):
            with self.assertRaises(AccessError):
                wizard.action_build()

    def test_wizard_names_rows_held_out_for_confirmation(self):
        bought, unbought = self._order(), self._order()
        batch = self._build()["batch"]
        self._purchase(batch, bought, _T[0])
        wizard = self.env["grove.label.batch.build"].create({"zone": "6", "company_id": self.company.id})
        with self._on(INSIDE_WINDOW):
            action = wizard.action_build()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn(f"{unbought.name} was in {batch.name}", action["params"]["message"])
        self.assertEqual(action["params"]["next"]["res_model"], "grove.label.batch")

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
