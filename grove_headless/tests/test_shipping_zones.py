"""Tests for the per-box 5-zone shipping rate engine (Box Engine v2).

The engine in ``models/shipping_zones.py`` is pure Python, so these are plain
``unittest`` cases with no DB — they run both under Odoo's ``--test-enable``
runner and standalone (``python3 -m pytest`` / direct execution). The module is
loaded by file path so importing it never drags in the Odoo addon package.

Two layers:
  * Contract tests — assert the engine's fail-safe behaviour. These pass at all
    times and guard against regression on the core routing logic.
  * Table-coverage tests — assert the finished table is complete and self-
    consistent. They automatically enforce full coverage across all 32 green
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

# Independent pin of the 32 green states (deliberately NOT sz.GREEN_STATES:
# the test must catch an accidental edit to the module's set, so it keeps
# its own copy of the compliance list).
GREEN = frozenset(
    {
        "AL",
        "AR",
        "CT",
        "DC",
        "DE",
        "FL",
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

# A complete single-zone box rate table for contract tests. Both shippable
# tiers pack into the same p24x10x4 / p24x10x6 cartons (GOL-3285), so ONE table
# serves both engines. p24x10x6 < 2*p24x10x4 so 11-20 trees pick one larger
# carton over two smaller ones. (Bareroot holds 1-10 / 11-20 whips per carton;
# potted holds 1-5 / 6-10 units of the same cartons — same rate rows either way.)
BOX_RATES_Z1 = {
    "p24x10x4": {"base": 12.0},
    "p24x10x6": {"base": 18.0},
}

# Back-compat alias: pre-GOL-3285 tests used a separate "both catalogs" table
# when bareroot had its own small/large SKUs. The catalogs now share the p-boxes,
# so the single table above serves both tiers.
BOX_RATES_Z1_BOTH = BOX_RATES_Z1


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
        self.assertIsNone(sz.box_rate("ZZ", "p24x10x4"))
        self.assertIsNone(sz.compute_order_shipping("ZZ", [("bareroot", 20, 1)], "leafed"))

    def test_empty_or_missing_state_returns_none(self):
        self.assertIsNone(sz.box_rate("", "p24x10x4"))
        self.assertIsNone(sz.box_rate(None, "p24x10x4"))

    def test_there_are_exactly_five_rate_zones(self):
        # zone_1..5. GOL-2238's 2026-09-08 zone_6/zone_7 split was retired
        # 2026-09-14: TN re-binned to zone_1, AR/MO/IA folded back to zone_5.
        self.assertEqual(len(sz.RATE_ZONE_IDS), 5)

    def test_rate_is_box_scoped(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertEqual(sz.box_rate("WV", "p24x10x4"), 12.0)
            self.assertEqual(sz.box_rate("WV", "p24x10x6"), 18.0)

    def test_missing_box_rule_returns_none(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": {"p24x10x4": {"base": 12.0}}}):
            self.assertIsNone(sz.box_rate("WV", "p24x10x6"))

    def test_rates_load_from_json_file(self):
        # The shipped data file parses and, if non-empty, only contains known
        # zone ids and catalog box ids with numeric non-negative "base".
        for zone, boxes in sz.ZONE_RATES.items():
            self.assertIn(zone, sz.RATE_ZONE_IDS)
            for box_id, rule in boxes.items():
                # Zone rows span BOTH catalogs (bareroot + potted, GOL-2031).
                self.assertIn(box_id, sb.known_box_ids())
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

    def test_potted_is_shippable_at_potted_rates(self):
        # GOL-2199 potted go-live: potted / peat-and-bagged ships on its own
        # engine — no block message, priced from the p-boxes. One unit -> the
        # cheapest rated potted carton (p24x10x4 @ 12).
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertIsNone(sz.unshippable_reason([("potted", 20, 1)]))
            self.assertEqual(sz.compute_order_shipping("WV", [("potted", 20, 1)], "leafed"), 12.0)

    def test_potted_without_pbox_rates_fails_safe(self):
        # Before the rate-checker's first p-box table (or any zone row missing
        # the p-boxes): both shippable tiers pack into the p-boxes now, so a zone
        # row with NO rated p-box must price a potted cart as None (no shipping
        # line -> checkout $0-shipping breaker blocks), never guess a rate.
        with _temp_table({"WV": "zone_1"}, {"zone_1": {}}):
            self.assertIsNone(sz.unshippable_reason([("potted", 20, 1)]))
            self.assertIsNone(sz.compute_order_shipping("WV", [("potted", 20, 1)], "leafed"))

    def test_unknown_tier_treated_as_potted(self):
        # A mistagged product prices as potted (DEFAULT_TIER) — so it can never
        # ship with no charge (GOL-2199). One unit -> p24x10x4 @ 12.
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertIsNone(sz.unshippable_reason([("mystery", 20, 1)]))
            self.assertEqual(sz.compute_order_shipping("WV", [("mystery", 20, 1)], "leafed"), 12.0)

    def test_bareroot_has_no_unshippable_reason(self):
        self.assertIsNone(sz.unshippable_reason([("bareroot", 20, 3)]))

    def test_seed_line_is_skipped_not_blocked(self):
        # Seed pre-orders (GOL-3257) are not quoted at checkout — a seed line is
        # SKIPPED by packing, not routed through the non-shippable block seam.
        self.assertIn("seed", sz.TIERS)
        self.assertNotIn("seed", sz.SHIPPABLE_TIERS)
        self.assertIsNone(sz.unshippable_reason([("seed", 0, 2)]))

    def test_seed_line_contributes_no_shipping(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1_BOTH}):
            bareroot_only = sz.compute_order_shipping("WV", [("bareroot", 20, 1)], "leafed")
            with_seed = sz.compute_order_shipping("WV", [("bareroot", 20, 1), ("seed", 0, 5)], "leafed")
            self.assertEqual(with_seed, bareroot_only)

    def test_zero_qty_potted_line_is_ignored(self):
        self.assertIsNone(sz.unshippable_reason([("potted", 20, 0), ("bareroot", 20, 1)]))


class TestGreenStateCoverage(unittest.TestCase):
    """The 32-state green list and its rate coverage."""

    def test_exactly_the_green_states_are_mapped(self):
        self.assertEqual(set(sz.ZONE_BY_STATE), GREEN)

    def test_every_mapped_state_prices_every_catalog_box(self):
        for state in GREEN:
            for box_id in sb.BOXES:
                rate = sz.box_rate(state, box_id)
                self.assertIsNotNone(rate, f"{state}/{box_id} has no rate")
                self.assertGreater(rate, 0.0)

    def test_potted_coverage_is_all_or_nothing(self):
        # GOL-2199: until the rate-checker's first potted-inclusive table
        # merges, zone rows legitimately carry no potted boxes (potted ship-to
        # carts fail safe at the checkout breaker). But once ANY zone prices a
        # potted box, EVERY green state must price EVERY potted box — partial
        # coverage would make potted shippability vary by destination in a way
        # the checkout cannot explain to the shopper.
        any_potted = any(b in sb.POTTED_BOXES for boxes in sz.ZONE_RATES.values() for b in boxes)
        if not any_potted:
            self.skipTest("potted rates not yet published (GOL-2199 pre-first-table)")
        for state in GREEN:
            for box_id in sb.POTTED_BOXES:
                rate = sz.box_rate(state, box_id)
                self.assertIsNotNone(rate, f"{state}/{box_id} has no potted rate")
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
        # the table (or a checker PR) needs a second look. Asserted PER CATALOG
        # FAMILY: zone rows carry both bareroot and potted boxes (GOL-2031) and
        # each family has its own representative-weight function; cross-family
        # ordering is not a claim the packers rely on (they never mix families
        # in one plan).
        families = (
            (sb.BOXES, sb.representative_billable_lb),
            (sb.POTTED_BOXES, sb.potted_representative_billable_lb),
        )
        for zone, boxes in sz.ZONE_RATES.items():
            for catalog, weigh in families:
                ordered = sorted((b for b in boxes if b in catalog), key=weigh)
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
            self.skipTest("32-state rate table not yet populated")
        mapped = set(sz.ZONE_BY_STATE)
        self.assertEqual(
            mapped,
            GREEN,
            f"mapped states {mapped} do not match green states {GREEN}",
        )

    def test_every_rate_rule_targets_a_real_zone(self):
        for zone in sz.ZONE_RATES:
            self.assertIn(zone, sz.RATE_ZONE_IDS, f"rate rule for unknown zone {zone}")

    def test_gol2238_corrected_bindings(self):
        # GOL-2238 P1 regression (2026-09-14): TN must sit in zone_1 (it borders
        # KY/VA/NC and its worst corner quotes the zone_1 rate), and AR/MO/IA in
        # zone_5 — never the retired zone_6/zone_7. Guards against a re-introduced
        # far-corner mis-bin that had TN pricing above Iowa.
        if not sz.is_configured():
            self.skipTest("rate table not yet populated")
        self.assertEqual(sz.zone_for_state("TN"), "zone_1")
        for st in ("AR", "MO", "IA"):
            self.assertEqual(sz.zone_for_state(st), "zone_5", st)
        # No green state may map to a retired band.
        self.assertNotIn("zone_6", set(sz.ZONE_BY_STATE.values()))
        self.assertNotIn("zone_7", set(sz.ZONE_BY_STATE.values()))
        # TN (border state) must never cost more than a farther Gulf/mid-continent
        # state for any box — the exact inversion the P1 defect reported.
        for box_id in sb.known_box_ids():
            tn = sz.box_rate("TN", box_id)
            for far in ("AL", "IA", "MO"):
                self.assertLessEqual(tn, sz.box_rate(far, box_id), f"TN > {far} for {box_id}")


class TestOrderShipping(unittest.TestCase):
    """compute_order_shipping: per-box totals from the packed plan."""

    TABLE = {"zone_1": BOX_RATES_Z1}

    def test_single_tree_prices_one_small_carton(self):
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 1)], "leafed"), 12.0)

    def test_ten_trees_fit_one_small_carton(self):
        # Bareroot p24x10x4 holds 1-10 whips -> one carton ($12), not ten.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 10)], "leafed"), 12.0)

    def test_eleven_trees_use_one_large_carton(self):
        # 11-20 -> one p24x10x6 ($18), NOT two p24x10x4 ($24).
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 11)], "dormant"), 18.0)

    def test_twentyone_trees_split_large_plus_small(self):
        # 21 -> p24x10x6 (20) + p24x10x4 (1) = 18 + 12 = 30.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            self.assertEqual(sz.compute_order_shipping("WV", [("bareroot", 20, 21)], "dormant"), 30.0)

    def test_mixed_length_classes_pool_by_count(self):
        # 3 whip-class + 3 standard-class = 6 trees -> one p24x10x4 ($12).
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 16, 3), ("bareroot", 20, 3)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "dormant"), 12.0)

    def test_mixed_cart_prices_both_engines(self):
        # Bareroot and potted share the p-boxes but pack on their own engines and
        # never consolidate across tiers: 2 bareroot whips -> one p24x10x4 ($12);
        # 3 potted units -> one p24x10x4 ($12). Two separate cartons, summed: $24.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 20, 2), ("potted", 20, 3)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "dormant"), 24.0)

    def test_potted_units_pool_across_lines_into_boxes(self):
        # 4 + 3 = 7 potted units -> one p24x10x6 10-pack ($18), not two 5-packs
        # ($24) — pack_potted picks the cheapest combo, pooled across lines.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("potted", 20, 4), ("potted", 16, 3)]
            self.assertEqual(sz.compute_order_shipping("WV", items, "leafed"), 18.0)

    def test_mixed_cart_one_engine_unpackable_fails_whole_order(self):
        # Either engine failing to pack-and-price fails the WHOLE plan — a
        # half-priced order must never add a shipping line (fail-safe kept from
        # the pre-split contract). Here the bareroot side has a 30" tree no box
        # can hold while the potted side prices fine; the whole plan is None.
        with _temp_table({"WV": "zone_1"}, self.TABLE):
            items = [("bareroot", 30, 1), ("potted", 20, 1)]
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


class TestSinglePottedRate(unittest.TestCase):
    """single_potted_rate: the potted PDP "shipping from $X" estimate (GOL-2199)."""

    def test_single_potted_unit_is_the_small_potted_box(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertEqual(sz.single_potted_rate("WV"), 12.0)

    def test_no_pbox_rates_returns_none(self):
        # A zone row with no rated p-box quotes nothing for potted (both tiers
        # pack into the p-boxes now) — never a guessed fallback.
        with _temp_table({"WV": "zone_1"}, {"zone_1": {}}):
            self.assertIsNone(sz.single_potted_rate("WV"))

    def test_unmapped_state_returns_none(self):
        with _temp_table({"WV": "zone_1"}, {"zone_1": BOX_RATES_Z1}):
            self.assertIsNone(sz.single_potted_rate("GA"))


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


class TestSeedShipStates(unittest.TestCase):
    """Seeds ship to every US state + DC except HI/AK (GOL-3257, Josh 2026-10-13).
    Deliberately independent of sz.SEED_SHIP_STATES so an accidental edit to the
    module constant is caught."""

    # 48 contiguous states + DC = 49 (50 states - HI - AK + DC).
    EXPECTED = frozenset(
        {
            "AL",
            "AZ",
            "AR",
            "CA",
            "CO",
            "CT",
            "DE",
            "DC",
            "FL",
            "GA",
            "ID",
            "IL",
            "IN",
            "IA",
            "KS",
            "KY",
            "LA",
            "ME",
            "MD",
            "MA",
            "MI",
            "MN",
            "MS",
            "MO",
            "MT",
            "NE",
            "NV",
            "NH",
            "NJ",
            "NM",
            "NY",
            "NC",
            "ND",
            "OH",
            "OK",
            "OR",
            "PA",
            "RI",
            "SC",
            "SD",
            "TN",
            "TX",
            "UT",
            "VT",
            "VA",
            "WA",
            "WV",
            "WI",
            "WY",
        }
    )

    def test_exact_membership(self):
        self.assertEqual(sz.SEED_SHIP_STATES, self.EXPECTED)

    def test_count_is_49(self):
        self.assertEqual(len(sz.SEED_SHIP_STATES), 49)

    def test_excludes_hawaii_and_alaska(self):
        self.assertNotIn("HI", sz.SEED_SHIP_STATES)
        self.assertNotIn("AK", sz.SEED_SHIP_STATES)
        self.assertEqual(sz.SEED_SHIP_EXCLUDED_STATES, frozenset({"HI", "AK"}))

    def test_excludes_territories(self):
        for terr in ("PR", "VI", "GU", "AS", "MP"):
            self.assertNotIn(terr, sz.SEED_SHIP_STATES)

    def test_wider_than_the_green_list(self):
        # Seeds reach far more of the country than live trees.
        self.assertTrue(sz.GREEN_STATES <= sz.SEED_SHIP_STATES)
        self.assertGreater(len(sz.SEED_SHIP_STATES), len(sz.GREEN_STATES))


class TestStateZoneBands(unittest.TestCase):
    """STATE_ZONE_BANDS: the planner's ZIP-unknown USDA fallback (GOL-3379 §B)."""

    def test_every_green_state_has_a_band(self):
        # The acceptance invariant: one entry per green state, no gaps, no extras.
        self.assertEqual(set(sz.STATE_ZONE_BANDS), GREEN)

    def test_bands_are_ordered_cold_to_warm_pairs(self):
        for state, band in sz.STATE_ZONE_BANDS.items():
            self.assertEqual(len(band), 2, f"{state}: {band}")
            cold, warm = band
            self.assertTrue(1 <= cold <= warm <= 13, f"{state}: {band} out of USDA range")

    def test_spec_examples_hold(self):
        # The two worked examples from the spec shape.
        self.assertEqual(sz.STATE_ZONE_BANDS["WV"], [5, 7])
        self.assertEqual(sz.STATE_ZONE_BANDS["GA"], [7, 9])


if __name__ == "__main__":
    unittest.main()
