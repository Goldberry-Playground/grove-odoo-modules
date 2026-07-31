"""Tests for the per-box 5-zone shipping rate engine (Box Engine v2).

The engine in ``models/shipping_zones.py`` is pure Python, so these are plain
``unittest`` cases with no DB — they run both under Odoo's ``--test-enable``
runner and standalone (``python3 -m pytest`` / direct execution). The module is
loaded by file path so importing it never drags in the Odoo addon package.

Two layers:
  * Contract tests — assert the engine's fail-safe behaviour. These pass at all
    times and guard against regression on the core routing logic.
  * Table-coverage tests — assert the finished table is complete and self-
    consistent. They automatically enforce full coverage across all 21 green
    states, 5 zones, and every catalog box.
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_zones.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_zones", _MODULE_PATH)
sz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sz)

sb = sz.shipping_boxes

# Independent pin of the 21 green states (deliberately NOT sz.GREEN_STATES:
# the test must catch an accidental edit to the module's set, so it keeps
# its own copy of the compliance list).
GREEN = frozenset(
    {
        "CT",
        "DE",
        "IL",
        "IN",
        "KY",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "NH",
        "NJ",
        "NY",
        "NC",
        "OH",
        "PA",
        "RI",
        "VT",
        "VA",
        "WV",
        "WI",
    }
)

# A complete single-zone box rate table for contract tests (mirrors the
# provisional zone_1 card).
BOX_RATES_Z1 = {
    "br16": {"base": 18.0},
    "s20": {"base": 22.0},
    "s32": {"base": 24.0},
    "s46": {"base": 26.0},
    "b20": {"base": 28.0},
    "b32": {"base": 30.0},
}


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


class TestShippingZoneEngineContract(unittest.TestCase):
    """Fail-safe behaviour that must hold regardless of the data state."""

    def test_unmapped_state_returns_none(self):
        # None (not 0.0) => "no shipping configured, add no line".
        self.assertIsNone(sz.box_rate("ZZ", "s20"))
        self.assertIsNone(sz.compute_order_shipping("ZZ", [("bareroot", 20, 1)], "leafed"))

    def test_empty_or_missing_state_returns_none(self):
        self.assertIsNone(sz.box_rate("", "s20"))
        self.assertIsNone(sz.box_rate(None, "s20"))

    def test_there_are_exactly_five_rate_zones(self):
        self.assertEqual(len(sz.RATE_ZONE_IDS), 5)

    def test_rate_is_box_scoped(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertEqual(sz.box_rate("WV", "s20"), 22.0)
            self.assertEqual(sz.box_rate("WV", "b32"), 30.0)

    def test_missing_box_rule_returns_none(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"s20": {"base": 22.0}}}):
            self.assertIsNone(sz.box_rate("WV", "b32"))

    def test_rates_load_from_json_file(self):
        # The shipped data file parses and, if non-empty, only contains known
        # zone ids and catalog box ids with numeric non-negative "base".
        for zone, boxes in sz.ZONE_RATES.items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for box_id, rule in boxes.items():
                self.assertIn(box_id, sb.BOXES)
                self.assertGreaterEqual(float(rule["base"]), 0.0)

    def test_state_lookup_is_case_and_space_insensitive(self):
        import copy

        saved_state = copy.deepcopy(dict(sz.ZONE_BY_STATE))
        try:
            sz.ZONE_BY_STATE["WV"] = "zone_1"
            self.assertEqual(sz.zone_for_state(" wv "), "zone_1")
        finally:
            sz.ZONE_BY_STATE.clear()
            sz.ZONE_BY_STATE.update(saved_state)

    def test_potted_is_never_shippable(self):
        # Potted = farm pickup only: reason for the checkout BLOCK message,
        # None from the pricing path — even with a fully populated table.
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertIsNotNone(sz.unshippable_reason([("potted", 20, 1)]))
            self.assertIsNone(sz.compute_order_shipping("WV", [("potted", 20, 1)], "leafed"))

    def test_unknown_tier_treated_as_potted(self):
        # A mistagged product can never ship undercharged — it cannot ship.
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertIsNotNone(sz.unshippable_reason([("mystery", 20, 1)]))
            self.assertIsNone(sz.compute_order_shipping("WV", [("mystery", 20, 1)], "leafed"))

    def test_bareroot_has_no_unshippable_reason(self):
        self.assertIsNone(sz.unshippable_reason([("bareroot", 20, 3)]))

    def test_zero_qty_potted_line_is_ignored(self):
        self.assertIsNone(sz.unshippable_reason([("potted", 20, 0), ("bareroot", 20, 1)]))


class TestTwentyOneStateCoverage(unittest.TestCase):
    """The 21-state green list and its rate coverage."""

    def test_exactly_the_21_green_states_are_mapped(self):
        self.assertEqual(set(sz.ZONE_BY_STATE), GREEN)

    def test_every_mapped_state_prices_every_catalog_box(self):
        for state in GREEN:
            for box_id in sb.BOXES:
                rate = sz.box_rate(state, box_id)
                self.assertIsNotNone(rate, f"{state}/{box_id} has no rate")
                self.assertGreater(rate, 0.0)

    def test_every_excluded_destination_returns_none(self):
        for code in sz.US_STATES:
            if code in GREEN:
                continue
            for box_id in sb.BOXES:
                self.assertIsNone(sz.box_rate(code, box_id), code)

    def test_heavier_box_never_cheaper_within_a_zone(self):
        # Rates monotone in representative billable weight keep the packer's
        # "fewer, bigger boxes for bulk" outcomes intuitive; a violation means
        # the table (or a checker PR) needs a second look.
        for zone, boxes in sz.ZONE_RATES.items():
            ordered = sorted(boxes, key=sb.representative_billable_lb)
            for lighter, heavier in zip(ordered, ordered[1:]):
                self.assertLessEqual(
                    boxes[lighter]["base"],
                    boxes[heavier]["base"],
                    f"{zone}: {lighter} costs more than heavier {heavier}",
                )


class TestShippingZoneTableCoverage(unittest.TestCase):
    """Enforced automatically once the table is populated."""

    def test_every_mapped_zone_has_a_rate(self):
        for state, zone in sz.ZONE_BY_STATE.items():
            self.assertIn(zone, sz.ZONE_RATES, f"state {state} maps to {zone} with no rate rule")
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"{zone} is not one of the 5 zone ids")

    def test_full_state_coverage_when_configured(self):
        if not sz.is_configured():
            self.skipTest("21-state rate table not yet populated")
        mapped = set(sz.ZONE_BY_STATE)
        self.assertEqual(
            mapped,
            GREEN,
            f"mapped states {mapped} do not match green states {GREEN}",
        )

    def test_every_rate_rule_targets_a_real_zone(self):
        for zone in sz.ZONE_RATES:
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"rate rule for unknown zone {zone}")


class TestOrderShipping(unittest.TestCase):
    """compute_order_shipping: per-box totals from the packed plan."""

    TABLE = {"zone_1": BOX_RATES_Z1}

    def test_single_leafed_tree_prices_one_s20(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 1)], "leafed"), 22.0)

    def test_five_leafed_trees_take_two_boxes(self):
        # cap 4/box leafed -> 4 + 1 = two s20 boxes.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 5)], "leafed"), 44.0)

    def test_bulk_dormant_order_uses_bulk_box(self):
        # 50 dormant -> one b20 ($28), NOT 4 x s20 ($88).
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 50)], "dormant"), 28.0)

    def test_sixty_dormant_split_bulk_plus_small(self):
        # 60 -> b20 (50) + s20 (10) = 28 + 22 = 50.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 60)], "dormant"), 50.0)

    def test_short_trees_top_up_tall_box(self):
        # 1 x 46" + 14 x 20" dormant all fit the one s46 (cap 15) = 26.0.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 46, 1), ("bareroot", 20, 14)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "dormant"), 26.0)

    def test_single_dormant_whip_uses_whip_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 16, 1)], "dormant"), 18.0)

    def test_any_potted_item_fails_whole_order(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 20, 2), ("potted", 20, 1)]
            self.assertIsNone(sz.compute_order_shipping("WV", items, "leafed"))

    def test_missing_box_rate_fails_whole_order(self):
        # Only s20 priced: a 46" tree has no usable rated box -> None.
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"s20": {"base": 22.0}}}):
            self.assertIsNone(sz.compute_order_shipping("WV", [("bareroot", 46, 1)], "leafed"))

    def test_unmapped_state_returns_none(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("GA", [("bareroot", 20, 1)], "leafed"))

    def test_zero_and_negative_qty_ignored(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 20, 0), ("bareroot", 20, 1)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "leafed"), 22.0)
            self.assertIsNone(sz.compute_order_shipping("WV", [("bareroot", 20, 0)], "leafed"))

    def test_empty_cart_returns_none(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("WV", [], "leafed"))


class TestSingleTreeRate(unittest.TestCase):
    """single_tree_rate: the product-card "shipping from $X" estimate."""

    TABLE = {"zone_1": BOX_RATES_Z1}

    def test_leafed_single_is_smallest_leafed_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.single_tree_rate("WV", 20, "leafed"), 22.0)

    def test_dormant_whip_single_is_whip_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.single_tree_rate("WV", 16, "dormant"), 18.0)

    def test_unmapped_state_returns_none(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.single_tree_rate("GA", 20, "leafed"))


class TestCanonicalStateCode(unittest.TestCase):
    """GOL-1021 defect 1 — a ship-to state given as a full name or in odd case
    must canonicalize to its USPS code, so the checkout never silently drops the
    shipping line (under-billing) for a green-list state it does ship to."""

    def test_two_letter_code_passthrough(self):
        self.assertEqual(sz.canonical_state_code("OH"), "OH")

    def test_lowercase_and_padded_code(self):
        self.assertEqual(sz.canonical_state_code("  wv "), "WV")

    def test_full_name_any_case(self):
        self.assertEqual(sz.canonical_state_code("Ohio"), "OH")
        self.assertEqual(sz.canonical_state_code("west virginia"), "WV")

    def test_full_name_collapses_internal_whitespace(self):
        self.assertEqual(sz.canonical_state_code("West   Virginia"), "WV")

    def test_empty_and_none_return_none(self):
        self.assertIsNone(sz.canonical_state_code(""))
        self.assertIsNone(sz.canonical_state_code(None))

    def test_unknown_returns_none(self):
        self.assertIsNone(sz.canonical_state_code("Atlantis"))
        self.assertIsNone(sz.canonical_state_code("ZZ"))

    def test_name_map_covers_every_destination_code(self):
        # Every code in the destination universe must be reachable by name too,
        # or a customer typing a full state name would be routed to None.
        mapped_codes = set(sz._STATE_NAME_TO_CODE.values())
        self.assertEqual(mapped_codes, set(sz.US_STATES))


class TestFullNameShippingRouting(unittest.TestCase):
    """Green-list states supplied as full names must still price (defect 1)."""

    TABLE = {"zone_1": BOX_RATES_Z1}

    def test_full_name_green_state_prices_like_its_code(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            by_code = sz.compute_order_shipping("WV", [("bareroot", 20, 1)], "leafed")
            by_name = sz.compute_order_shipping("West Virginia", [("bareroot", 20, 1)], "leafed")
            self.assertEqual(by_name, by_code)
            self.assertEqual(by_name, 22.0)

    def test_full_name_non_green_state_still_drops(self):
        # "Ohio" canonicalizes to OH, but OH is not in this temp green table,
        # so it correctly returns None (no guessed charge) — the fail-safe holds.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("Ohio", [("bareroot", 20, 1)], "leafed"))


if __name__ == "__main__":
    unittest.main()
