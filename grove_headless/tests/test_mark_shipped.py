"""Tests for the operator Mark-Shipped transition (GOL-1980).

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest via conftest — the pattern
every TransactionCase here follows so it is not double-skipped (GOL-1936).
"""

from unittest import mock

from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestMarkShipped(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env["res.partner"].create(
            {"name": "Ship Customer", "email": "ship@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "American Plum", "type": "consu", "is_storable": True, "list_price": 22.0}
        )

    def _make_order(self):
        return (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
                }
            )
        )

    def test_marks_shipped_and_fires_email_once(self):
        order = self._make_order()
        with mock.patch.object(type(order), "_grove_send_shipment_email") as send:
            result = order._grove_mark_shipped(actor="208085380262526976")
        self.assertTrue(result["newly_shipped"])
        self.assertEqual(order.grove_delivery_status, "shipped")
        send.assert_called_once()

    def test_double_click_is_noop_and_does_not_re_email(self):
        order = self._make_order()
        with mock.patch.object(type(order), "_grove_send_shipment_email") as send:
            first = order._grove_mark_shipped(actor="1")
            second = order._grove_mark_shipped(actor="1")
        self.assertTrue(first["newly_shipped"])
        self.assertFalse(second["newly_shipped"])
        self.assertEqual(order.grove_delivery_status, "shipped")
        # The customer email fires only on the real transition — a double-click
        # must never double-send (GOL-1975 guard).
        send.assert_called_once()

    def test_operator_id_recorded_in_chatter(self):
        order = self._make_order()
        with mock.patch.object(type(order), "_grove_send_shipment_email"):
            order._grove_mark_shipped(actor="998877")
        stamped = order.message_ids.filtered(lambda m: "998877" in (m.body or ""))
        self.assertTrue(stamped, "operator id should be stamped into the chatter for the audit trail")

    def test_missing_phase3_template_does_not_raise(self):
        # The real hook must degrade to a log (not raise) when the Phase-3
        # shipment template is absent, so mark-shipped never fails on the email.
        order = self._make_order()
        order._grove_mark_shipped(actor="1")
        self.assertEqual(order.grove_delivery_status, "shipped")
