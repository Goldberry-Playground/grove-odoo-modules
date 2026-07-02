"""Tests for the ZIP→USDA-zone lookup (Task 5, GOL-xx).

The lookup in ``models/shipping_calendar.py`` is pure Python, so these are
plain ``unittest`` cases with no DB.  They run both under Odoo's
``--test-enable`` runner and standalone (``python3 -m pytest`` / direct
execution).  The module is loaded by file path so importing it never drags
in the Odoo addon package.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_calendar.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_calendar", _MODULE_PATH)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


class TestZipZoneMatrix(unittest.TestCase):
    def test_known_wv_zip(self):
        self.assertEqual(sc.usda_zone_for_zip("26651"), 6)  # Summersville WV

    def test_zone_is_int_in_range(self):
        z = sc.usda_zone_for_zip("04101")  # Portland ME
        self.assertIsInstance(z, int)
        self.assertTrue(2 <= z <= 10)

    def test_unknown_or_malformed_zip_returns_none(self):
        self.assertIsNone(sc.usda_zone_for_zip("00000"))
        self.assertIsNone(sc.usda_zone_for_zip("abcde"))
        self.assertIsNone(sc.usda_zone_for_zip(""))
        self.assertIsNone(sc.usda_zone_for_zip(None))
        self.assertIsNone(sc.usda_zone_for_zip("123456"))

    def test_same_state_spans_multiple_zones(self):
        # The whole reason this matrix exists: WV alone spans 5a-7a.
        zones = {sc.usda_zone_for_zip(z) for z in ("26651", "25302", "26505")}
        zones.discard(None)
        self.assertGreater(len(zones), 1, "expected >1 USDA zone within WV sample ZIPs")

    def test_zip9_normalizes_to_zip5(self):
        self.assertEqual(sc.usda_zone_for_zip("26651-1234"), sc.usda_zone_for_zip("26651"))


class TestShipOptions(unittest.TestCase):
    Z5_ZIP = "04101"  # Portland ME — USDA 5-ish; assert actual zone in test
    Z6_ZIP = "26651"  # Summersville WV — USDA 6

    def test_july_potted_ships_now(self):
        r = sc.ship_options(self.Z6_ZIP, "potted", date(2026, 7, 15))
        self.assertTrue(r["ships_now"])
        self.assertIsNone(r["defer_to"])

    def test_january_anything_defers(self):
        for tier in ("potted", "bareroot"):
            r = sc.ship_options(self.Z6_ZIP, tier, date(2027, 1, 10))
            self.assertFalse(r["ships_now"], tier)
            self.assertIsNotNone(r["defer_to"], tier)

    def test_potted_defer_lands_after_freeze_window(self):
        r = sc.ship_options(self.Z6_ZIP, "potted", date(2027, 1, 10))
        # zone 6 freeze runs Dec 15 - Mar 1 => first safe day is Mar 2, 2027
        self.assertEqual(r["defer_to"], date(2027, 3, 2))

    def test_bareroot_next_wave_after_fall_deadline_is_spring(self):
        # Dec 1 2026, zone 6: fall order_by (Nov 21) passed -> spring wave 2027
        r = sc.ship_options(self.Z6_ZIP, "bareroot", date(2026, 12, 1))
        self.assertEqual(r["next_wave"]["season"], "spring")
        self.assertEqual(r["next_wave"]["ship_start"], date(2027, 4, 5))
        self.assertEqual(r["next_wave"]["order_by"], date(2027, 5, 31))

    def test_bareroot_in_summer_ships_now_and_shows_fall_wave(self):
        r = sc.ship_options(self.Z6_ZIP, "bareroot", date(2026, 7, 15))
        self.assertTrue(r["ships_now"])
        self.assertEqual(r["next_wave"]["season"], "fall")
        self.assertEqual(r["next_wave"]["ship_start"], date(2026, 11, 9))

    def test_unknown_zip_is_conservative(self):
        r = sc.ship_options("00000", "potted", date(2026, 7, 15))
        self.assertIsNone(r["usda_zone"])
        self.assertFalse(r["ships_now"])

    def test_every_usda_zone_has_waves_and_freeze(self):
        for z in range(2, 11):
            self.assertIn(z, sc.WAVE_SCHEDULE)
            self.assertIn(z, sc.FREEZE_WINDOWS)
            for season in ("fall", "spring"):
                w = sc.WAVE_SCHEDULE[z][season]
                self.assertLessEqual(w["order_by"], w["ship_end"])


if __name__ == "__main__":
    unittest.main()
