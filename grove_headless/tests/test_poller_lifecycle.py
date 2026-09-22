"""Carrier-poll-driven customer emails + Odoo close (GOL-2429, Phase 3 of GOL-1975).

The carrier poll (GOL-2272) is now the automatic source of shipped/delivered
events; the Discord Mark-Shipped button (GOL-1980) stays the manual one. Both,
plus the legacy Shippo webhook, fold into ``_apply_delivery_status``. What must
hold, driven through the real cron with fake carrier clients:

  * first in-transit scan -> ONE branded shipped email + stage ``shipped``;
  * delivered scan -> ONE delivered email + terminal ``delivered`` (closed);
  * repeat polls, a lagging box, or a replayed/older event never re-email and
    never re-transition;
  * operator marks shipped, then the poll confirms -> still one shipped email;
  * pickup orders and pre-wave preorder deposits get no email and no close;
  * the notice carries the persisted carrier + tracking, not a hard-coded UPS.

Needs a DB (sale.order), so it is listed in tests/__init__.py AND excluded from
pytest in conftest.py (the double-skip guard, GOL-1936).
"""

from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import carrier_tracking
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin

USPS_NUMBER = "9400111899223197428490"


class _FakeClient:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def track(self, tracking_number):
        self.calls.append(tracking_number)
        return self.status(tracking_number) if callable(self.status) else self.status


@tagged("post_install", "-at_install")
class TestPollerLifecycle(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Poll Customer", "email": "poll@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Pawpaw", "type": "consu", "is_storable": True, "list_price": 30.0}
        )

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
            "grove_fulfillment": "ship",
            "grove_checkout_status": "paid",
            "grove_fulfillment_state": "label_purchased",
            "grove_delivery_status": "label_purchased",
            "grove_tracking_numbers": USPS_NUMBER,
            "grove_shipping_carriers": "USPS",
            "grove_label_purchased_at": fields.Datetime.now(),
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _poll(self, status, usps=True):
        """One cron run with every box reporting ``status``; returns the list of
        notice statuses the branded-email path was asked to send."""
        client = _FakeClient(status)
        clients = {"UPS": None if usps else client, "USPS": client if usps else None}
        sent = []
        with (
            mock.patch.object(carrier_tracking, "build_clients", return_value=clients),
            mock.patch.object(grove_main, "_notify_discord"),
            mock.patch.object(
                grove_main, "_notify_shipping_status", side_effect=lambda env, o, s, t: sent.append((o.id, s))
            ),
        ):
            self.env["sale.order"].sudo()._cron_poll_carrier_tracking()
        return sent

    # ── shipped email once ───────────────────────────────────────────────

    def test_first_transit_scan_ships_and_emails_once(self):
        order = self._order()
        self.assertEqual(self._poll(carrier_tracking.STATUS_TRANSIT), [(order.id, "transit")])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertTrue(order.grove_is_outstanding)
        # The poll re-delivers the same scan every run: nothing new goes out.
        self.assertEqual(self._poll(carrier_tracking.STATUS_TRANSIT), [])
        self.assertEqual(self._poll(carrier_tracking.STATUS_TRANSIT), [])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    # ── delivered closes once ────────────────────────────────────────────

    def test_delivered_scan_closes_order_once(self):
        order = self._order()
        self._poll(carrier_tracking.STATUS_TRANSIT)
        self.assertEqual(self._poll(carrier_tracking.STATUS_DELIVERED), [(order.id, "delivered")])
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        self.assertFalse(order.grove_is_outstanding, "delivered is terminal: the order is closed")
        # A replayed delivered event (e.g. a late Shippo retry) is a no-op.
        with mock.patch.object(grove_main, "_notify_shipping_status") as notify:
            changed = grove_main._apply_delivery_status(self.env, order, "delivered", USPS_NUMBER)
        self.assertFalse(changed)
        notify.assert_not_called()

    def test_first_scan_already_delivered_walks_shipped_then_delivered(self):
        order = self._order()
        self.assertEqual(self._poll(carrier_tracking.STATUS_DELIVERED), [(order.id, "delivered")])
        self.assertEqual(order.grove_fulfillment_stage, "delivered")
        chatter = " ".join(order.message_ids.mapped("body"))
        self.assertIn("shipped", chatter)
        self.assertIn("delivered", chatter)

    # ── idempotency across sources and out-of-order events ──────────────

    def test_lagging_box_never_regresses_or_reemails(self):
        order = self._order()
        self._poll(carrier_tracking.STATUS_OUT_FOR_DELIVERY)
        self.assertEqual(order.grove_delivery_status, "out_for_delivery")
        # An older transit scan arrives afterwards: dropped, no second "shipped".
        self.assertEqual(self._poll(carrier_tracking.STATUS_TRANSIT), [])
        self.assertEqual(order.grove_delivery_status, "out_for_delivery")

    def test_failure_then_transit_does_not_resend_shipped(self):
        order = self._order()
        self._poll(carrier_tracking.STATUS_TRANSIT)
        with mock.patch.object(grove_main, "_notify_shipping_status") as notify:
            # Shippo can post a non-progress status (failure/returned) between scans.
            grove_main._apply_delivery_status(self.env, order, "failure", USPS_NUMBER)
            grove_main._apply_delivery_status(self.env, order, "transit", USPS_NUMBER)
        notify.assert_not_called()
        self.assertEqual(order.grove_shipment_notices_sent.splitlines(), [f"transit:{USPS_NUMBER}"])

    def test_operator_mark_shipped_then_poll_confirms_one_email(self):
        order = self._order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="not_applicable"),
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            grove_main._operator_mark_shipped(self.env, order, actor="1")
        notify.assert_called_once()
        self.assertEqual(self._poll(carrier_tracking.STATUS_TRANSIT), [])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    # ── mode gate (GOL-1982) ─────────────────────────────────────────────

    def test_pickup_order_gets_no_email_and_no_state_move(self):
        order = self._order(grove_fulfillment="pickup", grove_fulfillment_state="reserved")
        with mock.patch.object(grove_main, "_notify_shipping_status") as notify:
            changed = grove_main._apply_delivery_status(self.env, order, "transit", USPS_NUMBER)
            grove_main._apply_delivery_status(self.env, order, "delivered", USPS_NUMBER)
        self.assertFalse(changed)
        notify.assert_not_called()
        self.assertEqual(order.grove_fulfillment_stage, "reserved")
        self.assertEqual(order.grove_delivery_status, "label_purchased")

    def test_prewave_preorder_deposit_does_not_email_or_close(self):
        order = self._order(
            grove_checkout_status="deposit_paid",
            grove_fulfillment_state=False,
            grove_preorder_variant_ids=str(self.product.id),
        )
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        # Not in the poll domain at all...
        self.assertEqual(self._poll(carrier_tracking.STATUS_DELIVERED), [])
        # ...and a direct webhook event for a per-item label is refused too.
        with mock.patch.object(grove_main, "_notify_shipping_status") as notify:
            grove_main._apply_delivery_status(self.env, order, "delivered", USPS_NUMBER)
        notify.assert_not_called()
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        self.assertTrue(order.grove_is_outstanding)

    # ── persisted carrier + tracking in the real notice ─────────────────

    def test_shipped_notice_uses_persisted_carrier_tracking(self):
        order = self._order()
        client = _FakeClient(carrier_tracking.STATUS_TRANSIT)
        with (
            mock.patch.object(carrier_tracking, "build_clients", return_value={"UPS": None, "USPS": client}),
            mock.patch.object(grove_main, "_notify_discord"),
        ):
            self.env["sale.order"].sudo()._cron_poll_carrier_tracking()
        mails = self.env["mail.mail"].sudo().search([("email_to", "=", "poll@example.com")])
        self.assertEqual(len(mails), 1)
        self.assertIn("has shipped", mails.subject)
        self.assertIn("USPS", mails.body_html)
        self.assertIn(f"tools.usps.com/go/TrackConfirmAction?tLabels={USPS_NUMBER}", mails.body_html)
        self.assertEqual(client.calls, [USPS_NUMBER])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
