"""Tests for the box catalog + packing engine (Box Engine v2, shared p-boxes).

``models/shipping_boxes.py`` is pure Python (no Odoo, no DB) — plain unittest
cases loaded by file path, same pattern as ``test_shipping_zones.py``.

Bareroot now packs into the SAME cartons as peat-and-bagged (Josh ruling
2026-10-13, GOL-3285): ``p24x10x4`` (24x10x4, 1-10 whips) and ``p24x10x6``
(24x10x6, 11-20 whips), selected by tree count. A bareroot whip holds twice as
many per carton as a peat-and-bagged unit and weighs about half (PER_TREE_LB
dormant 1.0 vs POTTED_UNIT_LB 2.0), so both engines land on the same 12 / 22 lb
representative points and share the p24x10x4 / p24x10x6 zone rate rows.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_boxes.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_boxes", _MODULE_PATH)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

# Cost table used by packing tests. p24x10x6 < 2*p24x10x4 so 11-20 whips pick the
# larger carton over two smaller ones (the intended count threshold under a
# monotone table).
COSTS = {"p24x10x4": 12.0, "p24x10x6": 18.0}


def cost_of(box_id):
    return COSTS.get(box_id)


def plan_summary(plan):
    """[(box_id, count), ...] sorted for stable assertions."""
    return sorted((pb.box_id, pb.count) for pb in plan)


class TestCatalog(unittest.TestCase):
    def test_catalog_is_the_two_shared_pboxes(self):
        self.assertEqual(set(sb.BOXES), {"p24x10x4", "p24x10x6"})

    def test_bareroot_shares_potted_geometry(self):
        # GOL-3285: the two engines pack into the same physical cartons, so where
        # an id is in both dicts its geometry + box-only tare must be identical
        # (only capacity / per-unit weight legitimately differ). This mirrors the
        # import-time drift assert in the module.
        for bid in set(sb.BOXES) & set(sb.POTTED_BOXES):
            for key in ("length", "width", "height", "tare_lb"):
                self.assertEqual(sb.BOXES[bid][key], sb.POTTED_BOXES[bid][key], (bid, key))

    def test_every_box_is_usps_mailable(self):
        # USPS Ground Advantage hard limits: length + girth <= 130", weight <= 70 lb.
        for box_id, box in sb.BOXES.items():
            self.assertLessEqual(sb.length_plus_girth_in(box), sb.MAX_LENGTH_PLUS_GIRTH_IN, box_id)
            self.assertLessEqual(sb.representative_billable_lb(box_id), sb.MAX_SHIP_WEIGHT_LB, box_id)

    def test_length_plus_girth_matches_dimensions(self):
        # Both cartons are 24" long: p24x10x4 -> 24+2*(10+4)=52, p24x10x6 -> 56.
        self.assertEqual(sb.length_plus_girth_in(sb.BOXES["p24x10x4"]), 52)
        self.assertEqual(sb.length_plus_girth_in(sb.BOXES["p24x10x6"]), 56)

    def test_capacities_are_positive_and_mode_scoped(self):
        for box_id, box in sb.BOXES.items():
            self.assertTrue(box["capacity"], box_id)
            for mode, cap in box["capacity"].items():
                self.assertIn(mode, sb.MODES)
                self.assertGreater(cap, 0)

    def test_capacity_ranges_are_contiguous_and_non_overlapping(self):
        # 1-10 -> p24x10x4, 11-20 -> p24x10x6 (both modes carry the same count),
        # and each holds twice the peat-and-bagged count of the same carton.
        for mode in sb.MODES:
            self.assertEqual(sb.BOXES["p24x10x4"]["capacity"][mode], 10)
            self.assertEqual(sb.BOXES["p24x10x6"]["capacity"][mode], 20)
        self.assertEqual(sb.BOXES["p24x10x4"]["capacity"]["dormant"], 2 * sb.POTTED_BOXES["p24x10x4"]["capacity"])
        self.assertEqual(sb.BOXES["p24x10x6"]["capacity"]["dormant"], 2 * sb.POTTED_BOXES["p24x10x6"]["capacity"])

    def test_bareroot_weighs_about_half_a_potted_unit(self):
        # Josh ruling: a bareroot whip weighs about half a peat-and-bagged unit.
        self.assertEqual(sb.PER_TREE_LB["dormant"], sb.POTTED_UNIT_LB / 2)

    def test_every_length_class_has_a_box_in_both_modes(self):
        # Both boxes are 24" long, so every supported class fits both, each mode.
        for cls in sb.LENGTH_CLASSES:
            for mode in sb.MODES:
                self.assertTrue(sb.usable_boxes(cls, mode), (cls, mode))


class TestWeights(unittest.TestCase):
    def test_no_dim_weight_below_one_cubic_foot(self):
        # Both cartons are under 1 cu ft (p24x10x4 960, p24x10x6 1440) -> no DIM.
        self.assertEqual(sb.dim_weight_lb("p24x10x4"), 0.0)
        self.assertEqual(sb.dim_weight_lb("p24x10x6"), 0.0)

    def test_actual_weight_scales_with_count(self):
        lighter = sb.actual_weight_lb("p24x10x6", 1, "dormant")
        heavier = sb.actual_weight_lb("p24x10x6", 20, "dormant")
        self.assertGreater(heavier, lighter)

    def test_actual_weight_has_no_paper_term(self):
        # The shared p-boxes carry no separate void-fill term: weight is tare +
        # per-tree only. One dormant whip in p24x10x4: 1.4 + 1*1.0 = 2.4.
        self.assertEqual(sb.actual_weight_lb("p24x10x4", 1, "dormant"), 2.4)

    def test_billable_is_actual_when_no_dim(self):
        # One dormant whip in p24x10x4: tare 1.4 + 1*1.0 = 2.4; no DIM.
        self.assertEqual(sb.billable_weight_lb("p24x10x4", 1, "dormant"), 2.4)
        # Full dormant p24x10x6: tare 1.5 + 20*1.0 = 21.5 lb; no DIM.
        self.assertEqual(sb.billable_weight_lb("p24x10x6", 20, "dormant"), 21.5)

    def test_representative_billable_quotes_dormant_not_leafed(self):
        # The published table prices the DORMANT parcel only: a bareroot tree
        # only ships (as bareroot) in its dormant window; the heavier leafed
        # weight prices a peat-and-bagged parcel sold under the potted tier. Rep
        # must cover every QUOTABLE mode's worst fill, but need NOT cover leafed.
        for box_id, box in sb.BOXES.items():
            rep = sb.representative_billable_lb(box_id)
            for mode in sb.QUOTABLE_MODES:
                cap = box["capacity"][mode]
                self.assertGreaterEqual(rep, sb.billable_weight_lb(box_id, cap, mode), box_id)
        # Dormant full fills: p24x10x4 ceil(1.4+10*1.0)=ceil(11.4)=12,
        # p24x10x6 ceil(1.5+20*1.0)=ceil(21.5)=22.
        self.assertEqual(sb.representative_billable_lb("p24x10x4"), 12)
        self.assertEqual(sb.representative_billable_lb("p24x10x6"), 22)
        # And it must NOT be inflated by the un-shippable leafed weight
        # (leafed full p24x10x4 -> 22, p24x10x6 -> 42).
        self.assertLess(sb.representative_billable_lb("p24x10x4"), 22)
        self.assertLess(sb.representative_billable_lb("p24x10x6"), 42)

    def test_bareroot_reps_match_potted_reps(self):
        # The whole point of GOL-3285: both engines land on the same 12 / 22 lb
        # representative points for the same cartons, so bareroot reuses the
        # existing p-box zone rate rows with no re-derive.
        for box_id in set(sb.BOXES) & set(sb.POTTED_BOXES):
            self.assertEqual(
                sb.representative_billable_lb(box_id),
                sb.potted_representative_billable_lb(box_id),
                box_id,
            )


class TestWeightBasisAndCalibration(unittest.TestCase):
    """Packed-weight calibration + weight_basis (GOL-3201). The retired small/
    large calibration seed does not carry to the new cartons (GOL-3285), so both
    bareroot boxes start unverified and quote the conservative full-capacity
    weight (which equals the potted representative, so no rate moves)."""

    def setUp(self):
        # Snapshot the module calibration seed so patches never leak between tests.
        self._saved = dict(sb.WEIGHT_CALIBRATION)
        self.addCleanup(lambda: sb.WEIGHT_CALIBRATION.clear() or sb.WEIGHT_CALIBRATION.update(self._saved))

    def test_seed_is_empty_and_both_boxes_unverified(self):
        self.assertEqual(sb.WEIGHT_CALIBRATION, {})
        self.assertEqual(sb.weight_basis("p24x10x4"), "unverified")
        self.assertEqual(sb.weight_basis("p24x10x6"), "unverified")

    def test_unverified_quotes_the_conservative_full_capacity_weight(self):
        # p24x10x4 12, p24x10x6 22 — the conservative worst case, unchanged.
        self.assertEqual(sb.representative_billable_lb("p24x10x4"), 12)
        self.assertEqual(sb.representative_billable_lb("p24x10x6"), 22)
        for box_id in sb.BOXES:
            self.assertEqual(sb.representative_billable_lb(box_id), sb.conservative_billable_lb(box_id))

    def test_verified_prices_the_calibrated_median_not_full_capacity(self):
        # Verify the mechanism really diverges from the conservative path once a
        # measured median lands: p24x10x4 calibrated to 9.5 lb quotes 10, below
        # its 12 lb worst case. (p24x10x4 is not force-conservative.)
        sb.WEIGHT_CALIBRATION["p24x10x4"] = {"samples": 13, "typical_billable_lb": 9.5}
        self.assertEqual(sb.weight_basis("p24x10x4"), "verified")
        self.assertEqual(sb.representative_billable_lb("p24x10x4"), 10)
        self.assertLess(sb.representative_billable_lb("p24x10x4"), sb.conservative_billable_lb("p24x10x4"))

    def test_fewer_than_min_samples_stays_unverified(self):
        sb.WEIGHT_CALIBRATION["p24x10x4"] = {
            "samples": sb.MIN_CALIBRATION_SAMPLES - 1,
            "typical_billable_lb": 9.5,
        }
        self.assertEqual(sb.weight_basis("p24x10x4"), "unverified")
        self.assertEqual(sb.representative_billable_lb("p24x10x4"), sb.conservative_billable_lb("p24x10x4"))

    def test_p24x10x6_is_force_conservative_even_with_samples(self):
        sb.WEIGHT_CALIBRATION["p24x10x6"] = {"samples": 99, "typical_billable_lb": 14.0}
        self.assertEqual(sb.weight_basis("p24x10x6"), "unverified")
        self.assertEqual(sb.representative_billable_lb("p24x10x6"), 22)
        self.assertIn("p24x10x6", sb.FORCE_CONSERVATIVE_BOXES)

    def test_missing_calibration_entry_is_unverified(self):
        sb.WEIGHT_CALIBRATION.pop("p24x10x4", None)
        self.assertEqual(sb.weight_basis("p24x10x4"), "unverified")
        self.assertEqual(sb.representative_billable_lb("p24x10x4"), sb.conservative_billable_lb("p24x10x4"))

    def test_every_box_has_a_known_basis(self):
        for box_id in sb.BOXES:
            self.assertIn(sb.weight_basis(box_id), ("verified", "unverified"))


class TestPackingMode(unittest.TestCase):
    def test_winter_is_dormant(self):
        self.assertEqual(sb.packing_mode(date(2026, 1, 15)), "dormant")
        self.assertEqual(sb.packing_mode(date(2026, 11, 1)), "dormant")
        self.assertEqual(sb.packing_mode(date(2026, 12, 31)), "dormant")

    def test_window_edges(self):
        self.assertEqual(sb.packing_mode(date(2026, 4, 15)), "dormant")
        self.assertEqual(sb.packing_mode(date(2026, 4, 16)), "leafed")
        self.assertEqual(sb.packing_mode(date(2026, 10, 31)), "leafed")

    def test_summer_is_leafed(self):
        self.assertEqual(sb.packing_mode(date(2026, 7, 31)), "leafed")


class TestBarerootSeasonGate(unittest.TestCase):
    """The seasonal gate that makes the dormant-only rate table hold (GOL-1906,
    Josh 2026-09-07): bareroot may only be labeled / ship now inside the dormancy
    window; every other date is a preorder for the next dormant wave."""

    def test_dormant_dates_can_ship_bareroot(self):
        # Inside the window (wraps year end) and on both inclusive edges.
        for d in (date(2026, 11, 1), date(2026, 12, 31), date(2026, 1, 15), date(2026, 4, 15)):
            self.assertTrue(sb.can_ship_bareroot(d), d)

    def test_leafed_dates_cannot_ship_bareroot(self):
        # Just outside each edge, and mid-summer — the Apr 16–Jun 6 band is the
        # exact gap where a zone Arbor Day window still reads "in window" but the
        # nursery is out of dormancy (the undercharge this gate closes).
        for d in (date(2026, 4, 16), date(2026, 5, 15), date(2026, 6, 6), date(2026, 7, 31), date(2026, 10, 31)):
            self.assertFalse(sb.can_ship_bareroot(d), d)

    def test_gate_tracks_quotable_modes_exactly(self):
        # can_ship_bareroot is the ship/no-ship face of QUOTABLE_MODES — never let
        # the two drift, or a mode we quote could be one we refuse to label.
        for d in (date(2026, 2, 1), date(2026, 7, 1), date(2026, 11, 15)):
            self.assertEqual(sb.can_ship_bareroot(d), sb.packing_mode(d) in sb.QUOTABLE_MODES, d)


class TestInjectedDormancyWindow(unittest.TestCase):
    """The dormancy window is Odoo-editable (GOL-1906, Josh 2026-09-07): the
    constants are only a seed, and callers inject the live window. These use a
    NON-DEFAULT window so they prove the injection path drives the result rather
    than re-testing the module constants."""

    # A non-default window that also wraps the year end (Dec 15 -> Mar 31), so
    # the wrap is exercised on injected values, not just the default.
    WRAP = ((12, 15), (3, 31))

    def test_injected_wrap_window_in_and_out(self):
        # Inside the injected wrap window -> dormant; outside -> leafed.
        self.assertEqual(sb.packing_mode(date(2026, 1, 15), self.WRAP), "dormant")
        self.assertEqual(sb.packing_mode(date(2026, 6, 1), self.WRAP), "leafed")

    def test_injection_overrides_the_constants(self):
        # Nov 1 is DORMANT under the default (11-01..04-15) but LEAFED under the
        # injected Dec 15..Mar 31 — the only way this passes is if the argument,
        # not the module constant, decided. This is the core injection proof.
        self.assertEqual(sb.packing_mode(date(2026, 11, 1)), "dormant")  # default
        self.assertEqual(sb.packing_mode(date(2026, 11, 1), self.WRAP), "leafed")  # injected
        self.assertFalse(sb.can_ship_bareroot(date(2026, 11, 1), self.WRAP))
        self.assertTrue(sb.can_ship_bareroot(date(2026, 1, 15), self.WRAP))

    def test_non_wrapping_injected_window(self):
        # A summer, non-wrapping window (Jun 1 -> Sep 15) still works: dormant in,
        # leafed out — including the default-dormant Jan date now reading leafed.
        summer = ((6, 1), (9, 15))
        self.assertEqual(sb.packing_mode(date(2026, 7, 1), summer), "dormant")
        self.assertEqual(sb.packing_mode(date(2026, 1, 15), summer), "leafed")

    def test_none_window_falls_back_to_default(self):
        # Explicit None (the pure-caller default) matches DEFAULT_WINDOW exactly.
        for d in (date(2026, 1, 15), date(2026, 7, 1), date(2026, 11, 1)):
            self.assertEqual(sb.packing_mode(d, None), sb.packing_mode(d, sb.DEFAULT_WINDOW), d)


class TestParseWindow(unittest.TestCase):
    """`parse_window` validates the two MM-DD config values and FAILS CLOSED —
    a malformed date must raise, never silently fall back to the seed (GOL-1906,
    Josh 2026-09-07): a silent fallback is how a wrong window ships underpriced
    labels unnoticed."""

    def test_valid_padded_and_unpadded(self):
        self.assertEqual(sb.parse_window("11-01", "04-15"), ((11, 1), (4, 15)))
        self.assertEqual(sb.parse_window("3-1", "9-15"), ((3, 1), (9, 15)))

    def test_leap_day_allowed(self):
        self.assertEqual(sb.parse_window("02-29", "03-01"), ((2, 29), (3, 1)))

    def test_year_bearing_value_rejected(self):
        # A full date with a year would break the year-wrap logic — reject it.
        with self.assertRaises(ValueError):
            sb.parse_window("2026-11-01", "04-15")

    def test_impossible_dates_rejected(self):
        for bad in ("13-01", "00-10", "02-30", "11-31"):
            with self.assertRaises(ValueError):
                sb.parse_window(bad, "04-15")

    def test_non_integer_and_empty_rejected(self):
        for bad in ("nov-01", "", "11", "11-", "-15"):
            with self.assertRaises(ValueError):
                sb.parse_window(bad, "04-15")
        with self.assertRaises(ValueError):
            sb.parse_window(None, "04-15")

    def test_equal_endpoints_rejected(self):
        # start == end -> the wrap makes EVERY day dormant (leafed gate disabled),
        # the "inverted / zero-length" case that would undercharge all year.
        with self.assertRaises(ValueError):
            sb.parse_window("05-01", "05-01")

    def test_start_after_end_is_valid_wrap(self):
        # start > end is the NORMAL wrapping window (Nov -> Apr), not an error.
        self.assertEqual(sb.parse_window("11-01", "04-15"), ((11, 1), (4, 15)))


class TestPacking(unittest.TestCase):
    def test_empty_cart_packs_empty(self):
        self.assertEqual(sb.pack_order([], "leafed", cost_of), [])

    def test_one_to_ten_trees_use_one_small_carton(self):
        for n in (1, 2, 5, 10):
            plan = sb.pack_order([(20, n)], "dormant", cost_of)
            self.assertEqual(plan_summary(plan), [("p24x10x4", n)], n)

    def test_eleven_to_twenty_trees_use_one_large_carton(self):
        for n in (11, 15, 20):
            plan = sb.pack_order([(20, n)], "dormant", cost_of)
            self.assertEqual(plan_summary(plan), [("p24x10x6", n)], n)

    def test_twentyone_trees_split_large_plus_small(self):
        plan = sb.pack_order([(20, 21)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("p24x10x4", 1), ("p24x10x6", 20)])

    def test_forty_trees_use_two_large(self):
        # 40: two large ($36) beat large+two small ($42).
        plan = sb.pack_order([(20, 40)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("p24x10x6", 20), ("p24x10x6", 20)])

    def test_mixed_length_classes_pool_by_total_count(self):
        # 3 whip-class + 3 standard-class = 6 trees -> one small carton (pooled),
        # not two cartons split by class.
        plan = sb.pack_order([(16, 3), (20, 3)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("p24x10x4", 6)])

    def test_leafed_and_dormant_pack_the_same_counts(self):
        for mode in sb.MODES:
            self.assertEqual(plan_summary(sb.pack_order([(20, 10)], mode, cost_of)), [("p24x10x4", 10)])
            self.assertEqual(plan_summary(sb.pack_order([(20, 11)], mode, cost_of)), [("p24x10x6", 11)])

    def test_tree_taller_than_any_box_fails_safe(self):
        # No box is longer than 24" -> a 30" tree cannot be packed.
        self.assertIsNone(sb.pack_order([(30, 1)], "dormant", cost_of))

    def test_class_at_box_length_still_fits(self):
        # A tree needing exactly 24" fits (box length 24 >= 24).
        plan = sb.pack_order([(24, 1)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("p24x10x4", 1)])

    def test_unknown_mode_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "potted", cost_of))

    def test_non_integer_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1.5)], "leafed", cost_of))

    def test_negative_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, -1)], "leafed", cost_of))

    def test_unrated_boxes_fail_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "leafed", lambda b: None))

    def test_partially_rated_catalog_still_packs(self):
        # Only p24x10x4 rated: 11 trees -> two p24x10x4 boxes (10 + 1).
        only_small = lambda b: 12.0 if b == "p24x10x4" else None  # noqa: E731
        plan = sb.pack_order([(20, 11)], "dormant", only_small)
        self.assertEqual(plan_summary(plan), [("p24x10x4", 1), ("p24x10x4", 10)])

    def test_no_box_ever_exceeds_capacity(self):
        for mode in sb.MODES:
            for qty in (1, 7, 23, 50, 137):
                plan = sb.pack_order([(20, qty)], mode, cost_of)
                self.assertIsNotNone(plan, (mode, qty))
                self.assertEqual(sum(pb.count for pb in plan), qty)
                for pb in plan:
                    cap = sb.BOXES[pb.box_id]["capacity"][mode]
                    self.assertLessEqual(pb.count, cap)

    def test_deterministic(self):
        a = plan_summary(sb.pack_order([(20, 37)], "dormant", cost_of))
        b = plan_summary(sb.pack_order([(20, 37)], "dormant", cost_of))
        self.assertEqual(a, b)


class TestPackedBoxTier(unittest.TestCase):
    """GOL-3291: since bareroot and potted share the p24x10x4 / p24x10x6 carton
    ids (GOL-3285), the box id alone can no longer pick the label weight model.
    ``PackedBox`` carries the packing ENGINE (``tier``) so the declared label
    weight is unambiguous regardless of the shared id."""

    def test_pack_order_stamps_bareroot_tier(self):
        for pb in sb.pack_order([(20, 15)], "dormant", cost_of):
            self.assertEqual(pb.tier, "bareroot")

    def test_pack_potted_stamps_potted_tier(self):
        for pb in sb.pack_potted(8, cost_of):
            self.assertEqual(pb.tier, "potted")

    def test_default_tier_is_bareroot(self):
        # Back-compat: historical single-engine callers/tests that build a
        # PackedBox with no tier keep the bareroot weight model.
        self.assertEqual(sb.PackedBox("p24x10x4", 5).tier, "bareroot")

    def test_potted_box_declares_potted_weight_in_dormant_mode(self):
        # The bug: a potted box in the shared carton, weighed in "dormant" mode,
        # must NOT use the lighter bareroot PER_TREE_LB (1.0). A 5-pack potted
        # p24x10x4 = tare 1.4 + 5*POTTED_UNIT_LB(2.0) = 11.4, never 1.4+5*1.0=6.4.
        potted = sb.PackedBox("p24x10x4", 5, tier="potted")
        self.assertEqual(potted.declared_weight_lb("dormant"), 11.4)
        self.assertEqual(potted.declared_weight_lb("dormant"), sb.potted_actual_weight_lb("p24x10x4", 5))
        # And mode-independent: a potted box weighs the same in leafed.
        self.assertEqual(potted.declared_weight_lb("leafed"), potted.declared_weight_lb("dormant"))

    def test_bareroot_box_declares_bareroot_weight_by_mode(self):
        bareroot = sb.PackedBox("p24x10x4", 5, tier="bareroot")
        self.assertEqual(bareroot.declared_weight_lb("dormant"), sb.actual_weight_lb("p24x10x4", 5, "dormant"))
        # Dormant 1.4+5*1.0=6.4; leafed 1.4+5*2.0=11.4 — mode matters for bareroot.
        self.assertEqual(bareroot.declared_weight_lb("dormant"), 6.4)
        self.assertEqual(bareroot.declared_weight_lb("leafed"), 11.4)

    def test_shared_id_two_tiers_diverge_in_dormant(self):
        # Same carton id + count, different engine -> different declared weight in
        # dormant mode (the under-declare GOL-3291 fixes). They coincide in leafed.
        bareroot = sb.PackedBox("p24x10x6", 10, tier="bareroot")
        potted = sb.PackedBox("p24x10x6", 10, tier="potted")
        self.assertLess(bareroot.declared_weight_lb("dormant"), potted.declared_weight_lb("dormant"))
        self.assertEqual(bareroot.declared_weight_lb("leafed"), potted.declared_weight_lb("dormant"))

    def test_declared_label_weight_lb_dispatch(self):
        # The module-level dispatch both label callers share.
        self.assertEqual(
            sb.declared_label_weight_lb("p24x10x4", 5, "dormant", "potted"),
            sb.potted_actual_weight_lb("p24x10x4", 5),
        )
        self.assertEqual(
            sb.declared_label_weight_lb("p24x10x4", 5, "dormant", "bareroot"),
            sb.actual_weight_lb("p24x10x4", 5, "dormant"),
        )
        # Unknown/absent tier falls back to the bareroot model (the safe default).
        self.assertEqual(
            sb.declared_label_weight_lb("p24x10x4", 5, "dormant"),
            sb.actual_weight_lb("p24x10x4", 5, "dormant"),
        )


if __name__ == "__main__":
    unittest.main()
