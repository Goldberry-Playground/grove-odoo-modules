"""GOL-2271 Pirate Ship label batch (spec §B1).

Covers batch building (one row per packed box, eligibility, idempotent
re-export) and the all-or-nothing reconcile (bad ref, duplicate ref,
already-tracked skip, incomplete order, and the happy path that advances the
order to label_purchased and marks the batch purchased).

The Box Engine packer + dormancy gate are stubbed at the sale_order module seam
(as test_preorder_label_skip does) so these tests isolate the batch/reconcile
logic from the live rate table and ship calendar. box_rate / box_service_title
are stubbed in the label_batch module for a deterministic Committed Rate/Service.

``post_install`` + ``GroveTaxFixtureMixin`` so product.* creates resolve a live
default tax in the minimal chartless CI database (see tests/common.py).
"""

import csv
import io
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from odoo.addons.grove_headless.models import label_batch as label_batch_module
from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged

_VALID_TRACK = ["1Z999AA10123456784", "1Z999AA10123456785", "9400111899223817200000"]


def _box(n):
    return SimpleNamespace(box_id="small", count=n)


@tagged("post_install", "-at_install")
class TestLabelBatch(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {
                "name": "Label Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "state_id": self.env.ref("base.state_us_48").id,  # WV
                "zip": "26651",
                "country_id": self.env.ref("base.us").id,
                "email": "label@example.com",
                "phone": "3045551212",
            }
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Batch Dogwood",
                "type": "consu",
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )

    def _paid_ship_order(self, qty=2.0):
        """An order paid + awaiting a label (the eligible state)."""
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "grove_fulfillment": "ship",
                    "grove_checkout_status": "paid",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty, "price_unit": 40.0})],
                }
            )
        )
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")
        return order

    @contextmanager
    def _stub_packer(self, plan):
        """Isolate build from the live rate table / ship calendar."""
        with (
            patch.object(sale_order_module, "pack_for_state", return_value=plan),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
            patch.object(sale_order_module, "can_ship_bareroot", return_value=True),
            patch.object(sale_order_module, "packing_mode", return_value="dormant"),
            patch.object(sale_order_module, "dormancy_window", return_value=object()),
            patch.object(label_batch_module, "box_rate", return_value=24.0),
            patch.object(label_batch_module, "box_service_title", return_value="UPS Ground"),
        ):
            yield

    def _build(self, plan):
        with self._stub_packer(plan):
            return self.env["grove.label.batch"].build_open_batch(self.company)

    # ── Build ────────────────────────────────────────────────────────────
    def test_build_one_row_per_packed_box(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1), _box(1)])  # two physical boxes
        self.assertEqual(batch.state, "exported")
        self.assertEqual(batch.row_count, 2)
        refs = batch.line_ids.mapped("grove_ref")
        self.assertEqual(sorted(refs), sorted([f"{order.name}/1", f"{order.name}/2"]))
        self.assertEqual(batch.expected_total, 48.0)  # 2 × 24.0
        line = batch.line_ids[0]
        self.assertEqual(line.service, "UPS Ground")
        self.assertEqual(line.committed_rate, 24.0)
        self.assertTrue(line.weight_lb >= 1.0)
        self.assertTrue(order.grove_label_batch_id == batch)
        # CSV renders header + a row per line, Grove Ref first column.
        rows = list(csv.reader(io.StringIO(batch.csv_bytes().decode("utf-8"))))
        self.assertEqual(rows[0], label_batch_module.CSV_COLUMNS)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][0], f"{order.name}/1")

    def test_name_format_and_one_open_per_company(self):
        self._paid_ship_order()
        batch = self._build([_box(1)])
        self.assertRegex(batch.name, r"^LB-\d{8}-\d{2}$")
        # A second build returns the SAME open batch (idempotent), not a new one.
        again = self._build([_box(1)])
        self.assertEqual(again, batch)

    def test_eligibility_excludes_pickup_and_tracked(self):
        eligible = self._paid_ship_order()
        pickup = self._paid_ship_order()
        pickup.grove_fulfillment = "pickup"
        tracked = self._paid_ship_order()
        tracked.grove_tracking_numbers = "1Z-ALREADY"
        batch = self._build([_box(1)])
        self.assertEqual(batch.order_ids, eligible)

    def test_idempotent_reexport_no_duplicate_rows(self):
        self._paid_ship_order()
        batch = self._build([_box(1), _box(1)])
        self.assertEqual(batch.row_count, 2)
        again = self._build([_box(1), _box(1)])
        self.assertEqual(again, batch)
        self.assertEqual(again.row_count, 2)  # rebuilt, not appended

    # ── Reconcile ──────────────────────────────────────────────────────────
    def _tracking_csv(self, rows, header=None):
        header = header or ["Grove Ref", "Tracking Number", "Carrier", "Cost"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
        return buf.getvalue().encode("utf-8")

    def test_reconcile_happy_path_advances_and_purchases(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1), _box(1)])
        r1, r2 = f"{order.name}/1", f"{order.name}/2"
        raw = self._tracking_csv([[r1, _VALID_TRACK[0], "UPS", "9.10"], [r2, _VALID_TRACK[1], "USPS", "8.36"]])
        result = batch.import_tracking(raw, filename="track.csv")
        self.assertEqual(result["orders_advanced"], 1)
        self.assertEqual(result["skipped_already_tracked"], 0)
        self.assertEqual(result["total"], 17.46)
        self.assertEqual(batch.state, "purchased")
        self.assertEqual(batch.purchased_total, 17.46)
        self.assertTrue(batch.purchased_at)
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
        self.assertEqual(order.grove_tracking_numbers, f"{_VALID_TRACK[0]}\n{_VALID_TRACK[1]}")
        self.assertEqual(order.grove_shipping_carriers, "UPS ups_ground\nUSPS usps_ground_advantage")
        self.assertEqual(order.grove_actual_shipping_cost, 17.46)

    def test_reconcile_bad_ref_writes_nothing(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1)])
        raw = self._tracking_csv([["NOPE/1", _VALID_TRACK[0], "UPS", "9.10"]])
        with self.assertRaises(label_batch_module.LabelBatchError):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)
        self.assertEqual(batch.state, "exported")

    def test_reconcile_duplicate_ref_rejected(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1), _box(1)])
        r1 = f"{order.name}/1"
        raw = self._tracking_csv([[r1, _VALID_TRACK[0], "UPS", "9.10"], [r1, _VALID_TRACK[1], "UPS", "9.10"]])
        with self.assertRaisesRegex(label_batch_module.LabelBatchError, "more than once"):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)

    def test_reconcile_invalid_tracking_rejected(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1)])
        raw = self._tracking_csv([[f"{order.name}/1", "bad%wild", "UPS", "9.10"]])
        with self.assertRaisesRegex(label_batch_module.LabelBatchError, "invalid tracking"):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)

    def test_reconcile_incomplete_order_rejected(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1), _box(1)])  # two boxes
        raw = self._tracking_csv([[f"{order.name}/1", _VALID_TRACK[0], "UPS", "9.10"]])  # only one
        with self.assertRaisesRegex(label_batch_module.LabelBatchError, "incomplete"):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)

    def test_reconcile_unknown_carrier_rejected(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1)])
        raw = self._tracking_csv([[f"{order.name}/1", _VALID_TRACK[0], "DHL", "9.10"]])
        with self.assertRaisesRegex(label_batch_module.LabelBatchError, "carrier"):
            batch.import_tracking(raw)
        self.assertFalse(order.grove_tracking_numbers)

    def test_reconcile_already_tracked_is_skipped(self):
        order = self._paid_ship_order()
        batch = self._build([_box(1)])
        raw = self._tracking_csv([[f"{order.name}/1", _VALID_TRACK[0], "UPS", "9.10"]])
        first = batch.import_tracking(raw)
        self.assertEqual(first["orders_advanced"], 1)
        # Re-import the same file: idempotent skip, no error, no re-write.
        second = batch.import_tracking(raw)
        self.assertEqual(second["orders_advanced"], 0)
        self.assertEqual(second["skipped_already_tracked"], 1)
        self.assertEqual(order.grove_tracking_numbers, _VALID_TRACK[0])
