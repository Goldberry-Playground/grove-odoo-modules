"""Tests for the 12-zone shipping rate engine (GOL-15).

The engine in ``models/shipping_zones.py`` is pure Python, so these are plain
``unittest`` cases with no DB — they run both under Odoo's ``--test-enable``
runner and standalone (``python3 -m pytest`` / direct execution). The module is
loaded by file path so importing it never drags in the Odoo addon package.

Two layers:
  * Contract tests — assert the engine's fail-safe behaviour. These pass NOW,
    while the rate table is still blocked/empty, and guard it from regressing.
  * Table-coverage tests — assert the finished table is complete and self
    consistent. They no-op while the table is empty and automatically start
    enforcing full coverage the moment Josh's data is filled in.
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_zones.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_zones", _MODULE_PATH)
sz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sz)


class TestShippingZoneEngineContract(unittest.TestCase):
    """Fail-safe behaviour that must hold regardless of the data state."""

    def test_unmapped_state_returns_none(self):
        # None (not 0.0) => "no shipping configured, add no line".
        self.assertIsNone(sz.compute_shipping_rate("ZZ"))

    def test_empty_or_missing_state_returns_none(self):
        self.assertIsNone(sz.compute_shipping_rate(""))
        self.assertIsNone(sz.compute_shipping_rate(None))

    def test_there_are_exactly_five_rate_zones(self):
        self.assertEqual(len(sz.RATE_ZONE_IDS), 5)

    def test_rate_is_tier_scoped(self):
        rates = {"zone_1": {"bareroot": {"base": 21.0}, "potted": {"base": 32.0}}}
        with _temp_table({"WV": "zone_1"}, rates):
            self.assertEqual(sz.compute_shipping_rate("WV", tier="bareroot"), 21.0)
            self.assertEqual(sz.compute_shipping_rate("WV", tier="potted"), 32.0)

    def test_unknown_tier_prices_as_potted(self):
        rates = {"zone_1": {"bareroot": {"base": 21.0}, "potted": {"base": 32.0}}}
        with _temp_table({"WV": "zone_1"}, rates):
            self.assertEqual(sz.compute_shipping_rate("WV", tier="mystery"), 32.0)

    def test_missing_tier_rule_returns_none(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"bareroot": {"base": 21.0}}}):
            self.assertIsNone(sz.compute_shipping_rate("WV", tier="potted"))

    def test_rates_load_from_json_file(self):
        # The shipped data file parses and, if non-empty, only contains known
        # zone ids and tiers with numeric non-negative "base".
        for zone, tiers in sz.ZONE_RATES.items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for tier, rule in tiers.items():
                self.assertIn(tier, sz.TIERS)
                self.assertGreaterEqual(float(rule["base"]), 0.0)

    def test_state_lookup_is_case_and_space_insensitive(self):
        sz.ZONE_BY_STATE["WV"] = "zone_1"
        try:
            self.assertEqual(sz.zone_for_state(" wv "), "zone_1")
        finally:
            sz.ZONE_BY_STATE.pop("WV", None)

    def test_flat_base_rate(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"potted": {"base": 8.0}}}):
            self.assertEqual(sz.compute_shipping_rate("WV", tier="potted"), 8.0)

    def test_per_pound_surcharge(self):
        with _temp_table({"CA": "zone_5"}, {"zone_5": {"potted": {"base": 10.0, "per_lb": 0.5}}}):
            # 10 base + 0.5 * 6 lbs = 13.00
            self.assertEqual(sz.compute_shipping_rate("CA", tier="potted", weight=6.0), 13.0)

    def test_free_over_threshold(self):
        rule = {"zone_1": {"potted": {"base": 8.0, "free_over": 75.0}}}
        with _temp_table({"WV": "zone_1"}, rule):
            self.assertEqual(sz.compute_shipping_rate("WV", tier="potted", subtotal=80.0), 0.0)
            self.assertEqual(sz.compute_shipping_rate("WV", tier="potted", subtotal=20.0), 8.0)


class TestShippingZoneTableCoverage(unittest.TestCase):
    """Enforced automatically once the blocked table is populated."""

    def test_every_mapped_zone_has_a_rate(self):
        for state, zone in sz.ZONE_BY_STATE.items():
            self.assertIn(zone, sz.ZONE_RATES, f"state {state} maps to {zone} with no rate rule")
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"{zone} is not one of the 5 zone ids")

    def test_full_state_coverage_when_configured(self):
        if not sz.is_configured():
            self.skipTest("12-zone rate table not yet populated (GOL-15 blocked)")
        missing = [s for s in sz.US_STATES if s not in sz.ZONE_BY_STATE]
        self.assertFalse(missing, f"states with no zone assignment: {missing}")

    def test_every_rate_rule_targets_a_real_zone(self):
        for zone in sz.ZONE_RATES:
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"rate rule for unknown zone {zone}")


class _temp_table:
    """Context manager: temporarily install a zone table for one assertion."""

    def __init__(self, by_state, rates):
        self._by_state, self._rates = by_state, rates

    def __enter__(self):
        import copy

        self._saved_state = copy.deepcopy(dict(sz.ZONE_BY_STATE))
        self._saved_rates = copy.deepcopy(dict(sz.ZONE_RATES))
        sz.ZONE_BY_STATE.clear()
        sz.ZONE_BY_STATE.update(self._by_state)
        sz.ZONE_RATES.clear()
        sz.ZONE_RATES.update(self._rates)
        return self

    def __exit__(self, *exc):
        sz.ZONE_BY_STATE.clear()
        sz.ZONE_BY_STATE.update(self._saved_state)
        sz.ZONE_RATES.clear()
        sz.ZONE_RATES.update(self._saved_rates)
        return False


if __name__ == "__main__":
    unittest.main()
