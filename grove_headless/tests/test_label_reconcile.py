"""Legacy label reconcile wizard (GOL-3091, split from GOL-3083 item 2).

The one-time cleanup: orders Josh shipped by hand in Pirate Ship before the Odoo
ship flow existed still sit "awaiting a label" in Odoo. This wizard matches the
Pirate Ship Shipments export to those open orders by recipient email + name and
records the historical label with one click — WITHOUT re-emailing a customer
whose order delivered weeks ago (the exact GOL-3083 harm) and WITHOUT re-settling.

What must hold (GOL-3083 item 5, reconcile portion):
  * matching is by email + name and REFUSES ambiguity — two open orders for the
    same recipient are flagged ambiguous and offer no one-click Record;
  * a matched legacy order records its historical label and sends NO customer
    shipment email (the branded notify path is never reached) and does NOT settle;
  * recording leaves the order OUT of the eligible pool (it will never be
    re-proposed for a Pirate Ship label again).

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest in conftest.py (GOL-1936).
"""

import base64
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin

UPS_TRACKING = "1Z21W6C10310589105"


def _export_csv(rows):
    """Build a Pirate Ship Shipments-export CSV (bytes) from row tuples
    (recipient, email, tracking, carrier, cost, status, date)."""
    header = "Recipient,Email,Tracking Number,Carrier,Cost,Status,Delivered Date"
    body = "\n".join(",".join(str(c) for c in r) for r in rows)
    return f"{header}\n{body}\n".encode("utf-8")


@tagged("post_install", "-at_install")
class TestLabelReconcile(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.product = self.env["product.product"].create(
            {"name": "Legacy Bareroot Fig", "type": "consu", "is_storable": True, "list_price": 42.0}
        )

    def _partner(self, name, email):
        return self.env["res.partner"].create({"name": name, "email": email, "company_id": self.company.id})

    def _open_order(self, partner):
        """A paid SHIP order awaiting a label — the state a legacy hand-shipped
        order is stuck in because its label was never recorded in Odoo."""
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "grove_fulfillment": "ship",
                    "grove_checkout_status": "paid",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
                }
            )
        )
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")
        return order

    def _scan(self, raw):
        wizard = self.env["grove.label.reconcile"].create({"data": base64.b64encode(raw), "filename": "shipments.csv"})
        wizard.action_scan()
        return wizard

    def _eligible(self):
        Batch = self.env["grove.label.batch"]
        return Batch.with_company(self.company)._eligible_orders(self.company)

    # ── matching by email + name ──────────────────────────────────────────

    def test_matches_by_email_and_name(self):
        partner = self._partner("Jennifer Scott", "jen@example.com")
        order = self._open_order(partner)
        wizard = self._scan(
            _export_csv(
                [("Jennifer Scott", "jen@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")]
            )
        )
        line = wizard.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.status, "matched")
        self.assertEqual(line.order_id, order)
        self.assertTrue(line.delivered)

    def test_refuses_ambiguous_same_recipient(self):
        partner = self._partner("Same Person", "same@example.com")
        self._open_order(partner)
        self._open_order(partner)  # two OPEN orders, identical recipient
        wizard = self._scan(
            _export_csv([("Same Person", "same@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")])
        )
        line = wizard.line_ids
        self.assertEqual(line.status, "ambiguous")
        self.assertFalse(line.order_id)  # never auto-applied to either order
        with self.assertRaises(UserError):
            line.action_record_one()  # the one-click path refuses an ambiguous row

    def test_unmatched_when_no_open_order(self):
        wizard = self._scan(
            _export_csv([("Ghost Buyer", "ghost@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")])
        )
        self.assertEqual(wizard.line_ids.status, "unmatched")

    # ── recording a match: no customer email, leaves the pool ─────────────

    def test_record_sends_no_customer_email_and_lands_delivered(self):
        partner = self._partner("Delivered Dan", "dan@example.com")
        order = self._open_order(partner)
        wizard = self._scan(
            _export_csv([("Delivered Dan", "dan@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")])
        )
        # The branded shipment email + settlement live in the controller ship
        # orchestration; the historical path must never reach either.
        with (
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
            mock.patch.object(grove_main, "settle_order_at_ship") as settle,
        ):
            wizard.line_ids.action_record_one()

        notify.assert_not_called()  # NO customer shipment email
        settle.assert_not_called()  # NO re-settlement weeks later
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        self.assertEqual(order.grove_tracking_numbers, UPS_TRACKING)
        self.assertEqual(order.grove_shipping_carriers, "UPS")
        self.assertEqual(order.grove_actual_shipping_cost, 8.79)
        # Historical purchase date preserved, not stamped as now().
        self.assertEqual(str(order.grove_label_purchased_at)[:10], "2026-09-15")
        # The checkout status is untouched — the balance was NOT re-captured.
        self.assertEqual(order.grove_checkout_status, "paid")

    def test_record_leaves_order_out_of_eligible_pool(self):
        partner = self._partner("Pool Pat", "pat@example.com")
        order = self._open_order(partner)
        self.assertIn(order, self._eligible())  # eligible before recording
        wizard = self._scan(
            _export_csv([("Pool Pat", "pat@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")])
        )
        with (
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.object(grove_main, "settle_order_at_ship"),
        ):
            wizard.line_ids.action_record_one()
        self.assertNotIn(order, self._eligible())  # never re-proposed for a label

    def test_not_delivered_status_lands_at_shipped(self):
        partner = self._partner("Transit Tess", "tess@example.com")
        order = self._open_order(partner)
        wizard = self._scan(
            _export_csv(
                [("Transit Tess", "tess@example.com", UPS_TRACKING, "UPS", "$8.79", "In Transit", "2026-09-30")]
            )
        )
        self.assertFalse(wizard.line_ids.delivered)
        with (
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.object(grove_main, "settle_order_at_ship"),
        ):
            wizard.line_ids.action_record_one()
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    def test_already_recorded_tracking_is_flagged_duplicate(self):
        partner = self._partner("Done Dana", "dana@example.com")
        order = self._open_order(partner)
        # Record it once via the historical path.
        order.action_grove_record_historical_label(UPS_TRACKING, carrier="UPS", actual_cost=8.79)
        # A fresh export carrying the SAME tracking has nothing left to do.
        wizard = self._scan(
            _export_csv([("Done Dana", "dana@example.com", UPS_TRACKING, "UPS", "$8.79", "Delivered", "2026-09-15")])
        )
        self.assertEqual(wizard.line_ids.status, "duplicate")

    # ── the model method's guards ─────────────────────────────────────────

    def test_historical_record_refuses_pickup(self):
        partner = self._partner("Pickup Pete", "pete@example.com")
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "grove_fulfillment": "pickup",
                    "grove_checkout_status": "paid",
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
                }
            )
        )
        with self.assertRaises(UserError):
            order.action_grove_record_historical_label(UPS_TRACKING, carrier="UPS", actual_cost=8.79)
