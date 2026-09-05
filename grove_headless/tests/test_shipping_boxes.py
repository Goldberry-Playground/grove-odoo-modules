"""Tests for the box catalog + packing engine (Box Engine v2).

``models/shipping_boxes.py`` is pure Python (no Odoo, no DB) — plain unittest
cases loaded by file path, same pattern as ``test_shipping_zones.py``.
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "shipping_boxes.py")
_spec = importlib.util.spec_from_file_location("grove_shipping_boxes", _MODULE_PATH)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

# Cost table used by packing tests (provisional zone_1 card).
COSTS = {"br16": 18.0, "s20": 22.0, "s32": 24.0, "s46": 26.0, "b20": 28.0, "b32": 30.0}


def cost_of(box_id):
    return COSTS.get(box_id)


def plan_summary(plan):
    """[(box_id, count), ...] sorted for stable assertions."""
    return sorted((pb.box_id, pb.count) for pb in plan)


class TestCatalog(unittest.TestCase):
    def test_every_box_is_usps_mailable(self):
        # USPS Ground Advantage hard limits: length + girth <= 130", weight <= 70 lb.
        for box_id, box in sb.BOXES.items():
            self.assertLessEqual(sb.length_plus_girth_in(box), sb.MAX_LENGTH_PLUS_GIRTH_IN, box_id)
            self.assertLessEqual(sb.representative_billable_lb(box_id), sb.MAX_SHIP_WEIGHT_LB, box_id)

    def test_capacities_are_positive_and_mode_scoped(self):
        for box_id, box in sb.BOXES.items():
            self.assertTrue(box["capacity"], box_id)
            for mode, cap in box["capacity"].items():
                self.assertIn(mode, sb.MODES)
                self.assertGreater(cap, 0)

    def test_bulk_boxes_are_dormant_only(self):
        # Leafed canopy never packs into the 12x12 bulk boxes or the whip box.
        for box_id in ("b20", "b32", "br16"):
            self.assertNotIn("leafed", sb.BOXES[box_id]["capacity"], box_id)

    def test_every_length_class_has_a_dormant_box(self):
        for cls in sb.LENGTH_CLASSES:
            self.assertTrue(sb.usable_boxes(cls, "dormant"), cls)

    def test_every_length_class_above_16_has_a_leafed_box(self):
        for cls in sb.LENGTH_CLASSES:
            if cls > 16:
                self.assertTrue(sb.usable_boxes(cls, "leafed"), cls)


class TestWeights(unittest.TestCase):
    def test_dim_weight_respects_usps_cubic_foot_threshold(self):
        # USPS Ground Advantage applies DIM only above 1 cu ft (1728 cu in).
        # s20 = 20x8x8 = 1280 cu in (<= 1 cu ft) -> no DIM.
        self.assertEqual(sb.dim_weight_lb("s20"), 0.0)
        # b32 = 32x12x12 = 4608 cu in (> 1 cu ft) -> 4608 / 139 = 33.2 lb.
        self.assertEqual(sb.dim_weight_lb("b32"), 33.2)

    def test_actual_weight_scales_with_count(self):
        lighter = sb.actual_weight_lb("s20", 1, "dormant")
        heavier = sb.actual_weight_lb("s20", 15, "dormant")
        self.assertGreater(heavier, lighter)

    def test_billable_is_max_of_actual_and_dim(self):
        # One dormant whip in the s20 (<= 1 cu ft, no USPS DIM): billable is the
        # actual scale weight, tare 1.6 + 1*0.5 = 2.1 lb.
        self.assertEqual(sb.billable_weight_lb("s20", 1, "dormant"), 2.1)
        # Full dormant b20 (> 1 cu ft): actual 2.9 + 50*0.5 = 27.9 > DIM 20.7.
        self.assertEqual(sb.billable_weight_lb("b20", 50, "dormant"), 27.9)
        # One dormant whip in the b32 (> 1 cu ft): actual 4.1 + 0.5 = 4.6 lb but
        # DIM 33.2 dominates -> billable 33.2.
        self.assertEqual(sb.billable_weight_lb("b32", 1, "dormant"), 33.2)

    def test_representative_billable_covers_worst_mode(self):
        # The rate-checker must quote the worst typical fill (never undercharge).
        for box_id, box in sb.BOXES.items():
            rep = sb.representative_billable_lb(box_id)
            for mode, cap in box["capacity"].items():
                self.assertGreaterEqual(rep, sb.billable_weight_lb(box_id, cap, mode), box_id)


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

    def test_single_leafed_tree(self):
        plan = sb.pack_order([(20, 1)], "leafed", cost_of)
        self.assertEqual(plan_summary(plan), [("s20", 1)])

    def test_five_leafed_trees_split_four_one(self):
        plan = sb.pack_order([(20, 5)], "leafed", cost_of)
        self.assertEqual(plan_summary(plan), [("s20", 1), ("s20", 4)])

    def test_fifty_dormant_use_one_bulk_box(self):
        plan = sb.pack_order([(20, 50)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("b20", 50)])

    def test_sixty_dormant_bulk_plus_small(self):
        plan = sb.pack_order([(20, 60)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("b20", 50), ("s20", 10)])

    def test_packing_is_cost_optimal_not_greedy_by_size(self):
        # 16 dormant: b20 ($28) beats 2 x s20 ($44) — but 15 fit one s20 ($22).
        self.assertEqual(plan_summary(sb.pack_order([(20, 15)], "dormant", cost_of)), [("s20", 15)])
        self.assertEqual(plan_summary(sb.pack_order([(20, 16)], "dormant", cost_of)), [("b20", 16)])

    def test_short_trees_top_up_taller_boxes_first(self):
        # 1 x 46" opens an s46 (dormant cap 15); 14 x 20" ride along free.
        plan = sb.pack_order([(46, 1), (20, 14)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("s46", 15)])

    def test_overflow_after_top_up_opens_cheapest_box(self):
        # 1 x 46" + 20 x 20": 14 top up the s46, 6 need one s20.
        plan = sb.pack_order([(46, 1), (20, 20)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("s20", 6), ("s46", 15)])

    def test_dormant_single_whip_gets_whip_box(self):
        plan = sb.pack_order([(16, 1)], "dormant", cost_of)
        self.assertEqual(plan_summary(plan), [("br16", 1)])

    def test_leafed_whip_class_rides_a_real_box(self):
        # br16 has no leafed capacity; a 16" tree ships leafed in an s20.
        plan = sb.pack_order([(16, 1)], "leafed", cost_of)
        self.assertEqual(plan_summary(plan), [("s20", 1)])

    def test_unknown_length_class_fails_safe(self):
        self.assertIsNone(sb.pack_order([(24, 1)], "leafed", cost_of))

    def test_unknown_mode_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "potted", cost_of))

    def test_non_integer_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1.5)], "leafed", cost_of))

    def test_negative_qty_fails_safe(self):
        self.assertIsNone(sb.pack_order([(20, -1)], "leafed", cost_of))

    def test_unrated_boxes_fail_safe(self):
        self.assertIsNone(sb.pack_order([(20, 1)], "leafed", lambda b: None))

    def test_partially_rated_catalog_still_packs(self):
        # Only s20 rated: 5 leafed trees -> two s20 boxes.
        only_s20 = lambda b: 22.0 if b == "s20" else None  # noqa: E731
        plan = sb.pack_order([(20, 5)], "leafed", only_s20)
        self.assertEqual(plan_summary(plan), [("s20", 1), ("s20", 4)])

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
        a = plan_summary(sb.pack_order([(46, 2), (32, 9), (20, 30)], "dormant", cost_of))
        b = plan_summary(sb.pack_order([(46, 2), (32, 9), (20, 30)], "dormant", cost_of))
        self.assertEqual(a, b)


# ── Potted / peat-and-bagged engine (GOL-2031) ───────────────────────────────
# Provisional zone_1 potted card; real rates come from rate_check once the
# potted boxes are in the live Shippo probe list.
POTTED_COSTS = {"p24x6": 20.0, "p24x9": 32.0}


def potted_cost_of(box_id):
    return POTTED_COSTS.get(box_id)


class TestPottedCatalog(unittest.TestCase):
    def test_capacities_positive_and_scalar(self):
        for box_id, box in sb.POTTED_BOXES.items():
            self.assertIsInstance(box["capacity"], int, box_id)
            self.assertGreater(box["capacity"], 0, box_id)

    def test_boxes_clear_usps_length_plus_girth(self):
        for box_id, box in sb.POTTED_BOXES.items():
            self.assertLessEqual(sb.length_plus_girth_in(box), sb.MAX_LENGTH_PLUS_GIRTH_IN, box_id)

    def test_bench_geometry_matches_josh(self):
        # Josh's real bench boxes for leafed-out trees (2026-09-05).
        def dims(box_id):
            b = sb.POTTED_BOXES[box_id]
            return (b["length"], b["width"], b["height"])

        self.assertEqual(dims("p24x6"), (24, 6, 4))
        self.assertEqual(dims("p24x9"), (24, 9, 6))
        # Both are 24" long (> 22") -> USPS nonstandard-length surcharge applies.
        for box in sb.POTTED_BOXES.values():
            self.assertGreater(box["length"], 22)

    def test_potted_is_separate_from_bareroot(self):
        # No id collision — a mistagged tier can never cross catalogs.
        self.assertFalse(set(sb.POTTED_BOXES) & set(sb.BOXES))


class TestPottedWeights(unittest.TestCase):
    def test_dim_is_zero_under_one_cubic_foot(self):
        # p24x9 = 24x9x6 = 1296 cu in (<= 1 cu ft) -> no USPS DIM.
        self.assertEqual(sb.potted_dim_weight_lb("p24x9"), 0.0)

    def test_actual_scales_with_units(self):
        self.assertGreater(sb.potted_actual_weight_lb("p24x9", 10), sb.potted_actual_weight_lb("p24x9", 1))

    def test_representative_under_seventy_pound_ceiling(self):
        for box_id in sb.POTTED_BOXES:
            rep = sb.potted_representative_billable_lb(box_id)
            self.assertLessEqual(rep, sb.MAX_SHIP_WEIGHT_LB, box_id)
            self.assertGreaterEqual(
                rep, sb.potted_billable_weight_lb(box_id, sb.POTTED_BOXES[box_id]["capacity"]), box_id
            )


class TestPottedPacking(unittest.TestCase):
    def test_empty_packs_empty(self):
        self.assertEqual(sb.pack_potted(0, potted_cost_of), [])

    def test_single_unit_uses_small_box(self):
        self.assertEqual(plan_summary(sb.pack_potted(1, potted_cost_of)), [("p24x6", 1)])

    def test_five_units_one_small_box(self):
        self.assertEqual(plan_summary(sb.pack_potted(5, potted_cost_of)), [("p24x6", 5)])

    def test_six_units_prefer_one_large_over_two_small(self):
        # 6 units: one p24x9 ($32) beats two p24x6 ($40).
        self.assertEqual(plan_summary(sb.pack_potted(6, potted_cost_of)), [("p24x9", 6)])

    def test_ten_units_one_large_box(self):
        self.assertEqual(plan_summary(sb.pack_potted(10, potted_cost_of)), [("p24x9", 10)])

    def test_eleven_units_large_plus_small(self):
        # 11: p24x9 ($32) + p24x6 ($20) = $52 beats 2x p24x9 ($64).
        self.assertEqual(plan_summary(sb.pack_potted(11, potted_cost_of)), [("p24x6", 1), ("p24x9", 10)])

    def test_never_exceeds_capacity(self):
        for n in (1, 3, 7, 15, 44):
            plan = sb.pack_potted(n, potted_cost_of)
            self.assertIsNotNone(plan, n)
            self.assertEqual(sum(pb.count for pb in plan), n)
            for pb in plan:
                self.assertLessEqual(pb.count, sb.POTTED_BOXES[pb.box_id]["capacity"])

    def test_negative_fails_safe(self):
        self.assertIsNone(sb.pack_potted(-1, potted_cost_of))

    def test_non_integer_fails_safe(self):
        self.assertIsNone(sb.pack_potted(1.5, potted_cost_of))

    def test_none_count_fails_safe(self):
        self.assertIsNone(sb.pack_potted(None, potted_cost_of))

    def test_unrated_boxes_fail_safe(self):
        self.assertIsNone(sb.pack_potted(3, lambda b: None))

    def test_partially_rated_still_packs(self):
        # Only the small box rated: 7 units -> two p24x6 (5 + 2).
        only_small = lambda b: 20.0 if b == "p24x6" else None  # noqa: E731
        self.assertEqual(plan_summary(sb.pack_potted(7, only_small)), [("p24x6", 2), ("p24x6", 5)])


if __name__ == "__main__":
    unittest.main()
