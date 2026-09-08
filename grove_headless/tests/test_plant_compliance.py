"""Tests for the per-product genus/species compliance carve-out engine.

``models/plant_compliance.py`` is pure Python (no Odoo imports), so these are
plain ``unittest`` cases with no DB — they run under both Odoo's ``--test-enable``
runner and standalone pytest. The module is loaded by file path so importing it
never drags in the Odoo addon package.

The carve-out map is compliance-critical: a wrong entry either leaks a
restricted plant into a state that prohibits it, or blocks a legal sale. These
tests pin every ratified rule (NPB Oct-2025) and the two behaviours that are
easy to regress: species-level resolution (Morus alba blocked, Morus rubra
clean) and the empty-botanical fail-safe (blocks only into regulated states).
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "plant_compliance.py")
_spec = importlib.util.spec_from_file_location("grove_plant_compliance", _MODULE_PATH)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


class TestParseTaxon(unittest.TestCase):
    def test_genus_species(self):
        self.assertEqual(pc.parse_taxon("Castanea mollissima"), ("castanea", "mollissima"))

    def test_cultivar_is_ignored(self):
        self.assertEqual(pc.parse_taxon("Morus alba 'Maple Leaf'"), ("morus", "alba"))

    def test_bare_genus(self):
        self.assertEqual(pc.parse_taxon("Cornus"), ("cornus", None))

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(pc.parse_taxon("  PRUNUS   PERSICA  "), ("prunus", "persica"))

    def test_empty_is_unparseable(self):
        for value in ("", "   ", None):
            self.assertIsNone(pc.parse_taxon(value))

    def test_non_alpha_first_token_is_unparseable(self):
        self.assertIsNone(pc.parse_taxon("123 mystery"))


class TestTaxonBlocked(unittest.TestCase):
    def test_castanea_blocks_its_states(self):
        for state in ("WA", "OR", "FL"):
            self.assertTrue(pc.is_taxon_blocked("castanea", "dentata", state), state)

    def test_castanea_clears_a_green_state(self):
        self.assertFalse(pc.is_taxon_blocked("castanea", "dentata", "GA"))

    def test_prunus_blocks_wa_or_only(self):
        self.assertTrue(pc.is_taxon_blocked("prunus", "americana", "WA"))
        self.assertTrue(pc.is_taxon_blocked("prunus", "americana", "OR"))
        self.assertFalse(pc.is_taxon_blocked("prunus", "americana", "FL"))

    def test_cornus_blocks_fl(self):
        self.assertTrue(pc.is_taxon_blocked("cornus", "florida", "FL"))
        self.assertFalse(pc.is_taxon_blocked("cornus", "florida", "WA"))

    def test_carya_blocks_az_nm(self):
        self.assertTrue(pc.is_taxon_blocked("carya", "ovata", "AZ"))
        self.assertTrue(pc.is_taxon_blocked("carya", "ovata", "NM"))

    def test_morus_alba_blocked_but_rubra_clean(self):
        # Species-level resolution: white mulberry restricted, red mulberry not.
        for state in ("IN", "OH", "WI"):
            self.assertTrue(pc.is_taxon_blocked("morus", "alba", state), state)
            self.assertFalse(pc.is_taxon_blocked("morus", "rubra", state), state)
        # A bare "morus" with no species must NOT match the alba-only rule.
        self.assertFalse(pc.is_taxon_blocked("morus", None, "IN"))

    def test_diospyros_allow_only_ca(self):
        self.assertFalse(pc.is_taxon_blocked("diospyros", "virginiana", "CA"))
        for state in ("GA", "TN", "FL", "OH"):
            self.assertTrue(pc.is_taxon_blocked("diospyros", "virginiana", state), state)

    def test_unrestricted_genus_never_blocks(self):
        self.assertFalse(pc.is_taxon_blocked("malus", "domestica", "FL"))


class TestRegulatedStates(unittest.TestCase):
    def test_regulated_states_is_union_of_all_rules(self):
        self.assertEqual(pc.REGULATED_STATES, frozenset({"WA", "OR", "FL", "AZ", "NM", "IN", "OH", "WI", "CA"}))


class TestEvaluateLine(unittest.TestCase):
    def test_blocked_line_returns_message_not_failsafe(self):
        msg, failsafe = pc.evaluate_line("Castanea mollissima", "FL", "Florida")
        self.assertIsNotNone(msg)
        self.assertFalse(failsafe)
        self.assertIn("Florida", msg)
        self.assertIn("Castanea mollissima", msg)

    def test_clean_line_ships(self):
        self.assertEqual(pc.evaluate_line("Malus domestica", "GA", "Georgia"), (None, False))

    def test_morus_rubra_ships_to_indiana(self):
        self.assertEqual(pc.evaluate_line("Morus rubra", "IN", "Indiana"), (None, False))

    def test_morus_alba_blocked_indiana(self):
        msg, failsafe = pc.evaluate_line("Morus alba 'Maple Leaf'", "IN", "Indiana")
        self.assertIsNotNone(msg)
        self.assertFalse(failsafe)

    def test_empty_botanical_failsafe_blocks_regulated_state(self):
        msg, failsafe = pc.evaluate_line("", "FL", "Florida")
        self.assertIsNotNone(msg)
        self.assertTrue(failsafe)

    def test_empty_botanical_ships_to_unregulated_state(self):
        # The fail-safe must NOT break the existing catalog: an unlabeled product
        # into a non-regulated green state ships exactly as before.
        self.assertEqual(pc.evaluate_line("", "GA", "Georgia"), (None, False))

    def test_unparseable_botanical_failsafe_regulated(self):
        msg, failsafe = pc.evaluate_line("42", "OH", "Ohio")
        self.assertIsNotNone(msg)
        self.assertTrue(failsafe)

    def test_state_label_defaults_to_code(self):
        msg, _ = pc.evaluate_line("Castanea mollissima", "FL")
        self.assertIn("FL", msg)


class TestCarveOutFeed(unittest.TestCase):
    def test_feed_shape(self):
        feed = pc.carve_out_feed()
        self.assertEqual(feed["schema"], 1)
        self.assertEqual(feed["carve_outs"]["castanea"], {"kind": "block", "states": ["FL", "OR", "WA"]})
        self.assertEqual(feed["carve_outs"]["diospyros"]["kind"], "allow")
        self.assertEqual(feed["regulated_states"], sorted(pc.REGULATED_STATES))

    def test_feed_is_json_serializable(self):
        import json

        json.dumps(pc.carve_out_feed())


if __name__ == "__main__":
    unittest.main()
