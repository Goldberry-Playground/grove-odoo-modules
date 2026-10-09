"""Tests for the seed pre-order season resolver (GOL-3257, Train #3).

``models/seed_season.py`` is pure Python, so these are plain ``unittest`` cases
with no DB — they run under Odoo's ``--test-enable`` runner and standalone. The
module is loaded by file path so importing it never drags in the Odoo addon
package (same pattern as ``test_shipping_zones``).
"""

import datetime
import importlib.util
import os
import types
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "seed_season.py")
_spec = importlib.util.spec_from_file_location("grove_seed_season", _MODULE_PATH)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

D = datetime.date

# Chinquapin launch season (spec Launch catalog): ship Oct 15 to Nov 15 2026,
# order by Nov 1 2026, cap 10 lb.
CHINQUAPIN = dict(
    grove_seed_open=True,
    grove_seed_ship_start=D(2026, 10, 15),
    grove_seed_ship_end=D(2026, 11, 15),
    grove_seed_order_by=D(2026, 11, 1),
    grove_seed_cap_lb=10.0,
    grove_seed_reserved_lb=0.0,
)


def tmpl(**overrides):
    data = dict(CHINQUAPIN)
    data.update(overrides)
    return types.SimpleNamespace(**data)


class SeedSeasonCurrent(unittest.TestCase):
    def test_before_order_by_is_current_season(self):
        r = ss.seed_season(tmpl(), today=D(2026, 10, 20))
        self.assertEqual(r["year"], 2026)
        self.assertFalse(r["rolled_over"])
        self.assertIsNone(r["reason"])
        self.assertTrue(r["open"])
        self.assertEqual(r["ship_start"], D(2026, 10, 15))
        self.assertEqual(r["ship_end"], D(2026, 11, 15))
        self.assertEqual(r["order_by"], D(2026, 11, 1))

    def test_on_order_by_date_is_still_current(self):
        # Boundary: today == order_by must still reserve this season.
        r = ss.seed_season(tmpl(), today=D(2026, 11, 1))
        self.assertFalse(r["rolled_over"])
        self.assertEqual(r["year"], 2026)

    def test_adding_lb_within_cap_stays_current(self):
        r = ss.seed_season(tmpl(grove_seed_reserved_lb=9.0), today=D(2026, 10, 20), adding_lb=1.0)
        self.assertFalse(r["rolled_over"])


class SeedSeasonRollover(unittest.TestCase):
    def test_after_order_by_rolls_to_next_fall(self):
        r = ss.seed_season(tmpl(), today=D(2026, 11, 2))
        self.assertTrue(r["rolled_over"])
        self.assertEqual(r["reason"], ss.REASON_ORDER_BY_PASSED)
        self.assertEqual(r["year"], 2027)
        self.assertEqual(r["ship_start"], D(2027, 10, 15))
        self.assertEqual(r["ship_end"], D(2027, 11, 15))
        self.assertEqual(r["order_by"], D(2027, 11, 1))

    def test_at_cap_exactly_still_current_on_zero_add_read(self):
        # Spec §3 formula: reserved + adding <= cap -> current. A catalog read
        # (adding=0) of a product whose reserved weight exactly equals the cap
        # is still "open"; any real pack add (adding>0) then rolls it over.
        r = ss.seed_season(tmpl(grove_seed_reserved_lb=10.0), today=D(2026, 10, 20))
        self.assertFalse(r["rolled_over"])

    def test_over_cap_rolls_to_next_fall(self):
        r = ss.seed_season(tmpl(grove_seed_reserved_lb=10.5), today=D(2026, 10, 20))
        self.assertTrue(r["rolled_over"])
        self.assertEqual(r["reason"], ss.REASON_CAP_REACHED)
        self.assertEqual(r["year"], 2027)

    def test_adding_lb_crossing_cap_rolls_over(self):
        r = ss.seed_season(tmpl(grove_seed_reserved_lb=9.5), today=D(2026, 10, 20), adding_lb=1.0)
        self.assertTrue(r["rolled_over"])
        self.assertEqual(r["reason"], ss.REASON_CAP_REACHED)

    def test_cap_freed_by_cancel_returns_to_current(self):
        # A cancelled/refunded line drops out of reserved_lb, so the weight
        # returns to the cap and the season is purchasable again.
        full = ss.seed_season(tmpl(grove_seed_reserved_lb=10.5), today=D(2026, 10, 20))
        self.assertTrue(full["rolled_over"])
        freed = ss.seed_season(tmpl(grove_seed_reserved_lb=7.5), today=D(2026, 10, 20))
        self.assertFalse(freed["rolled_over"])

    def test_order_by_dominates_cap_when_both(self):
        # Past order-by AND over cap: the date-based reason wins.
        r = ss.seed_season(tmpl(grove_seed_reserved_lb=10.0), today=D(2026, 11, 5))
        self.assertTrue(r["rolled_over"])
        self.assertEqual(r["reason"], ss.REASON_ORDER_BY_PASSED)


class SeedSeasonSwitchesAndEdits(unittest.TestCase):
    def test_closed_switch_is_not_purchasable_but_shows_window(self):
        r = ss.seed_season(tmpl(grove_seed_open=False), today=D(2026, 10, 20))
        self.assertFalse(r["open"])
        self.assertFalse(r["rolled_over"])
        self.assertEqual(r["ship_start"], D(2026, 10, 15))

    def test_unconfigured_template_yields_none_dates(self):
        r = ss.seed_season(
            tmpl(grove_seed_ship_start=None, grove_seed_ship_end=None, grove_seed_order_by=None),
            today=D(2026, 10, 20),
        )
        self.assertIsNone(r["ship_start"])
        self.assertIsNone(r["order_by"])
        self.assertFalse(r["rolled_over"])
        self.assertEqual(r["year"], 2026)

    def test_date_edit_moves_current_season_no_cron(self):
        # Josh rolls the season forward by editing the dates to 2027; a 2027
        # shopper sees the 2027 season as current with no code/cron change.
        next_year = tmpl(
            grove_seed_ship_start=D(2027, 10, 15),
            grove_seed_ship_end=D(2027, 11, 15),
            grove_seed_order_by=D(2027, 11, 1),
        )
        r = ss.seed_season(next_year, today=D(2027, 10, 20))
        self.assertEqual(r["year"], 2027)
        self.assertFalse(r["rolled_over"])


if __name__ == "__main__":
    unittest.main()
