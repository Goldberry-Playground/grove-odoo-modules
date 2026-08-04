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
from datetime import date, timedelta

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


class TestAnnualCalendar(unittest.TestCase):
    """Rev-2 annual per-USDA-zone shipping calendar + three-mode resolver (GOL-1172)."""

    ZONE = 6  # any configured zone; defaults are identical across 2-10

    def _mode(self, today, calendar=None):
        return sc.resolve_fulfillment(self.ZONE, today, calendar)

    def test_spring_in_window(self):
        r = self._mode(date(2027, 3, 1))
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-01-01", "2027-05-05"])

    def test_fall_in_window(self):
        r = self._mode(date(2026, 10, 1))
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-09-15", "2026-10-30"])

    def test_fall_preorder(self):
        # Aug 15 -> Sep 14: reserve now, ships the upcoming fall wave.
        r = self._mode(date(2026, 8, 20))
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-09-15", "2026-10-30"])
        self.assertIn("Reserve now", r["ship_timing"])

    def test_spring_preorder_spans_year_end(self):
        # Nov 1 (year Y) preorder ships the spring wave of year Y+1.
        r = self._mode(date(2026, 12, 1))
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-01-01", "2027-05-05"])

    def test_leafed_summer_is_peat_and_bagged(self):
        r = self._mode(date(2026, 7, 1))
        self.assertEqual(r["mode"], sc.MODE_PEAT)
        self.assertEqual(r["fulfillment_days"], [5, 10])
        self.assertIn("5–10 business days", r["ship_timing"])

    def test_shipped_past_zone_falls_back_to_ships_now_not_preorder(self):
        # Oct 31: the fall wave (ends Oct 30) has shipped and spring preorder
        # (opens Nov 1) has NOT — the order must ship now on the 5-10 day policy,
        # NOT be held as a spring preorder. (Josh, rev-2: dormant AND leafed.)
        r = self._mode(date(2026, 10, 31))
        self.assertEqual(r["mode"], sc.MODE_PEAT)
        self.assertEqual(r["season"], None)

    def test_narrowed_zone_window_makes_earlier_date_preorder(self):
        # Warm-zone stagger: an admin narrows zone 6's fall wave to Sep 20 start,
        # so Sep 17 flips from in-window to preorder — no code change.
        cal = sc.merge_calendar_override({"zones": {"6": {"fall": [[9, 20], [10, 5]]}}})
        r = self._mode(date(2026, 9, 17), cal)
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["ship_window"], ["2026-09-20", "2026-10-05"])

    def test_unknown_and_unconfigured_zone_return_no_mode(self):
        self.assertIsNone(sc.resolve_fulfillment(None, date(2026, 7, 1))["mode"])
        self.assertIsNone(sc.resolve_fulfillment(99, date(2026, 7, 1))["mode"])

    def test_every_calendar_date_resolves_to_exactly_one_mode(self):
        # Coverage/exhaustiveness: every day of a full year maps to a mode.
        cal = sc.default_calendar()
        valid = {sc.MODE_PREORDER, sc.MODE_IN_WINDOW, sc.MODE_PEAT}
        d = date(2026, 1, 1)
        while d <= date(2026, 12, 31):
            self.assertIn(self._mode(d, cal)["mode"], valid, d.isoformat())
            d += timedelta(days=1)


class TestCalendarSerialization(unittest.TestCase):
    def test_default_calendar_shape(self):
        cal = sc.default_calendar()
        self.assertEqual(set(cal["zones"]), set(range(2, 11)))
        self.assertEqual(cal["preorder_open"]["fall"], (8, 15))

    def test_serialize_is_json_safe(self):
        out = sc.serialize_calendar()
        self.assertEqual(set(out["zones"]), {str(z) for z in range(2, 11)})
        self.assertEqual(out["preorder_open"], {"fall": [8, 15], "spring": [11, 1]})
        self.assertEqual(out["zones"]["6"]["spring"], [[1, 1], [5, 5]])

    def test_merge_ignores_garbage_override(self):
        self.assertEqual(sc.merge_calendar_override("not-a-dict"), sc.default_calendar())
        self.assertEqual(sc.merge_calendar_override(None), sc.default_calendar())

    def test_merge_partial_override_keeps_defaults(self):
        merged = sc.merge_calendar_override({"fulfillment_days": [3, 7]})
        self.assertEqual(merged["fulfillment_days"], (3, 7))
        self.assertEqual(merged["zones"][6]["fall"], ((9, 15), (10, 30)))  # untouched


if __name__ == "__main__":
    unittest.main()
