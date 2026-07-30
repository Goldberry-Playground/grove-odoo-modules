"""Pure tests for the live shipping-rate feed builder (GOL-952).

``rate_feed()`` is what the ``/grove/api/v1/shipping/rates`` controller serves.
Like the engine it reads from, it is pure Python (no Odoo, no DB), so these are
plain unittest cases loaded by file path — the same pattern as
``test_shipping_zones.py``. The contract: the feed exposes exactly the in-memory
table checkout prices with, plus the authoritative green-list zone map, in the
shape the frontend ``resolveRateTable`` drops in unchanged.
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_zones.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_zones_feed", _MODULE_PATH)
sz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sz)


class RateFeedTests(unittest.TestCase):
    def setUp(self):
        self.feed = sz.rate_feed()

    def test_top_level_shape(self):
        self.assertEqual(set(self.feed), {"zones", "zone_by_state", "green_states"})

    def test_zones_mirror_engine_table(self):
        # The feed must serve exactly what compute_shipping_rate prices with.
        self.assertEqual(self.feed["zones"], sz.ZONE_RATES)

    def test_zone_shape_matches_rates_json(self):
        # Same structure as data/shipping_rates.json: zone -> tier -> {"base": <num>}.
        for zone, tiers in self.feed["zones"].items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for tier, rule in tiers.items():
                self.assertIn(tier, sz.TIERS)
                self.assertIsInstance(rule.get("base"), (int, float))

    def test_zone_by_state_is_authoritative_green_list(self):
        # The zone->state map IS the compliance gate; it must equal the engine's
        # map and cover the 21 green states exactly.
        self.assertEqual(self.feed["zone_by_state"], sz.ZONE_BY_STATE)
        self.assertEqual(set(self.feed["zone_by_state"]), set(sz.GREEN_STATES))
        self.assertEqual(self.feed["green_states"], sorted(sz.GREEN_STATES))

    def test_every_green_state_prices(self):
        # Eligibility parity: every state the feed lists as green must resolve to
        # a zone that has a priced rule, so the frontend never offers a state the
        # backend can't price.
        for state, zone in self.feed["zone_by_state"].items():
            self.assertIn(zone, self.feed["zones"])
            self.assertIsNotNone(sz.compute_shipping_rate(state, tier="potted"))

    def test_returned_dict_is_decoupled_copy(self):
        # Mutating the feed must never corrupt the engine's live tables.
        self.feed["zones"]["zone_1"]["potted"]["base"] = 9999
        self.feed["zone_by_state"]["WV"] = "zone_5"
        fresh = sz.rate_feed()
        self.assertNotEqual(fresh["zones"]["zone_1"]["potted"]["base"], 9999)
        self.assertEqual(fresh["zone_by_state"]["WV"], "zone_1")


if __name__ == "__main__":
    unittest.main()
