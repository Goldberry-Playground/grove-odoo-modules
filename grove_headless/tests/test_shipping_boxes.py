"""Tests for the box catalog + packing engine (Box Engine v2, two-SKU catalog).

``models/shipping_boxes.py`` is pure Python (no Odoo, no DB) — plain unittest
cases loaded by file path, same pattern as ``test_shipping_zones.py``.

Catalog descoped to two SKUs by CEO directive 2026-09-07: ``small`` (24x6x4,
1-5 trees) and ``large`` (24x9x6, 6-10 trees), selected by tree count.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_boxes.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_boxes", _MODULE_PATH)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

# Cost table used by packing tests. large < 2*small so 6-10 trees pick the large
# box over two smalls (the intended count threshold under a monotone table).
COSTS = {"small": 12.0, "large": 18.0}


def cost_of(box_id):
    return COSTS.get(box_id)


def plan_summary(plan):
    """[(box_id, count), ...] sorted for stable assertions."""
    return sorted((pb.box_id, pb.count) for pb in plan)


class TestCatalog(unittest.TestCase):
    def test_catalog_is_the_two_descoped_skus(self):
        self.assertEqual(set(sb.BOXES), {"small", "large"})

    def test_every_box_is_usps_mailable(self):
        # USPS Ground Advantage hard limits: length + girth <= 130", weight <= 70 lb.
        for box_id, box in sb.BOXES.items():
            self.assertLessEqual(sb.length_plus_girth_in(box), sb.MAX_LENGTH_PLUS_GIRTH_IN, box_id)
            self.assertLessEqual(sb.representative_billable_lb(box_id), sb.MAX_SHIP_WEIGHT_LB, box_id)

    def test_length_plus_girth_matches_directive(self):
        # CEO directive spelled these out: small 44", large 54".
        self.assertEqual(sb.length_plus_girth_in(sb.BOXES["small"]), 44)
        self.assertEqual(sb.length_plus_girth_in(sb.BOXES["large"]), 54)

    def test_capacities_are_positive_and_mode_scoped(self):
        for box_id, box in sb.BOXES.items():
            self.assertTrue(box["capacity"], box_id)
            for mode, cap in box["capacity"].items():
                self.assertIn(mode, sb.MODES)
                self.assertGreater(cap, 0)

    def test_capacity_ranges_are_contiguous_and_non_overlapping(self):
        # 1-5 -> small, 6-10 -> large (both modes carry the same count).
        for mode in sb.MODES:
            self.assertEqual(sb.BOXES["small"]["capacity"][mode], 5)
            self.assertEqual(sb.BOXES["large"]["capacity"][mode], 10)

    def test_every_length_class_has_a_box_in_both_modes(self):
        # Both boxes are 24" long, so every supported class fits both, each mode.
        for cls in sb.LENGTH_CLASSES:
            for mode in sb.MODES:
                self.assertTrue(sb.usable_boxes(cls, mode), (cls, mode))


class TestWeights(unittest.TestCase):
    def test_no_dim_weight_below_one_cubic_foot(self):
        # Both descoped boxes are under 1 cu ft (small 576, large 1296) -> no DIM.
        self.assertEqual(sb.dim_weight_lb("small"), 0.0)
        self.assertEqual(sb.dim_weight_lb("large"), 0.0)

    def test_actual_weight_scales_with_count(self):
        lighter = sb.actual_weight_lb("large", 1, "dormant")
        heavier = sb.actual_weight_lb("large", 10, "dormant")
        self.assertGreater(heavier, lighter)

    def test_billable_is_actual_when_no_dim(self):
        # One dormant tree in the small box: tare 0.9 + 1*0.5 = 1.4 lb; no DIM.
        self.assertEqual(sb.billable_weight_lb("small", 1, "dormant"), 1.4)
        # Full leafed large: tare 1.4 + 10*2.0 = 21.4 lb; no DIM.
        self.assertEqual(sb.billable_weight_lb("large", 10, "leafed"), 21.4)

    def test_representative_billable_covers_worst_mode(self):
        # The rate-checker must quote the worst typical fill (never undercharge).
        for box_id, box in sb.BOXES.items():
            rep = sb.representative_billable_lb(box_id)
            for mode, cap in box["capacity"].items():
                self.assertGreaterEqual(rep, sb.billable_weight_lb(box_id, cap, mode), box_id)
        # Worst-case leafed fills: small ceil(10.9)=11, large ceil(21.4)=22.
        self.assertEqual(sb.representative_billable_lb("small"), 11)
        self.assertEqual(sb.representative_billable_lb("large"), 22)


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


class TestPacking(unittest.TestCase):
    def test_empty_cart_packs_empty(self):
        self.assertEqual(sb.pack_order([], "leafed", cost_of), [])

    def test_one_to_five_trees_use_one_small_box(self):
        for n in (1, 2, 5):
            plan = sb.pack_order([(20, n)], "dormant", cost_of)
            self.assertEqual(plan_summary(plan), [("small", n)], n)

    def test_six_to_ten_trees_use_one_large_box(self):
        for n in (6, 8, 10):
            plan = sb.pack_order([(20, n)], "dormant", cost_of)
            self.assertEqual(plan_summary(plan), [("large", n)], n)

    def test_eleven_trees_split_large_plus_small(self):
        plan = sb.pack_order([(20, 11)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("large", 10), ("small", 1)])

    def test_sixteen_trees_use_two_large(self):
        # 16: two large ($36) beat large+two small ($42).
        plan = sb.pack_order([(20, 16)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("large", 6), ("large", 10)])

    def test_mixed_length_classes_pool_by_total_count(self):
        # 3 whip-class + 3 standard-class = 6 trees -> one large box (pooled),
        # not two small boxes split by class.
        plan = sb.pack_order([(16, 3), (20, 3)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("large", 6)])

    def test_leafed_and_dormant_pack_the_same_counts(self):
        for mode in sb.MODES:
            self.assertEqual(plan_summary(sb.pack_order([(20, 5)], mode, cost_of)), [("small", 5)])
            self.assertEqual(plan_summary(sb.pack_order([(20, 6)], mode, cost_of)), [("large", 6)])

    def test_tree_taller_than_any_box_fails_safe(self):
        # No box is longer than 24" -> a 30" tree cannot be packed.
        self.assertIsNone(sb.pack_order([(30, 1)], "dormant", cost_of))

    def test_class_at_box_length_still_fits(self):
        # A tree needing exactly 24" fits (box length 24 >= 24).
        plan = sb.pack_order([(24, 1)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("small", 1)])

    def test_unknown_mode_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "potted", cost_of))

    def test_non_integer_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1.5)], "leafed", cost_of))

    def test_negative_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, -1)], "leafed", cost_of))

    def test_unrated_boxes_fail_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "leafed", lambda b: None))

    def test_partially_rated_catalog_still_packs(self):
        # Only small rated: 6 trees -> two small boxes (5 + 1).
        only_small = lambda b: 12.0 if b == "small" else None  # noqa: E731
        plan = sb.pack_order([(20, 6)], "dormant", only_small)
        self.assertEqual(plan_summary(plan), [("small", 1), ("small", 5)])

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


if __name__ == "__main__":
    unittest.main()
