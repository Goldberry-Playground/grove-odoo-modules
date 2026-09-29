"""Pure tests for the GOL-2589 bundle-BoM seeder planning core.

``scripts/seed_bundle_boms.py`` attaches phantom BoMs to the *existing* prod
bundle products. The XML-RPC I/O runs on prod (not here); what these tests pin
is the pure planning core that decides how many kits are buildable and whether
seeding a BoM would flip a currently-sellable bundle to sold out — the guard
the issue makes a hard STOP. They also pin the composition table so the two
Josh-gated / custom-mix bundles can never be silently seeded.

Loaded by file path (like test_bundle_substitution.py) so pytest never drags in
the Odoo addon package.
"""

import importlib.util
import os
import sys
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "seed_bundle_boms.py")
_spec = importlib.util.spec_from_file_location("seed_bundle_boms", _PATH)
sb = importlib.util.module_from_spec(_spec)
# Register before exec: the module defines dataclasses with stringized
# annotations (PEP 563), and dataclasses resolves them via sys.modules[__module__].
sys.modules["seed_bundle_boms"] = sb
_spec.loader.exec_module(sb)


class TestKitAvailability(unittest.TestCase):
    def test_min_over_components_floor_divides(self):
        components = (sb.Component(8, 1, "Chestnut"), sb.Component(91, 2, "PawPaw"))
        # chestnut: 5//1=5, pawpaw: 5//2=2 → min is 2
        self.assertEqual(sb.kit_available_from_stock(components, {8: 5, 91: 5}), 2)

    def test_single_qty_components_are_the_scarcest(self):
        components = tuple(sb.Component(t, 1, f"c{t}") for t in (8, 4, 5, 19, 18))
        on_hand = {8: 4, 4: 10, 5: 7, 19: 100, 18: 6}
        self.assertEqual(sb.kit_available_from_stock(components, on_hand), 4)

    def test_zero_stock_component_yields_zero_kits(self):
        components = (sb.Component(8, 1, "Chestnut"), sb.Component(18, 1, "Mulberry"))
        self.assertEqual(sb.kit_available_from_stock(components, {8: 9, 18: 0}), 0)

    def test_fractional_stock_floors_not_rounds(self):
        # 3 on hand, 2 per kit → 1 kit, never 2 from a round-up.
        self.assertEqual(sb.kit_available_from_stock((sb.Component(91, 2, "PawPaw"),), {91: 3}), 1)

    def test_missing_component_stock_raises(self):
        with self.assertRaises(KeyError):
            sb.kit_available_from_stock((sb.Component(8, 1, "Chestnut"),), {})

    def test_non_positive_qty_raises(self):
        with self.assertRaises(ValueError):
            sb.kit_available_from_stock((sb.Component(8, 0, "Chestnut"),), {8: 5})

    def test_no_components_is_zero(self):
        self.assertEqual(sb.kit_available_from_stock((), {}), 0)


class TestFlipGuard(unittest.TestCase):
    def test_sellable_bundle_going_to_zero_flips(self):
        # 22 Remembrance has 4 on hand today; if components can't build a kit, STOP.
        self.assertTrue(sb.would_flip_to_soldout(current_sellable=4, kit_available=0))

    def test_still_buildable_does_not_flip(self):
        self.assertFalse(sb.would_flip_to_soldout(current_sellable=4, kit_available=3))

    def test_already_soldout_bundle_does_not_flip(self):
        # Nothing to protect if it wasn't sellable to begin with.
        self.assertFalse(sb.would_flip_to_soldout(current_sellable=0, kit_available=0))


class TestBundleTable(unittest.TestCase):
    def _by_id(self, tid):
        return next(b for b in sb.BUNDLES if b.template_id == tid)

    def test_dry_run_is_the_default(self):
        # The module was imported with no DRY_RUN env set → dry run ON.
        self.assertTrue(sb.DRY_RUN)

    def test_remembrance_is_ready_with_five_single_qty_components(self):
        b = self._by_id(22)
        self.assertEqual(b.status, sb.READY)
        self.assertEqual({c.template_id for c in b.components}, {8, 4, 5, 19, 18})
        self.assertTrue(all(c.qty == 1 for c in b.components))

    def test_mountain_mama_needs_input_and_is_not_ready(self):
        b = self._by_id(132)
        self.assertEqual(b.status, sb.NEEDS_INPUT)
        self.assertNotEqual(b.status, sb.READY)
        self.assertTrue(b.note, "NEEDS_INPUT bundle must document what Josh decides")

    def test_custom_mix_bundles_are_not_ready(self):
        for tid in (133, 134, 135):
            b = self._by_id(tid)
            self.assertEqual(b.status, sb.CUSTOM_MIX)
            self.assertTrue(b.note)

    def test_only_ready_bundles_carry_a_full_seedable_composition(self):
        # Guard: nothing but READY bundles should ever reach the create path.
        ready = [b for b in sb.BUNDLES if b.status == sb.READY]
        self.assertEqual([b.template_id for b in ready], [22])


if __name__ == "__main__":
    unittest.main()
