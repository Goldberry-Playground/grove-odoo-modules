"""Tests for ``preorder_waves()``: fall + spring pre-order availability per zone.

Pure Python, no DB. The module is loaded by file path so importing it never
drags in the Odoo addon package.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_calendar.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_calendar_waves", _MODULE_PATH)
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)


def waves(zone, m, d):
    return {w["wave"]: w for w in cal.preorder_waves(zone, date(2026, m, d))}


class TestPreorderWaves(unittest.TestCase):
    def test_both_waves_open_from_sep_1(self):
        w = waves(8, 9, 1)
        self.assertTrue(w["fall"]["open"] and w["spring"]["open"])
        self.assertEqual(w["fall"]["order_by"], [11, 21])
        self.assertEqual(w["spring"]["order_by"], [4, 16])

    def test_closed_before_sep_1_in_summer(self):
        w = waves(6, 8, 31)
        self.assertFalse(w["fall"]["open"])
        self.assertEqual(w["fall"]["reason"], "opens_sep_1")
        self.assertFalse(w["spring"]["open"])
        self.assertEqual(w["spring"]["reason"], "opens_sep_1")

    def test_fall_greys_after_zone_order_by_inclusive(self):
        self.assertTrue(waves(8, 11, 21)["fall"]["open"])
        w = waves(8, 11, 22)
        self.assertFalse(w["fall"]["open"])
        self.assertEqual(w["fall"]["reason"], "deadline_passed")
        self.assertTrue(w["spring"]["open"])

    def test_spring_open_through_new_year_until_its_order_by(self):
        self.assertTrue(waves(8, 1, 10)["spring"]["open"])
        self.assertTrue(waves(8, 4, 16)["spring"]["open"])
        w = waves(8, 4, 17)
        self.assertFalse(w["spring"]["open"])
        self.assertEqual(w["spring"]["reason"], "deadline_passed")

    def test_fall_closed_in_spring_months(self):
        w = waves(6, 3, 1)
        self.assertFalse(w["fall"]["open"])
        self.assertEqual(w["fall"]["reason"], "opens_sep_1")

    def test_order_and_shape(self):
        out = cal.preorder_waves(6, date(2026, 9, 15))
        self.assertEqual([w["wave"] for w in out], ["fall", "spring"])
        self.assertEqual(set(out[0]), {"wave", "ship_window", "order_by", "open", "reason"})
        self.assertIsNone(out[0]["reason"])


if __name__ == "__main__":
    unittest.main()
