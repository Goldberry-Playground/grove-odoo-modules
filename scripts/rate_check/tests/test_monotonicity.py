import importlib.util
import os
import unittest

_MONO = os.path.join(os.path.dirname(__file__), "..", "monotonicity.py")
_spec = importlib.util.spec_from_file_location("monotonicity", _MONO)
mono = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mono)

_SZ = os.path.join(os.path.dirname(__file__), "..", "..", "..", "grove_headless", "models", "shipping_zones.py")
_szspec = importlib.util.spec_from_file_location("grove_shipping_zones", _SZ)
sz = importlib.util.module_from_spec(_szspec)
_szspec.loader.exec_module(sz)
sb = sz.shipping_boxes

# A minimal 2-box x 2-zone sound table (small lighter than large; zone_1 nearer
# than zone_2). Enough to exercise both monotonicity axes.
BOXES = ["small", "large"]
ZONES = ["zone_1", "zone_2"]


def _table(z1_light, z1_heavy, z2_light, z2_heavy):
    return {
        "zone_1": {"small": {"base": z1_light}, "large": {"base": z1_heavy}},
        "zone_2": {"small": {"base": z2_light}, "large": {"base": z2_heavy}},
    }


class TestFindViolations(unittest.TestCase):
    def test_sound_table_has_no_violations(self):
        self.assertEqual(mono.find_violations(_table(18, 22, 19, 23), BOXES, ZONES), [])

    def test_heavier_box_cheaper_flagged(self):
        # large (heavier) costs less than small in zone_1.
        v = mono.find_violations(_table(22, 18, 23, 24), BOXES, ZONES)
        self.assertTrue(any("box order" in x and "zone_1" in x for x in v), v)

    def test_cross_zone_cheaper_is_allowed(self):
        # Cross-zone monotonicity is intentionally NOT enforced (GOL-1495):
        # real UPS Ground doesn't order our state bands by cost, and worst-case
        # reference ZIPs guarantee no undercharge. A "farther" zone quoting
        # cheaper (here small costs less in zone_2 than zone_1) is not a
        # violation, as long as box monotonicity holds within each zone.
        self.assertEqual(mono.find_violations(_table(20, 22, 18, 23), BOXES, ZONES), [])

    def test_missing_cell_is_a_coverage_violation(self):
        table = _table(18, 22, 19, 23)
        del table["zone_2"]["large"]
        v = mono.find_violations(table, BOXES, ZONES)
        self.assertTrue(any("coverage" in x and "large" in x for x in v), v)
        # A coverage gap is not also double-reported as a "cheaper" finding.
        self.assertFalse(any("order" in x for x in v), v)

    def test_equal_rates_are_allowed(self):
        # Monotone means non-decreasing: ties are fine on both axes.
        self.assertEqual(mono.find_violations(_table(20, 20, 20, 20), BOXES, ZONES), [])

    def test_accepts_bare_number_cells(self):
        # rate_check builds its proposed table as {zone: {box: int}}.
        table = {"zone_1": {"small": 18, "large": 22}, "zone_2": {"small": 19, "large": 23}}
        self.assertEqual(mono.find_violations(table, BOXES, ZONES), [])


class TestOrderedBoxes(unittest.TestCase):
    def test_orders_by_representative_billable_weight(self):
        order = mono.ordered_boxes(sb.BOXES, sb.representative_billable_lb)
        weights = [sb.representative_billable_lb(b) for b in order]
        self.assertEqual(weights, sorted(weights))
        self.assertEqual(set(order), set(sb.BOXES))


class TestLiveTable(unittest.TestCase):
    """The committed rate table (SoR) must be sound — this is the guard that
    runs in CI on every PR that touches shipping_rates.json."""

    def test_committed_table_is_monotone_and_complete(self):
        feed = sz.rate_feed()
        table = feed["zones"]
        zones = [z for z in sz.RATE_ZONE_IDS if z in table]
        boxes = mono.ordered_boxes(sb.BOXES, sb.representative_billable_lb)
        # 2 boxes x 7 zones — descoped bareroot catalog (CEO 2026-09-07) across
        # zone_1..5 + GOL-2238 real probe-derived zone_6 / zone_7.
        self.assertEqual(len(boxes), 2)
        self.assertEqual(len(zones), 7)
        self.assertEqual(mono.find_violations(table, boxes, zones), [])


if __name__ == "__main__":
    unittest.main()
