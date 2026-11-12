"""Tests for the consult travel estimator (spec C.7).

``models/consult_travel.py`` is pure Python (stdlib only), so these are plain
``unittest`` cases with no DB — collected by pytest and also run under Odoo's
``--test-enable`` runner. The module is loaded by file path so importing it never
drags in the Odoo addon package (same contract as ``test_shipping_zones.py``).
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "consult_travel.py")
_spec = importlib.util.spec_from_file_location("grove_consult_travel", _MODULE_PATH)
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class TestHaversine(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(ct.haversine_miles(38.0, -81.0, 38.0, -81.0), 0.0, places=6)

    def test_known_city_pair(self):
        # NYC (40.7128,-74.0060) to LA (34.0522,-118.2437) ≈ 2445 statute miles.
        miles = ct.haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertAlmostEqual(miles, 2445, delta=15)

    def test_symmetric(self):
        a = ct.haversine_miles(38.26, -80.99, 39.1, -77.5)
        b = ct.haversine_miles(39.1, -77.5, 38.26, -80.99)
        self.assertAlmostEqual(a, b, places=6)


class TestEstimate(unittest.TestCase):
    def test_close_trip_no_bill_no_overnight(self):
        # ~same place: well under one hour each way.
        out = ct.estimate((38.26, -80.99), (38.30, -81.05))
        self.assertLess(out["hours"], 1.0)
        self.assertEqual(out["billable_hours"], 0.0)
        self.assertFalse(out["overnight"])
        self.assertEqual(out["overnight_usd"], 0.0)

    def test_road_factor_and_round_trip(self):
        straight = ct.haversine_miles(38.0, -81.0, 39.0, -80.0)
        out = ct.estimate((38.0, -81.0), (39.0, -80.0))
        self.assertAlmostEqual(out["miles"], round(straight * ct.ROAD_FACTOR, 1), places=1)
        self.assertAlmostEqual(out["round_trip_miles"], round(straight * ct.ROAD_FACTOR * 2, 1), places=1)

    def test_billable_is_hours_minus_first(self):
        out = ct.estimate((40.7128, -74.0060), (34.0522, -118.2437))
        self.assertAlmostEqual(out["billable_hours"], round(out["hours"] - ct.FREE_HOURS, 2), places=2)

    def test_overnight_and_600_rule_far_trip(self):
        # Cross-country is well past the three-hour one-way threshold.
        out = ct.estimate((40.7128, -74.0060), (34.0522, -118.2437))
        self.assertTrue(out["overnight"])
        self.assertEqual(out["overnight_usd"], ct.OVERNIGHT_USD)

    def test_overnight_threshold_boundary(self):
        # Construct a destination whose one-way drive is just over three hours.
        # 3 h at 45 mph with a 1.3 road factor needs ~104 straight miles.
        far = ct.estimate((38.0, -81.0), (40.0, -81.0))  # ~138 straight miles north
        self.assertGreaterEqual(far["hours"], 3.0)
        self.assertTrue(far["overnight"])


class TestCentroidLookup(unittest.TestCase):
    def test_farm_zip_resolves(self):
        latlon = ct.centroid_for_zip("26651")  # Summersville WV, the farm ZIP
        self.assertIsNotNone(latlon)
        self.assertAlmostEqual(latlon[0], 38.27, delta=0.3)
        self.assertAlmostEqual(latlon[1], -80.99, delta=0.3)

    def test_zip_plus_four_accepted(self):
        self.assertIsNotNone(ct.centroid_for_zip("26651-1234"))

    def test_unknown_and_malformed_return_none(self):
        self.assertIsNone(ct.centroid_for_zip("00000"))
        self.assertIsNone(ct.centroid_for_zip("abcde"))
        self.assertIsNone(ct.centroid_for_zip(""))
        self.assertIsNone(ct.centroid_for_zip(None))
        self.assertIsNone(ct.centroid_for_zip(26651))  # not a string

    def test_estimate_for_zips_roundtrip(self):
        out = ct.estimate_for_zips("26651", "26651")
        self.assertIsNotNone(out)
        self.assertEqual(out["zip"], "26651")
        self.assertEqual(out["farm_zip"], "26651")
        self.assertLess(out["miles"], 1.0)

    def test_estimate_for_zips_unknown_is_none(self):
        self.assertIsNone(ct.estimate_for_zips("26651", "00000"))


if __name__ == "__main__":
    unittest.main()
