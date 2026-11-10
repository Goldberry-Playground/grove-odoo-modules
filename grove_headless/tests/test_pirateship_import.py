"""Tolerant Pirate Ship export import (the operator's "Import Pirate Ship export").

The batch reconcile (``import_tracking``) is all-or-nothing by design, which is
right for the automated runner but dead-ends the operator: a real Pirate Ship
export covers a date range, so it nearly always carries a row Odoo cannot place
(a hand-bought label, a reprint, a refunded label, last week's shipments) and
the whole upload bounced. ``grove.label.batch.import_pirateship_file`` records
every row it can, treats a new tracking number on an already-labelled order as a
replacement label, flags refunded labels, and lists the rest for a human,
writing nothing for those rows.
"""

import base64
import csv
import io
from contextlib import contextmanager
from unittest.mock import patch

from odoo.addons.grove_headless.models import label_batch as label_batch_module
from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.models.shipping_boxes import PackedBox
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

_T = ["1Z999AA10123456784", "1Z999AA10123456785", "1Z999AA10123456786", "9400111899223817200000"]


@tagged("post_install", "-at_install")
class TestPirateShipImport(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.product = self.env["product.product"].create(
            {
                "name": "Import Dogwood",
                "type": "consu",
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )
        self.Batch = self.env["grove.label.batch"]

    def _partner(self, email):
        return self.env["res.partner"].create(
            {
                "name": f"Customer {email}",
                "street": "1 Grove Way",
                "city": "Summersville",
                "state_id": self.env.ref("base.state_us_48").id,
                "zip": "26651",
                "country_id": self.env.ref("base.us").id,
                "email": email,
            }
        )

    def _order(self, email="buyer@example.com"):
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self._partner(email).id,
                    "company_id": self.company.id,
                    "grove_fulfillment": "ship",
                    "grove_checkout_status": "paid",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "price_unit": 40.0})],
                }
            )
        )
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")
        return order

    def _labelled_order(self, tracking, email="buyer@example.com"):
        order = self._order(email)
        order.action_grove_record_hand_label(tracking, carrier="UPS", actual_cost=9.0)
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        return order

    @contextmanager
    def _stub_packer(self):
        with (
            patch.object(sale_order_module, "pack_for_state", return_value=[PackedBox(box_id="p24x10x4", count=1)]),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
            patch.object(sale_order_module, "can_ship_bareroot", return_value=True),
            patch.object(sale_order_module, "packing_mode", return_value="dormant"),
            patch.object(sale_order_module, "dormancy_window", return_value=object()),
            patch.object(label_batch_module, "box_rate", return_value=24.0),
            patch.object(label_batch_module, "box_service_title", return_value="UPS Ground"),
        ):
            yield

    def _csv(self, header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    def _import(self, raw, filename="export.csv"):
        return self.Batch.import_pirateship_file(raw, filename=filename, company=self.company)

    # ── Batch rows + strays in one file ────────────────────────────────────
    def test_batch_rows_recorded_and_stray_rows_listed_not_fatal(self):
        order = self._order()
        with self._stub_packer():
            batch = self.Batch.build_open_batch(self.company)
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [[f"{order.name}/1", _T[0], "UPS", "9.10"], ["S99999/1", _T[1], "UPS", "8.00"]],
        )
        result = self._import(raw)
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertEqual(batch.state, "purchased")
        self.assertIn(order.name, result["labelled"])
        self.assertTrue(any("S99999" in msg for msg in result["review"]), result["review"])

    def test_strict_batch_import_still_all_or_nothing(self):
        """The runner endpoint keeps the all-or-nothing contract."""
        order = self._order()
        with self._stub_packer():
            batch = self.Batch.build_open_batch(self.company)
        raw = self._csv(
            ["Grove Ref", "Tracking Number", "Carrier", "Cost"],
            [[f"{order.name}/1", _T[0], "UPS", "9.10"], ["S99999/1", _T[1], "UPS", "8.00"]],
        )
        with self.assertRaises(label_batch_module.LabelBatchError):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)

    # ── No open batch: single orders ──────────────────────────────────────
    def test_no_batch_records_label_by_order_number(self):
        order = self._order()
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[0], "UPS", "11.25"]])
        result = self._import(raw)
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertEqual(order.grove_shipping_carriers, "UPS")
        self.assertEqual(order.grove_actual_shipping_cost, 11.25)
        self.assertEqual(result["labelled"], [order.name])
        self.assertEqual(result["review"], [])

    def test_no_batch_records_label_by_unique_email(self):
        order = self._order("solo@example.com")
        raw = self._csv(
            ["Recipient", "Email", "Tracking Number", "Carrier", "Cost"],
            [["Solo", "Solo@Example.com", _T[0], "UPS", "9.00"]],
        )
        result = self._import(raw)
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertIn(order.name, result["manual_review"])

    def test_ambiguous_email_is_listed_and_nothing_written(self):
        a = self._order("twice@example.com")
        b = self._order("twice@example.com")
        raw = self._csv(["Email", "Tracking Number", "Carrier", "Cost"], [["twice@example.com", _T[0], "UPS", "9.00"]])
        result = self._import(raw)
        self.assertFalse(a.grove_tracking_numbers)
        self.assertFalse(b.grove_tracking_numbers)
        self.assertTrue(any(a.name in msg and b.name in msg for msg in result["review"]), result["review"])

    def test_xlsx_export_imports_without_a_batch(self):
        import openpyxl

        order = self._order()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Order", "Tracking Number", "Carrier", "Cost"])
        ws.append([order.name, _T[0], "UPS", 9.5])
        buf = io.BytesIO()
        wb.save(buf)
        result = self._import(buf.getvalue(), filename="pirateship.xlsx")
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertEqual(result["labelled"], [order.name])

    # ── Replacement labels ───────────────────────────────────────────────
    def test_new_tracking_on_labelled_order_replaces_the_label(self):
        order = self._labelled_order(_T[0])
        order.grove_carrier_poll_stopped = True
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[1], "UPS", "12.00"]])
        result = self._import(raw)
        self.assertEqual(order.grove_tracking_numbers, _T[1])
        self.assertEqual(order.grove_actual_shipping_cost, 12.0)
        self.assertFalse(order.grove_carrier_poll_stopped, "the poll watches the new label")
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertTrue(any(_T[0] in r and _T[1] in r for r in result["replaced"]), result["replaced"])
        bodies = " ".join(order.message_ids.mapped("body"))
        self.assertIn(_T[0], bodies)
        self.assertIn(_T[1], bodies)

    def test_same_tracking_reimported_is_a_noop(self):
        order = self._labelled_order(_T[0])
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[0], "UPS", "9.00"]])
        result = self._import(raw)
        self.assertEqual(result["already"], 1)
        self.assertEqual(result["replaced"], [])
        self.assertEqual(order.grove_tracking_numbers, _T[0])

    def test_one_label_listed_for_two_orders_needs_review(self):
        """Two orders shipped in one box: the same tracking can't silently land
        on both — record it by hand."""
        a = self._order("goode@example.com")
        b = self._order("goode@example.com")
        raw = self._csv(
            ["Email", "Tracking Number", "Carrier", "Cost"],
            [["goode@example.com", _T[2], "UPS", "14.00"]],
        )
        result = self._import(raw)
        self.assertFalse(a.grove_tracking_numbers or b.grove_tracking_numbers)
        self.assertTrue(result["review"])

    def test_replace_refuses_tracking_already_on_another_order(self):
        self._labelled_order(_T[0], email="one@example.com")
        other = self._labelled_order(_T[1], email="two@example.com")
        with self.assertRaises(UserError):
            other.action_grove_replace_label(_T[0], carrier="UPS", actual_cost=9.0)
        self.assertEqual(other.grove_tracking_numbers, _T[1])

    def test_replace_allowed_on_a_shipped_order(self):
        order = self._labelled_order(_T[0])
        order.action_grove_mark_shipped(source="test")
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertTrue(order.action_grove_replace_label(_T[1], carrier="UPS", actual_cost=9.0))
        self.assertEqual(order.grove_tracking_numbers, _T[1])

    def test_old_export_row_cannot_overwrite_a_delivered_order(self):
        order = self._labelled_order(_T[0])
        order.action_grove_mark_shipped(source="test")
        order.action_grove_mark_delivered(source="test")
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[1], "UPS", "9.00"]])
        result = self._import(raw)
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertEqual(result["replaced"], [])
        self.assertTrue(any(order.name in msg for msg in result["review"]), result["review"])

    # ── Access ───────────────────────────────────────────────────────────
    def test_own_documents_salesman_cannot_import(self):
        order = self._order()
        user = new_test_user(self.env, login="own_docs_only", groups="sales_team.group_sale_salesman")
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[0], "UPS", "9.00"]])
        with self.assertRaises(AccessError):
            self.Batch.with_user(user).import_pirateship_file(raw, filename="export.csv", company=self.company)
        self.assertFalse(order.grove_tracking_numbers)

    # ── Refunds ──────────────────────────────────────────────────────────
    def test_refunded_row_flags_order_and_stops_the_poll(self):
        order = self._labelled_order(_T[0])
        raw = self._csv(
            ["Order", "Tracking Number", "Carrier", "Cost", "Status"],
            [[order.name, _T[0], "UPS", "9.00", "Refunded"]],
        )
        result = self._import(raw)
        self.assertTrue(order.grove_carrier_poll_stopped)
        self.assertEqual(order.grove_tracking_numbers, _T[0], "the refund flags, it does not erase")
        self.assertTrue(any(order.name in r for r in result["refunded"]), result["refunded"])
        # Re-importing the same file does not stack chatter notes.
        before = len(order.message_ids)
        self._import(raw)
        self.assertEqual(len(order.message_ids), before)

    def test_refund_plus_replacement_in_one_file_ends_on_new_label(self):
        order = self._labelled_order(_T[0])
        raw = self._csv(
            ["Order", "Tracking Number", "Carrier", "Cost", "Status"],
            [
                [order.name, _T[0], "UPS", "9.00", "Refunded"],
                [order.name, _T[1], "UPS", "10.00", "Delivered"],
            ],
        )
        result = self._import(raw)
        self.assertEqual(order.grove_tracking_numbers, _T[1])
        self.assertFalse(order.grove_carrier_poll_stopped)
        self.assertEqual(result["refunded"], [])

    # ── Wizard ───────────────────────────────────────────────────────────
    def test_wizard_works_without_a_batch(self):
        order = self._order()
        raw = self._csv(["Order", "Tracking Number", "Carrier", "Cost"], [[order.name, _T[0], "UPS", "9.00"]])
        wiz = self.env["grove.label.batch.import"].create({"data": base64.b64encode(raw), "filename": "export.csv"})
        action = wiz.action_import()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn(order.name, action["params"]["message"])
        self.assertEqual(order.grove_tracking_numbers, _T[0])

    def test_hand_label_wizard_replaces_an_existing_label(self):
        order = self._labelled_order(_T[0])
        wiz = (
            self.env["grove.hand.label"]
            .with_context(active_id=order.id)
            .create({"order_id": order.id, "tracking_number": _T[1], "carrier": "UPS", "actual_cost": 10.0})
        )
        wiz.action_record()
        self.assertEqual(order.grove_tracking_numbers, _T[1])
