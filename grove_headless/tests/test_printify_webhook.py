"""Odoo-runtime tests for the Printify shipment/order webhook (spec §7, GOL-3314)."""

import hashlib
import hmac
import json
import os
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client
from odoo.tests import tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture

SECRET = "whsec_test"
ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42", "PRINTIFY_WEBHOOK_SECRET": SECRET}


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyWebhook(_PrintifyFixture, GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.write({"printify_order_id": "po_1", "printify_state": "in_production", "state": "sale"})

    def _event(self, kind):
        return {"id": "evt", "type": kind, "resource": {"id": "po_1", "type": "order", "data": {}}}

    def _shipped_order(self, delivered=False):
        return {
            "id": "po_1",
            "status": "fulfilled",
            "shipments": [
                {
                    "carrier": "usps",
                    "number": "9400111",
                    "url": "https://t",
                    "delivered_at": "2026-10-20" if delivered else None,
                }
            ],
        }

    def test_bad_signature_401(self):
        body = json.dumps(self._event("order:updated")).encode()
        with mock.patch.dict(os.environ, ENV, clear=False):
            resp = self.url_open(
                "/grove/api/v1/printify/webhook",
                data=body,
                headers={
                    "X-Pfy-Signature": "sha256=bad",
                    "Content-Type": "application/json",
                    "X-Odoo-Database": get_db_name(),
                },
            )
        self.assertEqual(resp.status_code, 401)

    def test_good_signature_200(self):
        body = json.dumps(self._event("order:sent-to-production")).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, ENV, clear=False):
            resp = self.url_open(
                "/grove/api/v1/printify/webhook",
                data=body,
                headers={"X-Pfy-Signature": sig, "Content-Type": "application/json", "X-Odoo-Database": get_db_name()},
            )
        self.assertEqual(resp.status_code, 200)

    def test_shipment_created_writes_tracking_then_transit(self):
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(printify_client, "get_order", return_value=self._shipped_order()),
            mock.patch.object(grove_main, "_apply_delivery_status", return_value=True) as apply,
        ):
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(self.order.grove_tracking_numbers, "9400111")
        self.assertEqual(self.order.grove_shipping_carriers, "USPS")
        self.assertEqual(apply.call_args.args[2], "transit")
        self.assertEqual(apply.call_args.kwargs["source"], "printify")
        self.assertEqual(self.order.printify_state, "shipped")

    def test_delivered_then_late_shipment_never_regresses(self):
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(printify_client, "get_order", return_value=self._shipped_order(delivered=True)),
            mock.patch.object(grove_main, "_notify_shipping_status"),
        ):
            grove_main._handle_printify_event(self.env, self._event("order:shipment:delivered"))
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(self.order.grove_delivery_status, "delivered")
        self.assertEqual(self.order.printify_state, "delivered")

    def test_duplicate_shipment_event_emails_once(self):
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(printify_client, "get_order", return_value=self._shipped_order()),
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(notify.call_count, 1)

    def test_canceled_alerts(self):
        event = self._event("order:updated")
        event["resource"]["data"] = {"status": "canceled"}
        with mock.patch.dict(os.environ, ENV, clear=False), mock.patch.object(grove_main, "_notify_discord") as discord:
            grove_main._handle_printify_event(self.env, event)
        self.assertEqual(self.order.printify_state, "canceled")
        discord.assert_called_once()

    def test_unknown_order_ignored(self):
        event = self._event("order:updated")
        event["resource"]["id"] = "po_unknown"
        self.assertEqual(grove_main._handle_printify_event(self.env, event), "ignored")
