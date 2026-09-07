"""Pure tests for the branded shipment-notification copy + tracking-URL helper
(GOL-1979). ``shipment_email`` is stdlib-only, so it loads by file path like the
other pure tests (preorder_email, shipping_calendar) and runs under the pytest
lane without an Odoo DB.
"""

import importlib.util
import os
import sys
import unittest

_MODELS = os.path.join(os.path.dirname(__file__), "..", "models")


def _load(modname):
    spec = importlib.util.spec_from_file_location(
        f"grove_headless.models.{modname}", os.path.join(_MODELS, f"{modname}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


se = _load("shipment_email")


class TrackingUrlTests(unittest.TestCase):
    def test_ups_deep_link(self):
        self.assertEqual(
            se.tracking_url("UPS", "1Z999AA10123456784"),
            "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784",
        )

    def test_usps_deep_link(self):
        self.assertEqual(
            se.tracking_url("USPS", "9400111899223817200001"),
            "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223817200001",
        )

    def test_carrier_match_is_case_insensitive(self):
        self.assertEqual(se.tracking_url("ups", "1Z9"), se.tracking_url("UPS", "1Z9"))

    def test_long_form_provider_names_alias(self):
        self.assertIn("ups.com", se.tracking_url("United Parcel Service", "1Z9"))
        self.assertIn("usps.com", se.tracking_url("United States Postal Service", "94001"))

    def test_number_is_url_encoded(self):
        # A stray space must not break the query string.
        self.assertEqual(
            se.tracking_url("UPS", "1Z 99"),
            "https://www.ups.com/track?loc=en_US&tracknum=1Z%2099",
        )

    def test_unknown_carrier_yields_no_link(self):
        self.assertIsNone(se.tracking_url("FedEx", "123"))
        self.assertIsNone(se.tracking_url("", "123"))

    def test_blank_tracking_yields_no_link(self):
        self.assertIsNone(se.tracking_url("UPS", ""))
        self.assertIsNone(se.tracking_url("UPS", "   "))
        self.assertIsNone(se.tracking_url("UPS", None))


class CarrierLabelTests(unittest.TestCase):
    def test_known_carriers(self):
        self.assertEqual(se.carrier_label("ups"), "UPS")
        self.assertEqual(se.carrier_label("usps"), "USPS")

    def test_unknown_falls_back_generic(self):
        self.assertEqual(se.carrier_label(""), "Carrier")
        self.assertEqual(se.carrier_label(None), "Carrier")


class ShipmentNoticeCopyTests(unittest.TestCase):
    def test_shipped_renders_carrier_and_working_links_per_box(self):
        subject, body = se.shipment_notice_copy(
            status="transit",
            order_name="S00042",
            customer_name="Dana",
            shipments=[("UPS", "1Z999AA10123456784"), ("USPS", "9400111899223817200001")],
        )
        self.assertEqual(subject, "Your order S00042 has shipped")
        self.assertIn("Hi Dana,", body)
        # UPS box: carrier label + clickable deep link.
        self.assertIn("UPS:", body)
        self.assertIn('href="https://www.ups.com/track?loc=en_US&amp;tracknum=1Z999AA10123456784"', body)
        # USPS box: carrier label + clickable deep link.
        self.assertIn("USPS:", body)
        self.assertIn("https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223817200001", body)

    def test_delivered_subject_and_lead(self):
        subject, body = se.shipment_notice_copy(
            status="delivered",
            order_name="S00042",
            customer_name="Dana",
            shipments=[("UPS", "1Z9")],
        )
        self.assertEqual(subject, "Your order S00042 has been delivered")
        self.assertIn("has been delivered", body)

    def test_unknown_carrier_shows_plain_number_no_link(self):
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="Dana",
            shipments=[("FedEx", "ABC123")],
        )
        self.assertIn("ABC123", body)
        self.assertNotIn("href", body)

    def test_balance_line_included_when_provided(self):
        note = "This was a preorder. You paid a $10 deposit per tree at checkout."
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="Dana",
            shipments=[("UPS", "1Z9")],
            balance_line=note,
        )
        self.assertIn(note, body)

    def test_no_balance_line_when_absent(self):
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="Dana",
            shipments=[("UPS", "1Z9")],
        )
        self.assertNotIn("preorder", body)
        self.assertNotIn("deposit", body)

    def test_missing_customer_name_falls_back(self):
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="",
            shipments=[("UPS", "1Z9")],
        )
        self.assertIn("Hi there,", body)

    def test_customer_name_is_html_escaped(self):
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="<script>",
            shipments=[("UPS", "1Z9")],
        )
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_blank_shipments_render_no_tracking_block(self):
        _, body = se.shipment_notice_copy(
            status="transit",
            order_name="S1",
            customer_name="Dana",
            shipments=[("UPS", "  ")],
        )
        self.assertNotIn("Tracking:", body)

    def test_house_voice_no_em_dashes(self):
        for status in ("transit", "delivered"):
            _, body = se.shipment_notice_copy(
                status=status,
                order_name="S1",
                customer_name="Dana",
                shipments=[("UPS", "1Z9")],
                balance_line="Balance settles on your saved card.",
            )
            self.assertNotIn("—", body)  # em dash
            self.assertNotIn("–", body)  # en dash


if __name__ == "__main__":
    unittest.main()
