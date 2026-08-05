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
        self.assertEqual(set(self.feed), {"schema", "zones", "zone_by_state", "green_states", "packing", "calendar"})
        self.assertEqual(self.feed["schema"], 2)

    def test_zones_mirror_engine_table(self):
        # The feed must serve exactly what compute_shipping_rate prices with.
        self.assertEqual(self.feed["zones"], sz.ZONE_RATES)

    def test_zone_shape_matches_rates_json(self):
        # Same structure as data/shipping_rates.json: zone -> box -> {"base": <num>}.
        for zone, boxes in self.feed["zones"].items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for box_id, rule in boxes.items():
                self.assertIn(box_id, sz.shipping_boxes.BOXES)
                self.assertIsInstance(rule.get("base"), (int, float))

    def test_packing_metadata_mirrors_box_catalog(self):
        # The frontend must be able to replay pack_order exactly: dims +
        # capacities per box, the length classes, modes, and dormancy window.
        packing = self.feed["packing"]
        self.assertEqual(set(packing["boxes"]), set(sz.shipping_boxes.BOXES))
        for box_id, box in packing["boxes"].items():
            src = sz.shipping_boxes.BOXES[box_id]
            self.assertEqual(box["length"], src["length"])
            self.assertEqual(box["capacity"], src["capacity"])
        self.assertEqual(packing["length_classes"], list(sz.shipping_boxes.LENGTH_CLASSES))
        self.assertEqual(packing["modes"], list(sz.shipping_boxes.MODES))
        # dormant_window was replaced by the top-level per-zone calendar (GOL-1172).
        self.assertNotIn("dormant_window", packing)

    def test_calendar_block_is_per_usda_zone(self):
        # GOL-1172/1177: the feed carries the annual, per-USDA-zone shipping
        # calendar (real Arbor Day windows) the frontend resolves
        # (date, usdaZone) -> shippable mode against, plus the weather advisory.
        cal = self.feed["calendar"]
        self.assertEqual(
            set(cal),
            {"preorder_open", "leafed_window", "fulfillment_days", "approximate", "weather_hold_note", "zones"},
        )
        # Global preorder-open switch dates: fall Aug 15, spring Nov 1.
        self.assertEqual(cal["preorder_open"], {"fall": [8, 15], "spring": [11, 1]})
        self.assertEqual(cal["fulfillment_days"], [5, 10])
        # Advisory (GOL-1177): approximate by default, no active weather hold.
        self.assertTrue(cal["approximate"])
        self.assertIsNone(cal["weather_hold_note"])
        # Keyed to USDA hardiness zones 2-10 (strings), each with fall + spring
        # windows and their (nullable) order deadlines.
        self.assertEqual(set(cal["zones"]), {str(z) for z in range(2, 11)})
        for zw in cal["zones"].values():
            self.assertEqual(set(zw), {"fall", "spring", "fall_order_deadline", "spring_order_deadline"})
            self.assertEqual(len(zw["fall"]), 2)
        # Real chart values for a sampled cold + warm zone.
        self.assertEqual(cal["zones"]["2"]["fall"], [[11, 2], [11, 13]])
        self.assertEqual(cal["zones"]["8"]["spring"], [[3, 1], [4, 30]])
        self.assertEqual(cal["zones"]["8"]["spring_order_deadline"], [4, 16])

    def test_calendar_override_flows_through_feed(self):
        # An admin override (the system parameter) narrows a single zone's fall
        # window + adds a weather hold, without a code change; rest keep defaults.
        override = {"zones": {"6": {"fall": [[9, 20], [10, 5]]}}, "weather_hold_note": "Frost hold"}
        feed = sz.rate_feed(override)
        self.assertEqual(feed["calendar"]["zones"]["6"]["fall"], [[9, 20], [10, 5]])
        self.assertEqual(feed["calendar"]["zones"]["7"]["fall"], [[11, 9], [11, 26]])  # real default
        self.assertEqual(feed["calendar"]["weather_hold_note"], "Frost hold")

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
            self.assertIsNotNone(sz.single_tree_rate(state, 20, "leafed"))

    def test_returned_dict_is_decoupled_copy(self):
        # Mutating the feed must never corrupt the engine's live tables.
        self.feed["zones"]["zone_1"]["s20"]["base"] = 9999
        self.feed["zone_by_state"]["WV"] = "zone_5"
        fresh = sz.rate_feed()
        self.assertNotEqual(fresh["zones"]["zone_1"]["s20"]["base"], 9999)
        self.assertEqual(fresh["zone_by_state"]["WV"], "zone_1")


if __name__ == "__main__":
    unittest.main()
