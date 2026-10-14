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

    def _cancel_event(self, reason="out_of_stock"):
        event = self._event("order:updated")
        event["resource"]["data"] = {"status": "canceled", "reason": reason}
        return event

    def test_in_production_cancel_alerts_only_no_refund(self):
        # setUp leaves the order in_production: a cancel here is NEVER auto-refunded
        # (the shirt may already be printed) — ops is alerted to handle it by hand.
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(grove_main.stripe_gateway, "create_refund") as refund,
            mock.patch.object(grove_main, "_notify_printify_cancel_customer") as email,
            mock.patch.object(grove_main, "_notify_discord") as discord,
        ):
            action = grove_main._handle_printify_event(self.env, self._cancel_event())
        self.assertEqual(action, "canceled_in_production")
        self.assertEqual(self.order.printify_state, "canceled")
        refund.assert_not_called()
        email.assert_not_called()
        discord.assert_called_once()
        self.assertFalse(self.order.printify_refund_id)

    def test_paid_cancel_refunds_once_and_emails(self):
        # A paid, pre-production order: full auto-refund + branded apology + cancel.
        self.order.write(
            {
                "printify_state": "created",
                "grove_checkout_status": "paid",
                "grove_stripe_payment_intent": "pi_shirt_1",
            }
        )
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(grove_main.stripe_gateway, "create_refund", return_value={"id": "re_1"}) as refund,
            mock.patch.object(grove_main, "_notify_printify_cancel_customer") as email,
            mock.patch.object(grove_main, "_notify_discord"),
        ):
            action = grove_main._handle_printify_event(self.env, self._cancel_event())
        self.assertEqual(action, "canceled")
        refund.assert_called_once()
        self.assertEqual(refund.call_args.args[1], "pi_shirt_1")
        self.assertEqual(
            refund.call_args.kwargs["idempotency_key"], f"printify-cancel-refund:{self.order.name}:pi_shirt_1"
        )
        email.assert_called_once()
        self.assertEqual(self.order.printify_refund_id, "re_1")
        self.assertEqual(self.order.printify_state, "canceled")
        self.assertEqual(self.order.state, "cancel")

    def test_redelivered_cancel_does_not_refund_twice(self):
        self.order.write(
            {
                "printify_state": "created",
                "grove_checkout_status": "paid",
                "grove_stripe_payment_intent": "pi_shirt_1",
            }
        )
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(grove_main.stripe_gateway, "create_refund", return_value={"id": "re_1"}) as refund,
            mock.patch.object(grove_main, "_notify_printify_cancel_customer") as email,
            mock.patch.object(grove_main, "_notify_discord"),
        ):
            grove_main._handle_printify_event(self.env, self._cancel_event())
            # Printify redelivers the same cancel: the recorded refund id short-circuits.
            second = grove_main._handle_printify_event(self.env, self._cancel_event())
        self.assertEqual(second, "canceled")
        refund.assert_called_once()
        email.assert_called_once()
        self.assertEqual(self.order.printify_refund_id, "re_1")

    def test_unknown_order_ignored(self):
        event = self._event("order:updated")
        event["resource"]["id"] = "po_unknown"
        self.assertEqual(grove_main._handle_printify_event(self.env, event), "ignored")
