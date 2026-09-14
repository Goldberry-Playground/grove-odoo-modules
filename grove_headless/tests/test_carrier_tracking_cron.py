"""Carrier-tracking poll cron (GOL-2272, Pirate Ship C).

The 2-hourly cron ``sale.order._cron_poll_carrier_tracking`` sweeps in-flight
orders, asks the injected UPS/USPS clients for each box's status, and folds the
result into the UNCHANGED ``_apply_delivery_status`` email path. What must hold:

  * a delivered scan applies the status exactly once (then the order drops out
    of the poll domain — no second email);
  * terminal (delivered) and aged (30-day cap) orders are skipped, and the cap
    posts one ops note then stops polling that order;
  * a client error never raises out of the cron;
  * three consecutive auth failures for a carrier post one ops alert and pause
    that carrier for the rest of the run;
  * a multi-box order takes the least-advanced box status;
  * an exception status posts one silent ops note and sends no customer email.

Needs a DB (sale.order), so it is listed in tests/__init__.py AND excluded from
pytest in conftest.py (the double-skip guard, GOL-1936).
"""

from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import carrier_tracking
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


class _FakeClient:
    """Stands in for a UpsTrackClient/UspsTrackClient: ``fn(tracking)`` returns a
    mapped status or raises. Records every tracking number it was asked about."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    def track(self, tracking_number):
        self.calls.append(tracking_number)
        return self._fn(tracking_number)


@tagged("post_install", "-at_install")
class TestCarrierTrackingCron(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Track Customer", "email": "track@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "American Plum", "type": "consu", "is_storable": True, "list_price": 22.0}
        )

    def _order(self, carriers="UPS", tracking="1Z999AA10123456784", **vals):
        # Mirrors a real order just after label purchase: the fulfilment
        # WATERMARK is set to label_purchased (grove_headless sale_order line
        # ~488), so the queryable stage stays "label_purchased" even after the
        # poll overwrites grove_delivery_status with transit/delivered — the
        # order does not fall out of the poll domain mid-journey.
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
            "grove_fulfillment": "ship",
            "grove_checkout_status": "paid",
            "grove_fulfillment_state": "label_purchased",
            "grove_delivery_status": "label_purchased",
            "grove_tracking_numbers": tracking,
            "grove_shipping_carriers": carriers,
            "grove_label_purchased_at": fields.Datetime.now(),
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _run(self, clients, order=None):
        """Run the cron with a fixed client map and a silenced Discord, capturing
        the branded-email calls (the once-only guard lives in that path)."""
        model = (order or self.env["sale.order"]).sudo()
        with (
            mock.patch.object(carrier_tracking, "build_clients", return_value=clients),
            mock.patch.object(grove_main, "_notify_discord") as discord,
            mock.patch.object(grove_main, "_notify_shipping_status") as notify,
        ):
            model._cron_poll_carrier_tracking()
        return discord, notify

    # ── happy path: delivered applies once ───────────────────────────────

    def test_delivered_applies_status_and_emails_once(self):
        order = self._order()
        ups = _FakeClient(lambda t: carrier_tracking.STATUS_DELIVERED)
        _discord, notify = self._run({"UPS": ups, "USPS": None})
        self.assertEqual(order.grove_delivery_status, "delivered")
        notify.assert_called_once()
        # A second run cannot re-email: the delivered order is out of the domain.
        _discord2, notify2 = self._run({"UPS": ups, "USPS": None})
        notify2.assert_not_called()
        self.assertEqual(len(ups.calls), 1, "a delivered order is not polled again")

    def test_transit_then_delivered_two_emails(self):
        order = self._order()
        transit = _FakeClient(lambda t: carrier_tracking.STATUS_TRANSIT)
        self._run({"UPS": transit, "USPS": None})
        self.assertEqual(order.grove_delivery_status, "transit")
        delivered = _FakeClient(lambda t: carrier_tracking.STATUS_DELIVERED)
        _d, notify = self._run({"UPS": delivered, "USPS": None})
        self.assertEqual(order.grove_delivery_status, "delivered")
        notify.assert_called_once()

    # ── skips ────────────────────────────────────────────────────────────

    def test_already_delivered_is_not_polled(self):
        self._order(grove_delivery_status="delivered")
        ups = _FakeClient(lambda t: carrier_tracking.STATUS_TRANSIT)
        self._run({"UPS": ups, "USPS": None})
        self.assertEqual(ups.calls, [], "a delivered order must never be polled")

    def test_aged_order_is_capped_with_one_note(self):
        order = self._order(grove_label_purchased_at=fields.Datetime.now() - timedelta(days=31))
        ups = _FakeClient(lambda t: carrier_tracking.STATUS_TRANSIT)
        discord, _n = self._run({"UPS": ups, "USPS": None})
        self.assertTrue(order.grove_carrier_poll_stopped)
        self.assertEqual(ups.calls, [], "a capped order is not polled")
        self.assertEqual(discord.call_count, 1)
        self.assertIn("Tracking poll stopped", discord.call_args[0][0])
        # Capped orders drop out of the domain on the next run entirely.
        discord2, _n2 = self._run({"UPS": ups, "USPS": None})
        discord2.assert_not_called()

    # ── resilience ───────────────────────────────────────────────────────

    def test_never_raises_on_client_error(self):
        order = self._order()

        def boom(_t):
            raise RuntimeError("carrier down")

        ups = _FakeClient(boom)
        # Must not raise.
        self._run({"UPS": ups, "USPS": None})
        self.assertEqual(order.grove_delivery_status, "label_purchased", "no status change on a failed call")

    def test_three_consecutive_auth_failures_pause_carrier_once(self):
        # Four UPS orders; every track raises an auth error. The carrier pauses
        # after the third failure, so the 4th order is never polled and exactly
        # one "paused" ops alert is posted.
        for i in range(4):
            self._order(tracking=f"1Z999AA1012345678{i}")

        def auth_fail(_t):
            raise carrier_tracking.CarrierAuthError("bad token")

        ups = _FakeClient(auth_fail)
        discord, _n = self._run({"UPS": ups, "USPS": None})
        self.assertEqual(len(ups.calls), 3, "carrier is paused after 3 failures, 4th order skipped")
        paused = [c for c in discord.call_args_list if "paused" in c[0][0]]
        self.assertEqual(len(paused), 1)

    # ── multi-box + exceptions ───────────────────────────────────────────

    def test_multibox_takes_least_advanced(self):
        order = self._order(carriers="UPS\nUSPS", tracking="1Z999AA10123456784\n9400111899223")

        def ups_delivered(_t):
            return carrier_tracking.STATUS_DELIVERED

        def usps_transit(_t):
            return carrier_tracking.STATUS_TRANSIT

        self._run({"UPS": _FakeClient(ups_delivered), "USPS": _FakeClient(usps_transit)})
        self.assertEqual(order.grove_delivery_status, "transit", "order is not delivered until every box is")

    def test_exception_posts_one_ops_note_no_email(self):
        order = self._order()
        ups = _FakeClient(lambda t: carrier_tracking.STATUS_FAILURE)
        discord, notify = self._run({"UPS": ups, "USPS": None})
        self.assertTrue(order.grove_carrier_exception_noted)
        self.assertEqual(order.grove_delivery_status, "label_purchased", "failure is silent, no status advance")
        notify.assert_not_called()
        exc_notes = [c for c in discord.call_args_list if "exception" in c[0][0].lower()]
        self.assertEqual(len(exc_notes), 1)
        # Re-running does not post a second exception note.
        discord2, _n = self._run({"UPS": ups, "USPS": None})
        exc2 = [c for c in discord2.call_args_list if "exception" in c[0][0].lower()]
        self.assertEqual(len(exc2), 0)

    def test_no_credentials_is_a_noop(self):
        order = self._order()
        self._run({"UPS": None, "USPS": None})
        self.assertEqual(order.grove_delivery_status, "label_purchased")
