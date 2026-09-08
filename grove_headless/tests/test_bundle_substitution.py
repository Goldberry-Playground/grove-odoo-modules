"""Tests for the per-state bundle component substitution engine (GOL-2237).

``models/bundle_substitution.py`` is pure Python (it only reaches into the
equally pure ``plant_compliance``), so these are plain ``unittest`` cases with
no DB — they run under both Odoo's ``--test-enable`` runner and standalone
pytest. Both modules are loaded by file path so importing never drags in the
Odoo addon package.

The substitution is compliance-critical in the other direction from the block
gate: a wrong entry either ships a restricted component into a prohibiting state
(under the "bundles ship everywhere" banner) or drops a legal component. These
tests pin the ratified Remembrance Grove behaviour — FL swaps chestnut only,
WA/OR swap chestnut + American plum — the substitute-safety invariant, and the
two easy regressions: the non-native jujube flipping ``all_native`` for WA/OR,
and the loud failure when a blocked component has no substitute.
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "bundle_substitution.py")
_spec = importlib.util.spec_from_file_location("grove_bundle_substitution", _MODULE_PATH)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


# The ratified Remembrance Grove default composition: five natives, incl. the two
# taxa that carry carve-outs (Castanea, Prunus). The other three are unrestricted
# everywhere, so they must never be swapped in any test below.
REMEMBRANCE = [
    ("Castanea dentata", "American Chestnut"),
    ("Prunus americana", "American Plum"),
    ("Asimina triloba", "Pawpaw"),
    ("Amelanchier canadensis", "Serviceberry"),
    ("Cercis canadensis", "Eastern Redbud"),
]


class TestSubstituteData(unittest.TestCase):
    def test_every_substitute_clears_the_states_it_serves(self):
        # A substitute is only valid if it itself ships to every state where the
        # taxon it replaces is blocked — otherwise the swap just moves the block.
        self.assertEqual(bs.validate_substitutes(), [])

    def test_feed_shape(self):
        feed = bs.substitution_feed()
        self.assertEqual(feed["schema"], 1)
        self.assertEqual(feed["substitutes"]["castanea"]["botanical"], "Carya ovata")
        self.assertTrue(feed["substitutes"]["castanea"]["native"])
        self.assertFalse(feed["substitutes"]["prunus"]["native"])


class TestSwapsForState(unittest.TestCase):
    def test_default_state_needs_no_swaps(self):
        # TN is green and regulates none of the five taxa.
        self.assertEqual(bs.swaps_for_state(REMEMBRANCE, "TN"), [])

    def test_florida_swaps_chestnut_only(self):
        swaps = bs.swaps_for_state(REMEMBRANCE, "FL")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].original_botanical, "Castanea dentata")
        self.assertEqual(swaps[0].substitute_botanical, "Carya ovata")
        self.assertEqual(swaps[0].substitute_label, "Shagbark Hickory")
        self.assertTrue(swaps[0].substitute_native)

    def test_washington_and_oregon_swap_chestnut_and_plum(self):
        for state in ("WA", "OR"):
            swaps = bs.swaps_for_state(REMEMBRANCE, state)
            by_original = {sw.original_botanical: sw for sw in swaps}
            self.assertEqual(set(by_original), {"Castanea dentata", "Prunus americana"}, state)
            self.assertEqual(by_original["Castanea dentata"].substitute_label, "Shagbark Hickory", state)
            self.assertEqual(by_original["Prunus americana"].substitute_botanical, "Ziziphus jujuba", state)

    def test_unparseable_component_is_left_alone(self):
        components = [("", "Mystery Filler"), ("Castanea dentata", "American Chestnut")]
        swaps = bs.swaps_for_state(components, "FL")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].original_botanical, "Castanea dentata")

    def test_blocked_component_without_substitute_fails_loud(self):
        # Cornus (dogwood) is blocked into FL but no substitute is defined — the
        # "ships everywhere" promise can't be kept, so we fail loud, not silently.
        with self.assertRaises(ValueError):
            bs.swaps_for_state([("Cornus florida", "Flowering Dogwood")], "FL")


class TestEffectiveComposition(unittest.TestCase):
    def test_default_composition_is_untouched_and_all_native(self):
        comp = bs.effective_composition(REMEMBRANCE, "TN")
        self.assertEqual([c["botanical"] for c in comp["components"]], [b for b, _ in REMEMBRANCE])
        self.assertFalse(any(c["substituted"] for c in comp["components"]))
        self.assertTrue(comp["all_native"])

    def test_florida_swaps_chestnut_keeps_plum_stays_native(self):
        comp = bs.effective_composition(REMEMBRANCE, "FL")
        by_botanical = {c["botanical"]: c for c in comp["components"]}
        self.assertIn("Carya ovata", by_botanical)
        self.assertNotIn("Castanea dentata", by_botanical)
        self.assertIn("Prunus americana", by_botanical)  # plum kept — Prunus clears FL
        self.assertTrue(by_botanical["Carya ovata"]["substituted"])
        self.assertEqual(by_botanical["Carya ovata"]["replaces_label"], "American Chestnut")
        self.assertTrue(comp["all_native"])  # hickory is native, so the claim holds

    def test_oregon_swaps_both_and_drops_native_claim(self):
        comp = bs.effective_composition(REMEMBRANCE, "OR")
        botanicals = {c["botanical"] for c in comp["components"]}
        self.assertIn("Carya ovata", botanicals)
        self.assertIn("Ziziphus jujuba", botanicals)
        self.assertNotIn("Castanea dentata", botanicals)
        self.assertNotIn("Prunus americana", botanicals)
        self.assertFalse(comp["all_native"])  # jujube isn't NA-native -> soften copy


class TestPackingSlipNote(unittest.TestCase):
    def test_no_swaps_is_empty(self):
        self.assertEqual(bs.packing_slip_note([], "Tennessee"), "")

    def test_note_names_substitute_and_original(self):
        swaps = bs.swaps_for_state(REMEMBRANCE, "OR")
        note = bs.packing_slip_note(swaps, "Oregon")
        self.assertIn("Oregon", note)
        self.assertIn("Shagbark Hickory", note)
        self.assertIn("IN PLACE OF", note)
        self.assertIn("American Chestnut", note)
        self.assertIn("Jujube", note)


if __name__ == "__main__":
    unittest.main()
