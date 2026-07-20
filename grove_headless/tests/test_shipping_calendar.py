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


class TestMatrixCaching(unittest.TestCase):
    """Regression: a transient CSV read failure must NOT be memoized (GOL-653).

    The old `@lru_cache` pinned any worker that first read the matrix mid-deploy
    (empty/unreadable file) to `{}` forever → "unknown zip" for every ZIP on
    that worker. The loader must only cache a successful, non-empty load.
    """

    def setUp(self):
        self._real_path = sc._MATRIX_PATH
        self._real_cache = sc._MATRIX_CACHE

    def tearDown(self):
        sc._MATRIX_PATH = self._real_path
        sc._MATRIX_CACHE = self._real_cache

    def test_failed_load_is_not_cached_and_recovers(self):
        # Simulate the file being unreadable (mid git-sync swap).
        sc._MATRIX_CACHE = None
        sc._MATRIX_PATH = os.path.join(os.path.dirname(__file__), "does-not-exist.csv")
        self.assertEqual(sc._zip_matrix(), {})
        self.assertIsNone(sc._MATRIX_CACHE, "empty result must not be memoized")

        # File readable again → the very next call recovers, no restart needed.
        sc._MATRIX_PATH = self._real_path
        matrix = sc._zip_matrix()
        self.assertTrue(matrix, "matrix should load once the CSV is readable again")
        self.assertEqual(matrix.get("26651"), 6)
        self.assertIs(sc._MATRIX_CACHE, matrix, "successful load must be cached")


class TestShipOptionsSerializer(unittest.TestCase):
    def test_dates_serialize_iso(self):
        r = sc.ship_options("26651", "bareroot", date(2026, 7, 15))
        out = sc.serialize_ship_options(r)
        self.assertEqual(out["next_wave"]["ship_start"], "2026-11-09")
        self.assertIsNone(out["defer_to"])
        self.assertTrue(out["ships_now"])


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


class TestFreezeBoundary(unittest.TestCase):
    """Freeze-window boundary conditions for zone-6 ZIP (26651 — Summersville WV)."""

    Z6_ZIP = "26651"

    def test_freeze_start_inclusive_dec15(self):
        # zone 6 freeze starts Dec 15 (inclusive) — should not ship on that date
        r = sc.ship_options(self.Z6_ZIP, "potted", date(2026, 12, 15))
        self.assertFalse(r["ships_now"], "Dec 15 is freeze start and must block shipping")

    def test_freeze_end_exclusive_mar1_defer_to_mar2(self):
        # Mar 1 is still inside the freeze window; defer_to must be Mar 2
        r = sc.ship_options(self.Z6_ZIP, "potted", date(2027, 3, 1))
        self.assertFalse(r["ships_now"], "Mar 1 is still inside the freeze window")
        self.assertEqual(r["defer_to"], date(2027, 3, 2))


if __name__ == "__main__":
    unittest.main()
