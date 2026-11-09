"""Tests for the pure order-lane and hold-date helpers.

``models/order_lanes.py`` is stdlib-only, so these are plain ``unittest``
cases with no DB; the module is loaded by file path.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "order_lanes.py")
_spec = importlib.util.spec_from_file_location("grove_order_lanes", _MODULE_PATH)
ol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ol)


class TestOrderLanes(unittest.TestCase):
    def test_lane(self):
        self.assertEqual(ol.lane_for("ship", False, "spring"), "spring")
        self.assertEqual(ol.lane_for("ship", False, "fall"), "individual")
        self.assertEqual(ol.lane_for("ship", False, False), "individual")
        self.assertEqual(ol.lane_for("pickup", False, "spring"), "none")
        self.assertEqual(ol.lane_for("ship", True, False), "none")

    def test_parse_hold_date(self):
        today = date(2026, 10, 20)
        self.assertEqual(ol.parse_hold_date("Oct 25", today), date(2026, 10, 25))
        self.assertEqual(ol.parse_hold_date("10/25", today), date(2026, 10, 25))
        self.assertEqual(ol.parse_hold_date("Oct 15", today), date(2027, 10, 15))
        self.assertEqual(ol.parse_hold_date("2027-03-01", today), date(2027, 3, 1))
        for bad in ("Feb 30", "soon", "", "2026-10-01"):
            with self.assertRaises(ValueError):
                ol.parse_hold_date(bad, today)

    def test_parse_hold_date_today_is_not_past(self):
        today = date(2026, 10, 20)
        self.assertEqual(ol.parse_hold_date("Oct 20", today), today)
        self.assertEqual(ol.parse_hold_date("2026-10-20", today), today)

    def test_suggest_is_later_of_dormancy_and_zone(self):
        self.assertEqual(
            ol.suggest_hold_date(date(2026, 9, 1), date(2026, 10, 15), date(2026, 11, 9)),
            date(2026, 11, 9),
        )
        self.assertEqual(ol.suggest_hold_date(date(2026, 9, 1), date(2026, 10, 15), None), date(2026, 10, 15))

    def test_suggest_never_before_today(self):
        self.assertEqual(
            ol.suggest_hold_date(date(2026, 12, 1), date(2026, 10, 15), date(2026, 11, 9)),
            date(2026, 12, 1),
        )

    def test_parse_tracking_accepts_ups_and_usps_shapes(self):
        self.assertEqual(ol.parse_tracking(" 1z999 aa1 0123456784\n"), "1Z999AA10123456784")
        for digits in (20, 22, 34):
            number = "9" * digits
            self.assertEqual(ol.parse_tracking(number), number)

    def test_parse_tracking_refuses_other_shapes(self):
        for bad in (
            "1Z999AA1012345678",  # 17, one short
            "1Z999AA101234567845",  # 19, one over
            "1Z999AA1012345678*",  # right length, not alphanumeric
            "9" * 19,
            "9" * 35,
            "94001000000000000A00",  # 20 characters, not all digits
            "٩" * 22,  # digits outside ASCII
        ):
            with self.assertRaises(ValueError, msg=bad) as caught:
                ol.parse_tracking(bad)
            self.assertIn(bad, str(caught.exception))

    def test_parse_tracking_asks_for_a_missing_number(self):
        for empty in ("", "   ", None):
            with self.assertRaises(ValueError) as caught:
                ol.parse_tracking(empty)
            self.assertIn("Enter the tracking number", str(caught.exception))

    def test_parse_label_cost_caps_at_250(self):
        self.assertEqual(ol.parse_label_cost("250.00"), 250.0)
        for too_big in ("250.01", "1240", "$1,240.00"):
            with self.assertRaises(ValueError) as caught:
                ol.parse_label_cost(too_big)
            self.assertIn("decimal", str(caught.exception))

    def test_clean_tracking_and_cost(self):
        self.assertEqual(ol.clean_tracking(" 1Z999 AA1 0123456784\n"), "1Z999AA10123456784")
        self.assertEqual(ol.parse_label_cost("$12.40"), 12.40)
        for bad in ("", "abc", "-1", "0"):
            with self.assertRaises(ValueError):
                ol.parse_label_cost(bad)
