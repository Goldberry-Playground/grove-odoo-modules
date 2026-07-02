"""TDD tests for shippo_client (pure Python, no Odoo DB required).

Module loaded by file path so relative imports in the source module don't
interfere — same pattern used by test_shipping_zones.py.
"""

import importlib.util
import os
import unittest
from unittest import mock

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shippo_client.py")
_spec = importlib.util.spec_from_file_location("grove_shippo_client", _MODULE_PATH)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


class TestShippoClient(unittest.TestCase):
    ADDR = {
        "name": "Jane Doe",
        "street1": "1 Elm St",
        "city": "Richwood",
        "state": "WV",
        "zip": "26261",
        "country": "US",
    }

    def test_payload_uses_tier_parcel(self):
        p = sp.build_shipment_payload(self.ADDR, "bareroot")
        self.assertEqual(p["parcels"][0]["length"], "48")
        self.assertTrue(p["address_to"]["is_residential"])
        self.assertEqual(p["address_from"]["zip"], "26651")

    def test_buy_label_happy_path(self):
        shipment = {
            "rates": [
                {
                    "object_id": "r1",
                    "provider": "UPS",
                    "servicelevel": {"token": "ups_ground"},
                    "amount": "14.23",
                }
            ]
        }
        transaction = {
            "status": "SUCCESS",
            "tracking_number": "1Z999",
            "label_url": "https://deliver.goshippo.com/x.pdf",
            "object_id": "t1",
        }
        posts = mock.Mock(
            side_effect=[
                mock.Mock(status_code=201, json=lambda: shipment, raise_for_status=lambda: None),
                mock.Mock(status_code=201, json=lambda: transaction, raise_for_status=lambda: None),
            ]
        )
        out = sp.buy_ups_ground_label("key", sp.build_shipment_payload(self.ADDR, "bareroot"), post=posts)
        self.assertEqual(out["tracking_number"], "1Z999")

    def test_no_ups_ground_rate_raises(self):
        shipment = {
            "rates": [
                {
                    "object_id": "r1",
                    "provider": "USPS",
                    "servicelevel": {"token": "usps_ground_advantage"},
                    "amount": "9.99",
                }
            ]
        }
        posts = mock.Mock(return_value=mock.Mock(status_code=201, json=lambda: shipment, raise_for_status=lambda: None))
        with self.assertRaises(sp.ShippoError):
            sp.buy_ups_ground_label("key", sp.build_shipment_payload(self.ADDR, "bareroot"), post=posts)


class TestTrackingValidation(unittest.TestCase):
    """is_valid_tracking: alphanumeric 6-40 chars; rejects LIKE wildcards and junk."""

    def test_valid_ups_tracking(self):
        self.assertTrue(sp.is_valid_tracking("1Z999AA10123456784"))

    def test_percent_wildcard_rejected(self):
        self.assertFalse(sp.is_valid_tracking("%"))

    def test_percent_in_tracking_rejected(self):
        self.assertFalse(sp.is_valid_tracking("1Z999%"))

    def test_empty_string_rejected(self):
        self.assertFalse(sp.is_valid_tracking(""))

    def test_none_rejected(self):
        self.assertFalse(sp.is_valid_tracking(None))

    def test_too_short_rejected(self):
        # "abc" is only 3 chars — below the 6-char minimum
        self.assertFalse(sp.is_valid_tracking("abc"))

    def test_41_char_string_rejected(self):
        # 41 alphanumeric chars — above the 40-char maximum
        self.assertFalse(sp.is_valid_tracking("A" * 41))

    def test_exactly_6_chars_valid(self):
        self.assertTrue(sp.is_valid_tracking("ABCDE1"))

    def test_exactly_40_chars_valid(self):
        self.assertTrue(sp.is_valid_tracking("A" * 40))

    def test_underscore_wildcard_rejected(self):
        self.assertFalse(sp.is_valid_tracking("1Z999_AA"))
