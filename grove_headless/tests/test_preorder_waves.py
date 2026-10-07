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
        self.assertEqual(w["spring"]["order_by"], [2, 22])

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
        self.assertTrue(waves(8, 2, 22)["spring"]["open"])
        w = waves(8, 2, 23)
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

    def test_spring_windows_end_inside_dormancy_and_order_by_precedes_start(self):
        # Josh 2026-10-07: every spring wave ships inside the Nov 1 -> Apr 15
        # dormancy window; order_by is 7 days before the spring ship start.
        for zone, sched in cal.WAVE_SCHEDULE.items():
            sp = sched["spring"]
            self.assertLessEqual(sp["ship_end"], (4, 15), zone)
            self.assertLess(sp["ship_start"], sp["ship_end"], zone)
            self.assertLess(sp["order_by"], sp["ship_start"], zone)
            start = date(2027, *sp["ship_start"])
            self.assertEqual((start - date(2027, *sp["order_by"])).days, 7, zone)
            z = cal.default_calendar()["zones"][zone]
            self.assertEqual(z["spring"][1], sp["ship_end"], zone)


if __name__ == "__main__":
    unittest.main()
