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
    """Rev-2 annual per-USDA-zone shipping calendar + three-mode resolver.

    Anchored to the REAL Arbor Day windows (GOL-1177). Zone 6: fall ships
    Nov 9–26 (order by Nov 21), spring ships Apr 5–Jun 6 (order by May 31).
    """

    ZONE = 6

    def _mode(self, today, calendar=None):
        return sc.resolve_fulfillment(self.ZONE, today, calendar)

    def test_spring_in_window(self):
        r = self._mode(date(2027, 5, 1))  # inside zone-6 spring window Apr 5–Jun 6
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-04-05", "2027-06-06"])
        self.assertEqual(r["order_deadline"], "2027-05-31")

    def test_fall_in_window(self):
        r = self._mode(date(2026, 11, 15))  # inside zone-6 fall window Nov 9–26
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-11-09", "2026-11-26"])
        self.assertEqual(r["order_deadline"], "2026-11-21")

    def test_fall_preorder(self):
        # Aug 15 -> Nov 8: reserve now, ships the upcoming fall wave.
        r = self._mode(date(2026, 8, 20))
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-11-09", "2026-11-26"])
        self.assertEqual(r["order_deadline"], "2026-11-21")
        self.assertIn("Reserve now", r["ship_timing"])

    def test_spring_preorder_spans_year_end(self):
        # Dec 1 (year Y) preorder ships the spring wave of year Y+1.
        r = self._mode(date(2026, 12, 1))
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-04-05", "2027-06-06"])
        self.assertEqual(r["order_deadline"], "2027-05-31")

    def test_leafed_summer_is_peat_and_bagged(self):
        r = self._mode(date(2026, 7, 1))
        self.assertEqual(r["mode"], sc.MODE_PEAT)
        self.assertEqual(r["fulfillment_days"], [5, 10])
        self.assertIsNone(r["order_deadline"])
        self.assertIn("5–10 business days", r["ship_timing"])

    def test_shipped_past_zone_falls_back_to_ships_now_not_preorder(self):
        # Warm zone 8: spring wave ends Apr 30; on May 1 it has shipped and the
        # next fall preorder (opens Aug 15) has NOT — the order ships now on the
        # 5-10 day policy, NOT held as a preorder. (Josh, rev-2: dormant AND
        # leafed alike.) Reconciled against the real deadlines (GOL-1177).
        r = sc.resolve_fulfillment(8, date(2027, 5, 1))
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
        # Real Arbor Day zone-6 spring window + its order deadline.
        self.assertEqual(out["zones"]["6"]["spring"], [[4, 5], [6, 6]])
        self.assertEqual(out["zones"]["6"]["spring_order_deadline"], [5, 31])

    def test_merge_ignores_garbage_override(self):
        self.assertEqual(sc.merge_calendar_override("not-a-dict"), sc.default_calendar())
        self.assertEqual(sc.merge_calendar_override(None), sc.default_calendar())

    def test_merge_partial_override_keeps_defaults(self):
        merged = sc.merge_calendar_override({"fulfillment_days": [3, 7]})
        self.assertEqual(merged["fulfillment_days"], (3, 7))
        self.assertEqual(merged["zones"][6]["fall"], ((11, 9), (11, 26)))  # untouched (real)


class TestRealArborDayWindows(unittest.TestCase):
    """GOL-1177: real per-zone Arbor Day windows + weather-hold advisory."""

    # (zone, season, expected ship_window, expected order_deadline) sampled at a
    # COLD (2) and WARM (8) zone in BOTH seasons — the acceptance sample.
    def test_cold_zone2_fall_in_window(self):
        r = sc.resolve_fulfillment(2, date(2026, 11, 5))  # zone 2 fall Nov 2–13
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-11-02", "2026-11-13"])
        self.assertEqual(r["order_deadline"], "2026-11-12")

    def test_cold_zone2_spring_in_window(self):
        r = sc.resolve_fulfillment(2, date(2027, 5, 1))  # zone 2 spring Apr 19–Jun 6
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-04-19", "2027-06-06"])
        self.assertEqual(r["order_deadline"], "2027-05-31")

    def test_warm_zone8_fall_in_window(self):
        r = sc.resolve_fulfillment(8, date(2026, 12, 1))  # zone 8 fall Nov 9–Dec 12
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "fall")
        self.assertEqual(r["ship_window"], ["2026-11-09", "2026-12-12"])
        self.assertEqual(r["order_deadline"], "2026-11-21")

    def test_warm_zone8_spring_in_window(self):
        r = sc.resolve_fulfillment(8, date(2027, 3, 15))  # zone 8 spring Mar 1–Apr 30
        self.assertEqual(r["mode"], sc.MODE_IN_WINDOW)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-03-01", "2027-04-30"])
        self.assertEqual(r["order_deadline"], "2027-04-16")

    def test_warm_zone8_spring_preorder_from_fall(self):
        # Dec 13 (zone 8): fall wave ended Dec 12 -> rolls straight into the
        # spring preorder (opens Nov 1), ships the real spring window. Confirms
        # preorder-open still reconciles with the later real spring dates.
        r = sc.resolve_fulfillment(8, date(2026, 12, 13))
        self.assertEqual(r["mode"], sc.MODE_PREORDER)
        self.assertEqual(r["season"], "spring")
        self.assertEqual(r["ship_window"], ["2027-03-01", "2027-04-30"])

    def test_no_dead_months_every_zone_every_day(self):
        # Exhaustive: every USDA zone, every calendar day resolves to exactly one
        # mode against the real windows — no gap, no None for a configured zone.
        cal = sc.default_calendar()
        valid = {sc.MODE_PREORDER, sc.MODE_IN_WINDOW, sc.MODE_PEAT}
        for zone in range(2, 11):
            d = date(2026, 1, 1)
            while d <= date(2026, 12, 31):
                mode = sc.resolve_fulfillment(zone, d, cal)["mode"]
                self.assertIn(mode, valid, f"zone {zone} {d.isoformat()}")
                d += timedelta(days=1)

    def test_approximate_flag_defaults_true(self):
        self.assertTrue(sc.resolve_fulfillment(6, date(2026, 7, 1))["approximate"])
        self.assertTrue(sc.serialize_calendar()["approximate"])

    def test_weather_hold_note_defaults_null_and_round_trips(self):
        # Absent by default...
        self.assertIsNone(sc.resolve_fulfillment(6, date(2026, 7, 1))["weather_hold_note"])
        self.assertIsNone(sc.serialize_calendar()["weather_hold_note"])
        # ...and an admin advisory round-trips through resolver + feed serialize.
        note = "Frost hold: NE zones delayed ~1 week"
        cal = sc.merge_calendar_override({"weather_hold_note": note, "approximate": False})
        self.assertEqual(cal["weather_hold_note"], note)
        self.assertFalse(cal["approximate"])
        r = sc.resolve_fulfillment(6, date(2026, 11, 15), cal)
        self.assertEqual(r["weather_hold_note"], note)
        self.assertFalse(r["approximate"])
        out = sc.serialize_calendar(cal)
        self.assertEqual(out["weather_hold_note"], note)
        self.assertFalse(out["approximate"])

    def test_override_accepts_joshs_nested_ship_deadline_shape(self):
        # The exact shape Josh proposed (GOL-1114 comment e8b071f9): a zone's
        # season as {"ship": [[m,d],[m,d]], "order_deadline": [m,d]}.
        override = {"zones": {"6": {"fall": {"ship": [[10, 20], [11, 5]], "order_deadline": [10, 30]}}}}
        cal = sc.merge_calendar_override(override)
        self.assertEqual(cal["zones"][6]["fall"], ((10, 20), (11, 5)))
        self.assertEqual(cal["zones"][6]["fall_order_deadline"], (10, 30))
        # Spring untouched -> keeps the real default deadline.
        self.assertEqual(cal["zones"][6]["spring_order_deadline"], (5, 31))
        out = sc.serialize_calendar(cal)
        self.assertEqual(out["zones"]["6"]["fall"], [[10, 20], [11, 5]])
        self.assertEqual(out["zones"]["6"]["fall_order_deadline"], [10, 30])

    def test_bad_override_never_breaks_defaults(self):
        # A malformed season value is ignored, not fatal (storefront must never
        # 500 on a bad system parameter).
        cal = sc.merge_calendar_override({"zones": {"6": {"fall": "garbage"}}})
        self.assertEqual(cal["zones"][6]["fall"], ((11, 9), (11, 26)))  # real default kept


class TestOverrideFailOpen(unittest.TestCase):
    """Valid-JSON-wrong-shape overrides must fail open, not 500 (GOL-1311).

    Every case here raised an uncaught ValueError/TypeError before the fix,
    which propagated out of the /shipping/options and /shipping/rates GET
    handlers as a 500. The contract: log the bad value, keep the built-in
    calendar for that field, and still apply the rest of the override.
    """

    def _default(self):
        return sc.default_calendar()

    def test_non_numeric_zone_key_is_ignored(self):
        # "6a" is not int-coercible -> int("6a") used to raise. Ignore that zone,
        # keep every real default, still return a full calendar.
        cal = sc.merge_calendar_override({"zones": {"6a": {"fall": [[9, 20], [10, 5]]}}})
        self.assertEqual(cal, self._default())

    def test_bad_zone_key_does_not_drop_a_good_one(self):
        # A poisoned key must not stop a valid sibling zone from applying.
        cal = sc.merge_calendar_override(
            {"zones": {"6a": {"fall": [[9, 20], [10, 5]]}, "6": {"fall": [[9, 20], [10, 5]]}}}
        )
        self.assertEqual(cal["zones"][6]["fall"], ((9, 20), (10, 5)))  # good zone applied

    def test_malformed_leafed_window_keeps_default(self):
        cal = sc.merge_calendar_override({"leafed_window": "not-a-pair"})
        self.assertEqual(cal["leafed_window"], self._default()["leafed_window"])

    def test_malformed_fulfillment_days_keeps_default(self):
        cal = sc.merge_calendar_override({"fulfillment_days": ["a", "b"]})
        self.assertEqual(cal["fulfillment_days"], self._default()["fulfillment_days"])

    def test_malformed_preorder_open_keeps_default(self):
        cal = sc.merge_calendar_override({"preorder_open": {"fall": "xx"}})
        self.assertEqual(cal["preorder_open"], self._default()["preorder_open"])

    def test_malformed_order_deadline_keeps_zone_default(self):
        # dict-form ship/deadline shape with a bad deadline -> _md used to raise.
        # Season-level fail-open: the whole fall season falls back to its built-in
        # window+deadline (no partial-apply), and crucially never 500s.
        cal = sc.merge_calendar_override(
            {"zones": {"6": {"fall": {"ship": [[10, 20], [11, 5]], "order_deadline": "zz"}}}}
        )
        default6 = self._default()["zones"][6]
        self.assertEqual(cal["zones"][6]["fall"], default6["fall"])
        self.assertEqual(cal["zones"][6]["fall_order_deadline"], default6["fall_order_deadline"])

    def test_one_bad_field_does_not_sink_the_others(self):
        cal = sc.merge_calendar_override(
            {"leafed_window": "garbage", "fulfillment_days": [3, 7]}
        )
        self.assertEqual(cal["leafed_window"], self._default()["leafed_window"])  # kept
        self.assertEqual(cal["fulfillment_days"], (3, 7))  # applied


if __name__ == "__main__":
    unittest.main()
