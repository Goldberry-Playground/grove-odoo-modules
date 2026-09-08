"""Tests for the per-box 5-zone shipping rate engine (Box Engine v2).

The engine in ``models/shipping_zones.py`` is pure Python, so these are plain
``unittest`` cases with no DB — they run both under Odoo's ``--test-enable``
runner and standalone (``python3 -m pytest`` / direct execution). The module is
loaded by file path so importing it never drags in the Odoo addon package.

Two layers:
  * Contract tests — assert the engine's fail-safe behaviour. These pass at all
    times and guard against regression on the core routing logic.
  * Table-coverage tests — assert the finished table is complete and self-
    consistent. They automatically enforce full coverage across all 31 green
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

# Independent pin of the 31 green states (deliberately NOT sz.GREEN_STATES:
# the test must catch an accidental edit to the module's set, so it keeps
# its own copy of the compliance list).
GREEN = frozenset(
    {
        "AL",
        "AR",
        "CT",
        "DC",
        "DE",
        "GA",
        "IA",
        "IL",
        "IN",
        "KY",
        "LA",
        "MA",
        "MD",
        "ME",
        "MI",
        "MN",
        "MO",
        "MS",
        "NC",
        "NH",
        "NJ",
        "NY",
        "OH",
        "PA",
        "RI",
        "SC",
        "TN",
        "VA",
        "VT",
        "WI",
        "WV",
    }
)

# A complete single-zone box rate table for contract tests (two-SKU catalog:
# small 1-5 trees, large 6-10; large < 2*small so 6-10 picks one large box).
BOX_RATES_Z1 = {
    "small": {"base": 12.0},
    "large": {"base": 18.0},
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
        self.assertIsNone(sz.box_rate("ZZ", "small"))
        self.assertIsNone(sz.compute_order_shipping("ZZ", [("bareroot", 20, 1)], "leafed"))

    def test_empty_or_missing_state_returns_none(self):
        self.assertIsNone(sz.box_rate("", "small"))
        self.assertIsNone(sz.box_rate(None, "small"))

    def test_there_are_exactly_five_rate_zones(self):
        self.assertEqual(len(sz.RATE_ZONE_IDS), 5)

    def test_rate_is_box_scoped(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertEqual(sz.box_rate("WV", "small"), 12.0)
            self.assertEqual(sz.box_rate("WV", "large"), 18.0)

    def test_missing_box_rule_returns_none(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"small": {"base": 12.0}}}):
            self.assertIsNone(sz.box_rate("WV", "large"))

    def test_rates_load_from_json_file(self):
        # The shipped data file parses and, if non-empty, only contains known
        # zone ids and catalog box ids with numeric non-negative "base".
        for zone, boxes in sz.ZONE_RATES.items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for box_id, rule in boxes.items():
                # The rate table covers BOTH catalogs: the bareroot Box Engine
                # (sb.BOXES) and the peat-and-bagged potted catalog
                # (sb.POTTED_BOXES, ids prefixed "p"). The rate-checker probes
                # both, so a box id is valid if it appears in either.
                self.assertIn(box_id, {**sb.BOXES, **sb.POTTED_BOXES})
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


class TestGreenStateCoverage(unittest.TestCase):
    """The 31-state green list and its rate coverage."""

    def test_exactly_the_green_states_are_mapped(self):
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
        # Monotonicity holds WITHIN a catalog, never across them. Bareroot and
        # potted price different products on different weight models
        # (PER_TREE_LB dormant 0.5 lb/tree vs POTTED_UNIT_LB 2.0 lb/unit), so a
        # potted box can legitimately cost more than a physically larger
        # bareroot one. Comparing them would fail on a correct table — and
        # sorting them together crashes outright, since
        # representative_billable_lb only knows sb.BOXES.
        catalogs = (
            ("bareroot", sb.BOXES, sb.representative_billable_lb),
            ("potted", sb.POTTED_BOXES, sb.potted_representative_billable_lb),
        )
        for zone, boxes in sz.ZONE_RATES.items():
            for label, catalog, weight_of in catalogs:
                ordered = sorted(
                    (b for b in boxes if b in catalog), key=weight_of
                )
                for lighter, heavier in zip(ordered, ordered[1:]):
                    self.assertLessEqual(
                        boxes[lighter]["base"],
                        boxes[heavier]["base"],
                        f"{zone} ({label}): {lighter} costs more than heavier {heavier}",
                    )


class TestShippingZoneTableCoverage(unittest.TestCase):
    """Enforced automatically once the table is populated."""

    def test_every_mapped_zone_has_a_rate(self):
        for state, zone in sz.ZONE_BY_STATE.items():
            self.assertIn(zone, sz.ZONE_RATES, f"state {state} maps to {zone} with no rate rule")
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"{zone} is not one of the 5 zone ids")

    def test_full_state_coverage_when_configured(self):
        if not sz.is_configured():
            self.skipTest("31-state rate table not yet populated")
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

    def test_single_tree_prices_one_small_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 1)], "leafed"), 12.0)

    def test_five_trees_fit_one_small_box(self):
        # Small holds 1-5 -> one box ($12), not five.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 5)], "leafed"), 12.0)

    def test_six_trees_use_one_large_box(self):
        # 6-10 -> one large box ($18), NOT two smalls ($24).
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 6)], "dormant"), 18.0)

    def test_eleven_trees_split_large_plus_small(self):
        # 11 -> large (10) + small (1) = 18 + 12 = 30.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 11)], "dormant"), 30.0)

    def test_mixed_length_classes_pool_by_count(self):
        # 3 whip-class + 3 standard-class = 6 trees -> one large box ($18).
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 16, 3), ("bareroot", 20, 3)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "dormant"), 18.0)

    def test_any_potted_item_fails_whole_order(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 20, 2), ("potted", 20, 1)]
            self.assertIsNone(sz.compute_order_shipping("WV", items, "leafed"))

    def test_tree_taller_than_any_box_fails_whole_order(self):
        # No box is longer than 24" -> a 30" tree can't be packed -> None.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("WV", [("bareroot", 30, 1)], "leafed"))

    def test_unmapped_state_returns_none(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("GA", [("bareroot", 20, 1)], "leafed"))

    def test_zero_and_negative_qty_ignored(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 20, 0), ("bareroot", 20, 1)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "leafed"), 12.0)
            self.assertIsNone(sz.compute_order_shipping("WV", [("bareroot", 20, 0)], "leafed"))

    def test_empty_cart_returns_none(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("WV", [], "leafed"))


class TestSingleTreeRate(unittest.TestCase):
    """single_tree_rate: the product-card "shipping from $X" estimate."""

    TABLE = {"zone_1": BOX_RATES_Z1}

    def test_single_tree_is_the_small_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.single_tree_rate("WV", 20, "leafed"), 12.0)

    def test_single_whip_class_is_also_the_small_box(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.single_tree_rate("WV", 16, "dormant"), 12.0)

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
            self.assertEqual(by_name, 12.0)

    def test_full_name_non_green_state_still_drops(self):
        # "Ohio" canonicalizes to OH, but OH is not in this temp green table,
        # so it correctly returns None (no guessed charge) — the fail-safe holds.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertIsNone(sz.compute_order_shipping("Ohio", [("bareroot", 20, 1)], "leafed"))


if __name__ == "__main__":
    unittest.main()
