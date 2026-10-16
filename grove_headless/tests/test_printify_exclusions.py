"""Odoo-runtime tests: a paid Printify order is kept out of the in-house
fulfillment paths — the Pirate Ship label batch and the UPS/USPS carrier poll
(spec Review Focus 2, GOL-3314)."""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import carrier_tracking
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyExclusions(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.action_confirm()
        self.order.write({"grove_checkout_status": "paid"})

    def test_not_in_label_batch(self):
        batch = self.env["grove.label.batch"].create({"name": "LB-PRINTIFY-TEST", "company_id": self.company.id})
        self.assertNotIn(self.order, batch._eligible_orders(self.company))

    def test_not_polled_by_carrier_cron(self):
        self.order.write({"grove_tracking_numbers": "9400111", "grove_shipping_carriers": "USPS"})
        clients = {"UPS": mock.Mock(), "USPS": mock.Mock()}
        with mock.patch.object(carrier_tracking, "build_clients", return_value=clients):
            self.env["sale.order"]._poll_carrier_tracking()
        clients["USPS"].track.assert_not_called()
