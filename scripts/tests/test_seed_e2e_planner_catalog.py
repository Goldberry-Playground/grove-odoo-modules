"""Pure tests for the QA planner-catalog seed (no Odoo, no network).

The seed's whole job is that every fixture computes ``grove_planner_ready`` the
moment it lands — otherwise ``/api/planner/catalog`` stays ``plants: 0`` and the
nursery planner e2e specs keep skipping (GOL-3999). That readiness is silent when
broken: a fixture missing one gate field produces a green seed and a feed that is
still empty hours later. So this re-derives the model's gate
(``grove_headless/models/product_template.py``) over each fixture's vals and
asserts readiness here, plus the set-level invariants (strata coverage, unique
SKUs, in-zone for WV zone-6).
"""

import importlib.util
import os
import re
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_e2e_planner_catalog.py")
_spec = importlib.util.spec_from_file_location("seed_e2e_planner_catalog", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_COMPANY_ID = 9  # At The Grove Nursery on QA; arbitrary for a pure vals test.

# Mirror of the model's required-fact / planner-core / dimension / wildlife gate
# so the test fails if a fixture would NOT compute planner-ready.
_REQUIRED_FACTS = [
    ("grove_botanical_name", "char"),
    ("grove_zone_min", "int"),
    ("grove_zone_max", "int"),
    ("grove_layer", "char"),
    ("grove_sun", "char"),
    ("grove_mature_size", "char"),
    ("grove_mature_spread", "char"),
    ("grove_spacing", "char"),
    ("grove_soil", "char"),
    ("grove_pollination", "char"),
    ("grove_years_to_fruit", "char"),
    ("grove_chill_hours", "char"),
]
_PLANNER_CORE = [
    "grove_years_to_fruit_min",
    "grove_pollination_min",
    "grove_harvest_months",
    "grove_planner_role",
    "grove_succession",
]
_DIMENSIONS = ("grove_mature_size", "grove_mature_spread", "grove_spacing")
_UNITLESS_RE = re.compile(r"^[\d\s.,;:/×x*+'\"()–—-]+$")
_LAYERS = {"canopy", "understory", "shrub", "ground", "vine", "fungal"}
_ROLES = {"anchor", "companion"}
_SUCCESSION = {"fast", "mid", "long"}


def _vals(spec):
    return mod.planner_fixture_vals(spec, _COMPANY_ID)


class TestFixtureSet(unittest.TestCase):
    def test_six_fixtures_unique_skus(self):
        skus = [f["sku"] for f in mod.FIXTURES]
        self.assertEqual(len(mod.FIXTURES), 6)
        self.assertEqual(len(set(skus)), len(skus))

    def test_spans_the_food_forest_strata(self):
        layers = {f["layer"] for f in mod.FIXTURES}
        # A plantable open-ground food forest needs the canopy-to-ground span plus
        # a vine; without diversity the planner cannot build a layered plan.
        self.assertTrue({"canopy", "understory", "shrub", "ground", "vine"} <= layers)

    def test_covers_both_roles_and_all_succession_bands(self):
        self.assertEqual({f["role"] for f in mod.FIXTURES}, _ROLES)
        self.assertEqual({f["succession"] for f in mod.FIXTURES}, _SUCCESSION)

    def test_valid_selection_values(self):
        for f in mod.FIXTURES:
            self.assertIn(f["layer"], _LAYERS, f["sku"])
            self.assertIn(f["role"], _ROLES, f["sku"])
            self.assertIn(f["succession"], _SUCCESSION, f["sku"])

    def test_all_in_zone_for_wv_zone_6(self):
        for f in mod.FIXTURES:
            self.assertLessEqual(f["zone_min"], 6, f["sku"])
            self.assertGreaterEqual(f["zone_max"], 6, f["sku"])


class TestFixtureIsPlannerReady(unittest.TestCase):
    """Re-derive the model gate over each fixture's vals; every one must pass."""

    def setUp(self):
        self.all_vals = [(f["sku"], _vals(f)) for f in mod.FIXTURES]

    def test_company_and_feed_selectors(self):
        for sku, v in self.all_vals:
            self.assertEqual(v["company_id"], _COMPANY_ID, sku)
            self.assertTrue(v["sale_ok"], sku)
            self.assertTrue(v["is_published"], sku)
            # grove_shipping_tier must be "bareroot": != "seed" (feed seed filter)
            # AND not potted-only, so the retail feed keeps the plant year-round
            # instead of dropping it outside the leafed window (GOL-3800).
            self.assertEqual(v.get("grove_shipping_tier"), "bareroot", sku)

    def test_stockable_and_priced(self):
        # GOL-4003: the planner engine builds only from in-stock plants, so each
        # fixture must be inventory-tracked (is_storable, so the caller's quant
        # gives it real on-hand) and carry a non-$0 price (so the planner carts a
        # priced line, not a $0 one).
        for sku, v in self.all_vals:
            self.assertTrue(v.get("is_storable"), sku)
            self.assertGreater(v.get("list_price") or 0, 0, sku)

    def test_required_growing_facts_all_set(self):
        for sku, v in self.all_vals:
            for name, kind in _REQUIRED_FACTS:
                val = v.get(name)
                if kind == "int":
                    self.assertTrue(bool(val) and val > 0, f"{sku}:{name}")
                else:
                    self.assertTrue(str(val or "").strip(), f"{sku}:{name}")

    def test_content_and_signoffs_present(self):
        for sku, v in self.all_vals:
            self.assertTrue(str(v.get("description_ecommerce") or "").strip(), sku)
            self.assertTrue(str(v.get("website_description") or "").strip(), sku)
            self.assertTrue(v.get("grove_guide_ready"), sku)
            self.assertTrue(v.get("grove_facts_reviewed"), sku)

    def test_planner_core_facts_set(self):
        for sku, v in self.all_vals:
            self.assertGreater(v["grove_years_to_fruit_min"], 0, sku)
            self.assertGreater(v["grove_pollination_min"], 0, sku)
            self.assertTrue(v["grove_harvest_months"].strip(), sku)
            self.assertIn(v["grove_planner_role"], _ROLES, sku)
            self.assertIn(v["grove_succession"], _SUCCESSION, sku)
            self.assertFalse(v.get("grove_consult_built"), sku)

    def test_dimensions_carry_a_unit(self):
        # _grove_format_warnings flags a bare-number dimension; such a warning
        # drops grove_listing_complete and the fixture would never be ready.
        for sku, v in self.all_vals:
            for name in _DIMENSIONS:
                text = (v[name] or "").strip()
                self.assertFalse(_UNITLESS_RE.match(text), f"{sku}:{name}={text!r} has no unit")

    def test_wildlife_is_not_junk(self):
        # _wildlife_is_junk flags < 3 words; keep Wildlife specific so no warning.
        for sku, v in self.all_vals:
            self.assertGreaterEqual(len(v["grove_wildlife"].split()), 3, sku)

    def test_no_double_spaces_in_spec_text(self):
        for sku, v in self.all_vals:
            for name in ("grove_wildlife", "grove_harvest_season", *_DIMENSIONS):
                self.assertNotIn("  ", v[name] or "", f"{sku}:{name}")


if __name__ == "__main__":
    unittest.main()
