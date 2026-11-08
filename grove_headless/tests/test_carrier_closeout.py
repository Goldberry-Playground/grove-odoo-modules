"""Carrier scan closes the order out in Odoo (Josh, 2026-11-08).

When the carrier poll (or Shippo, or the operator button) reports progress,
``_apply_delivery_status`` now also mirrors it onto Odoo's own records:

  * a shipped / out-for-delivery / delivered scan validates the outgoing
    picking for the RESERVED quantity; unreserved preorder lines go to a
    backorder instead of pushing stock negative;
  * a delivered scan locks the order once every outgoing picking is done and
    the checkout is fully paid;
  * a picking that refuses to validate rolls back on its own, leaves a chatter
    note, and never undoes the delivery status or the customer email.

Needs a DB (sale.order + stock), so it is listed in tests/__init__.py AND
excluded from pytest in conftest.py (the double-skip guard, GOL-1936).
"""

from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import carrier_tracking
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


class _FakeClient:
    def __init__(self, status):
        self._status = status

    def track(self, _tracking_number):
        return self._status


@tagged("post_install", "-at_install")
class TestCarrierCloseOut(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.partner = self.env["res.partner"].create(
            {"name": "Closeout Customer", "email": "closeout@example.com", "company_id": self.company.id}
        )
        self.potted = self.env["product.product"].create(
            {"name": "PawPaw (Potted)", "type": "consu", "is_storable": True, "list_price": 30.0}
        )
        self.bareroot = self.env["product.product"].create(
            {"name": "Serviceberry (Bareroot)", "type": "consu", "is_storable": True, "list_price": 18.0}
        )
        self.env["stock.quant"]._update_available_quantity(self.potted, self.warehouse.lot_stock_id, 10.0)

    def _order(self, products, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "warehouse_id": self.warehouse.id,
            "order_line": [(0, 0, {"product_id": p.id, "product_uom_qty": qty}) for p, qty in products],
        }
        order = self.env["sale.order"].with_company(self.company).create(base)
        order.action_confirm()
        order.picking_ids.action_assign()
        # Mirror a real order right after label purchase (see test_carrier_tracking_cron).
        order.write(
            {
                "grove_fulfillment": "ship",
                "grove_checkout_status": "paid",
                "grove_fulfillment_state": "label_purchased",
                "grove_delivery_status": "label_purchased",
                "grove_tracking_numbers": "1Z999AA10123456784",
                "grove_shipping_carriers": "UPS",
                "grove_label_purchased_at": fields.Datetime.now(),
                **vals,
            }
        )
        return order

    def _poll(self, status):
        with (
            mock.patch.object(
                carrier_tracking, "build_clients", return_value={"UPS": _FakeClient(status), "USPS": None}
            ),
            mock.patch.object(grove_main, "_notify_discord"),
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            self.env["sale.order"].sudo()._cron_poll_carrier_tracking()
        return notify

    def _outgoing(self, order):
        return order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")

    def test_shipped_validates_reserved_picking_delivered_locks(self):
        order = self._order([(self.potted, 2)])
        self.assertEqual(self._outgoing(order).state, "assigned")

        self._poll(carrier_tracking.STATUS_TRANSIT)
        self.assertEqual(self._outgoing(order).state, "done", "a shipped scan validates the delivery")
        self.assertEqual(order.delivery_status, "full")
        self.assertFalse(order.locked, "shipped is not yet closed out")

        self._poll(carrier_tracking.STATUS_DELIVERED)
        self.assertEqual(order.grove_delivery_status, "delivered")
        self.assertTrue(order.locked, "delivered + fully paid locks the order")

    def test_unreserved_preorder_line_goes_to_backorder_and_blocks_lock(self):
        order = self._order([(self.potted, 1), (self.bareroot, 2)])

        self._poll(carrier_tracking.STATUS_TRANSIT)
        pickings = self._outgoing(order)
        done = pickings.filtered(lambda p: p.state == "done")
        still_open = pickings - done
        self.assertEqual(len(done), 1)
        self.assertEqual(done.move_ids.filtered(lambda m: m.state == "done").product_id, self.potted)
        self.assertEqual(len(still_open), 1, "the bareroot remainder sits on a backorder")
        self.assertEqual(still_open.move_ids.product_id, self.bareroot)
        self.assertGreaterEqual(self.bareroot.qty_available, 0, "stock is never forced negative")

        self._poll(carrier_tracking.STATUS_DELIVERED)
        self.assertFalse(order.locked, "an open backorder keeps the order unlocked")

    def test_nothing_reserved_leaves_picking_open_with_note(self):
        order = self._order([(self.bareroot, 1)])
        self._poll(carrier_tracking.STATUS_TRANSIT)
        self.assertNotIn(self._outgoing(order).state, ("done", "cancel"))
        self.assertTrue(any("nothing reserved" in (m.body or "") for m in order.message_ids))

    def test_delivered_with_unsettled_balance_is_not_locked(self):
        order = self._order([(self.potted, 1)], grove_checkout_status="settlement_failed")
        self._poll(carrier_tracking.STATUS_DELIVERED)
        self.assertEqual(self._outgoing(order).state, "done")
        self.assertFalse(order.locked, "the settlement retry must still be able to edit the order")

    def test_validation_failure_never_undoes_the_scan(self):
        order = self._order([(self.potted, 1)])
        picking_cls = type(self.env["stock.picking"])
        with mock.patch.object(picking_cls, "button_validate", side_effect=RuntimeError("lot required")):
            notify = self._poll(carrier_tracking.STATUS_TRANSIT)
        self.assertEqual(order.grove_delivery_status, "transit", "the scan still lands")
        notify.assert_called_once()
        self.assertNotIn(self._outgoing(order).state, ("done", "cancel"))
        self.assertTrue(any("could not be validated" in (m.body or "") for m in order.message_ids))

    def test_repeat_scan_is_idempotent(self):
        order = self._order([(self.potted, 1)])
        self._poll(carrier_tracking.STATUS_TRANSIT)
        notes = len(order.message_ids)
        self._poll(carrier_tracking.STATUS_TRANSIT)
        self.assertEqual(len(order.message_ids), notes, "a replayed scan writes nothing")
