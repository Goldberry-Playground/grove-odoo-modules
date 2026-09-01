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

    def test_payload_uses_box_parcel(self):
        p = sp.build_shipment_payload(self.ADDR, "s20", 4, "leafed")
        self.assertEqual(p["parcels"][0]["length"], "20")
        self.assertEqual(p["parcels"][0]["width"], "8")
        self.assertEqual(p["parcels"][0]["height"], "8")
        # 1.6 tare + 4 x 2.0 leafed = 9.6 lb declared actual weight.
        self.assertEqual(p["parcels"][0]["weight"], "9.6")
        self.assertTrue(p["address_to"]["is_residential"])
        self.assertEqual(p["address_from"]["zip"], "26651")

    def test_payload_weight_never_below_one_pound(self):
        p = sp.build_shipment_payload(self.ADDR, "br16", 0, "dormant")
        self.assertGreaterEqual(float(p["parcels"][0]["weight"]), 1.0)

    @staticmethod
    def _posts(shipment, transaction):
        return mock.Mock(
            side_effect=[
                mock.Mock(status_code=201, json=lambda: shipment, raise_for_status=lambda: None),
                mock.Mock(status_code=201, json=lambda: transaction, raise_for_status=lambda: None),
            ]
        )

    TXN = {
        "status": "SUCCESS",
        "tracking_number": "1Z999",
        "label_url": "https://deliver.goshippo.com/x.pdf",
        "object_id": "t1",
    }

    def test_buy_label_happy_path(self):
        shipment = {
            "rates": [
                {"object_id": "r1", "provider": "UPS", "servicelevel": {"token": "ups_ground"}, "amount": "14.23"}
            ]
        }
        posts = self._posts(shipment, self.TXN)
        out = sp.buy_cheapest_ground_label("key", sp.build_shipment_payload(self.ADDR, "s20", 4, "leafed"), post=posts)
        self.assertEqual(out["tracking_number"], "1Z999")
        self.assertEqual(out["carrier"], "UPS")
        self.assertEqual(out["servicelevel"], "ups_ground")

    def test_buy_label_picks_cheapest_across_carriers(self):
        # USPS Ground Advantage is cheaper than UPS Ground here -> it must win,
        # and the winning carrier/service must be reported for persistence.
        shipment = {
            "rates": [
                {"object_id": "ru", "provider": "UPS", "servicelevel": {"token": "ups_ground"}, "amount": "29.70"},
                {
                    "object_id": "rp",
                    "provider": "USPS",
                    "servicelevel": {"token": "usps_ground_advantage"},
                    "amount": "10.08",
                },
            ]
        }
        posts = self._posts(shipment, self.TXN)
        out = sp.buy_cheapest_ground_label("key", sp.build_shipment_payload(self.ADDR, "s20", 4, "leafed"), post=posts)
        # The transaction call must reference the cheaper USPS rate object.
        bought = posts.call_args_list[1].kwargs["json"]["rate"]
        self.assertEqual(bought, "rp")
        self.assertEqual(out["carrier"], "USPS")
        self.assertEqual(out["servicelevel"], "usps_ground_advantage")

    def test_buy_label_single_carrier_failover(self):
        # Only USPS quotes (UPS outage). Failover: buy it rather than raise.
        shipment = {
            "rates": [
                {
                    "object_id": "rp",
                    "provider": "USPS",
                    "servicelevel": {"token": "usps_ground_advantage"},
                    "amount": "9.99",
                }
            ]
        }
        posts = self._posts(shipment, self.TXN)
        out = sp.buy_cheapest_ground_label("key", sp.build_shipment_payload(self.ADDR, "s20", 4, "leafed"), post=posts)
        self.assertEqual(out["carrier"], "USPS")

    def test_no_allowlisted_ground_rate_raises(self):
        # Only a non-allowlisted service present (UPS 3-Day Select). The
        # allowlist must reject it — never buy an off-list service.
        shipment = {
            "rates": [
                {"object_id": "r1", "provider": "UPS", "servicelevel": {"token": "ups_3_day_select"}, "amount": "9.99"}
            ]
        }
        posts = mock.Mock(return_value=mock.Mock(status_code=201, json=lambda: shipment, raise_for_status=lambda: None))
        with self.assertRaises(sp.ShippoError):
            sp.buy_cheapest_ground_label("key", sp.build_shipment_payload(self.ADDR, "s20", 4, "leafed"), post=posts)


class TestCheapestGroundSelector(unittest.TestCase):
    """select_cheapest_ground: allowlist + transit guard + least-cost."""

    def _r(self, provider, token, amount, days=None):
        r = {"provider": provider, "servicelevel": {"token": token}, "amount": str(amount)}
        if days is not None:
            r["estimated_days"] = days
        return r

    def test_none_when_no_allowlisted_rate(self):
        self.assertIsNone(sp.select_cheapest_ground([self._r("UPS", "ups_3_day_select", 5)]))

    def test_off_list_carrier_ignored_even_if_cheapest(self):
        # FedEx is not on the allowlist; the more expensive UPS Ground wins.
        rates = [self._r("FedEx", "fedex_ground", 1.00), self._r("UPS", "ups_ground", 20.00)]
        self.assertEqual(sp.select_cheapest_ground(rates)["provider"], "UPS")

    def test_cheapest_wins_within_transit_tolerance(self):
        rates = [self._r("UPS", "ups_ground", 29.70, days=2), self._r("USPS", "usps_ground_advantage", 10.08, days=3)]
        # tolerance 1 day: 3 <= fastest(2)+1 -> USPS allowed and cheaper.
        self.assertEqual(sp.select_cheapest_ground(rates)["provider"], "USPS")

    def test_transit_guard_excludes_too_slow_cheaper_rate(self):
        # USPS is cheaper but 3 days slower than fastest -> guard drops it.
        rates = [self._r("UPS", "ups_ground", 29.70, days=2), self._r("USPS", "usps_ground_advantage", 10.08, days=5)]
        self.assertEqual(sp.select_cheapest_ground(rates, tolerance_days=1)["provider"], "UPS")

    def test_missing_eta_not_excluded(self):
        # No estimated_days anywhere -> guard is a no-op, cheapest still wins.
        rates = [self._r("UPS", "ups_ground", 29.70), self._r("USPS", "usps_ground_advantage", 10.08)]
        self.assertEqual(sp.select_cheapest_ground(rates)["provider"], "USPS")


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


class TestShipFromOrigin(unittest.TestCase):
    """GOL-988 regression: ORIGIN.street1 used to be the literal placeholder
    "SET_AT_DEPLOY" while a comment claimed GROVE_SHIP_FROM_STREET overrode it —
    nothing read that var, so real labels carried a garbage return address."""

    def test_origin_street_is_a_real_address_not_a_placeholder(self):
        street = sp.ORIGIN["street1"]
        self.assertNotIn("SET_AT_DEPLOY", street)
        self.assertNotEqual(street.strip(), "")
        # A usable street line starts with a house number.
        self.assertRegex(street, r"^\d+\s+\S")

    def test_origin_city_state_zip_are_the_farm(self):
        self.assertEqual(sp.ORIGIN["city"], "Summersville")
        self.assertEqual(sp.ORIGIN["state"], "WV")
        self.assertEqual(sp.ORIGIN["zip"], "26651")
        self.assertEqual(sp.ORIGIN["country"], "US")

    def test_ship_from_street_env_override_is_actually_wired(self):
        """The documented override must work, not just be described in a comment."""
        with mock.patch.dict(os.environ, {"GROVE_SHIP_FROM_STREET": "1 Override Way"}):
            spec = importlib.util.spec_from_file_location("grove_shippo_reload", _MODULE_PATH)
            reloaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reloaded)
            self.assertEqual(reloaded.ORIGIN["street1"], "1 Override Way")

    def test_payload_ships_from_the_origin(self):
        payload = sp.build_shipment_payload(TestShippoClient.ADDR, "s20", 4, "leafed")
        self.assertEqual(payload["address_from"]["zip"], "26651")
        self.assertNotIn("SET_AT_DEPLOY", payload["address_from"]["street1"])
