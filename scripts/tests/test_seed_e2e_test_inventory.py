"""Pure tests for the QA E2E inventory fixture seed (no Odoo, no network).

Guards the two things GOL-2463 turned on and that a silent regression would make
invisible until a Train-#2 e2e run went red for the wrong reason:

  * the ``plant`` fixture really is Plants-categorised, gated, and sorts AFTER
    the two ``AAA …`` fixtures — while the original two stay UNcategorised, so
    every existing deterministic gate cart keeps earning zero volume tiers; and
  * the listing-gate content payload covers every field the publish gate
    requires, and the reconcile helpers converge (no perpetual drift, no
    clobbering a hand-improved fixture).
"""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_e2e_test_inventory.py")
_spec = importlib.util.spec_from_file_location("seed_e2e_test_inventory", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _fixture(key):
    return next(f for f in mod.FIXTURES if f["key"] == key)


class TestFixtureSpecs(unittest.TestCase):
    def test_plant_fixture_exists_and_is_plants_categorised(self):
        plant = _fixture("plant")
        self.assertTrue(plant["categ_xmlid"].startswith("grove_headless.categ_"))
        self.assertTrue(plant["gated"], "a Plants-categorised template is subject to the publish gate")

    def test_original_fixtures_stay_uncategorised(self):
        # The whole point of GOL-2463: 797/205 must NOT become plants, or every
        # existing 5+ qty gate cart silently picks up a 10-20% volume discount.
        for key in ("potted", "bareroot"):
            with self.subTest(key=key):
                self.assertIsNone(_fixture(key)["categ_xmlid"])
                self.assertFalse(_fixture(key)["gated"])

    def test_plant_fixture_sorts_last_of_the_three(self):
        # /shop is `name asc` and findProductByCta takes the first enabled
        # "Add to Cart" it meets — the new fixture must not displace them.
        plant = _fixture("plant")["name"]
        for key in ("potted", "bareroot"):
            with self.subTest(key=key):
                self.assertGreater(plant, _fixture(key)["name"])

    def test_every_fixture_name_matches_the_grove_sites_exclusion_prefix(self):
        # grove-sites apps/nursery/e2e/qa-helpers.ts:
        #     export const E2E_FIXTURE_NAME_RE = /^AAA QA E2E /;
        # catalogCards() and qa-photos.spec.ts drop exactly that prefix from the
        # catalog set. These fixtures are seeded with NO photo, so a name outside
        # the prefix lands in the catalog set and turns the photo gate red on a
        # "Photo coming soon" placeholder.
        for fixture in mod.FIXTURES:
            with self.subTest(key=fixture["key"]):
                self.assertTrue(fixture["name"].startswith("AAA QA E2E "), fixture["name"])

    def test_plant_fixture_is_shippable_with_a_box_size(self):
        plant = _fixture("plant")
        self.assertEqual(plant["shipping_tier"], "bareroot")
        self.assertIn(plant["tree_length"], {"16", "20", "32", "46"})

    def test_plant_fixture_is_stocked_deeper_than_the_others(self):
        # Each volume-tier run buys 5-10 units; a drained free_qty turns the cart
        # into a deposit cart and the tier assertions go false-red.
        self.assertGreater(_fixture("plant")["qty"], _fixture("bareroot")["qty"])

    def test_skus_are_distinct(self):
        skus = [f["sku"] for f in mod.FIXTURES]
        self.assertEqual(len(set(skus)), len(skus))


class TestListingGateContent(unittest.TestCase):
    def test_covers_every_required_growing_fact(self):
        content = mod.listing_gate_content("X")
        for field in mod.REQUIRED_LISTING_FACTS:
            with self.subTest(field=field):
                self.assertIn(field, content)

    def test_required_facts_are_non_blank(self):
        content = mod.listing_gate_content("X")
        for field in mod.REQUIRED_LISTING_FACTS:
            value = content[field]
            with self.subTest(field=field):
                if isinstance(value, int) and not isinstance(value, bool):
                    self.assertGreater(value, 0)
                else:
                    self.assertTrue(str(value).strip())

    def test_covers_description_guide_and_signoff(self):
        content = mod.listing_gate_content("X")
        self.assertFalse(mod._html_is_blank(content["description_ecommerce"]))
        self.assertFalse(mod._html_is_blank(content["website_description"]))
        self.assertTrue(content["grove_guide_ready"])
        self.assertTrue(content["grove_facts_reviewed"])

    def test_never_stamps_provenance(self):
        # A write that stamps grove_facts_provenance next to a fact is treated as
        # a machine enrichment write and CLEARS grove_facts_reviewed in the same
        # write, which would leave the fixture un-publishable on the next run.
        self.assertNotIn("grove_facts_provenance", mod.listing_gate_content("X"))


class TestWantDrift(unittest.TestCase):
    def test_categ_id_read_back_as_a_pair_is_not_drift(self):
        cur = {"categ_id": [32, "Plants / Trees"], "name": "n"}
        self.assertEqual(mod.want_drift(cur, {"categ_id": 32, "name": "n"}), {})

    def test_wrong_category_is_drift(self):
        cur = {"categ_id": [1, "All"]}
        self.assertEqual(mod.want_drift(cur, {"categ_id": 32}), {"categ_id": 32})

    def test_unset_category_is_drift(self):
        self.assertEqual(mod.want_drift({"categ_id": False}, {"categ_id": 32}), {"categ_id": 32})

    def test_plain_scalar_drift_still_reported(self):
        self.assertEqual(mod.want_drift({"is_published": False}, {"is_published": True}), {"is_published": True})


class TestMissingGateContent(unittest.TestCase):
    def test_converged_fixture_needs_nothing(self):
        content = mod.listing_gate_content("X")
        self.assertEqual(mod.missing_gate_content(dict(content), content), {})

    def test_gap_fills_only_unset_fields(self):
        content = mod.listing_gate_content("X")
        cur = dict(content, grove_soil="", grove_zone_min=0, grove_facts_reviewed=False)
        missing = mod.missing_gate_content(cur, content)
        self.assertEqual(set(missing), {"grove_soil", "grove_zone_min", "grove_facts_reviewed"})

    def test_hand_edited_content_is_not_clobbered(self):
        content = mod.listing_gate_content("X")
        cur = dict(content, grove_soil="Josh's better soil note")
        self.assertNotIn("grove_soil", mod.missing_gate_content(cur, content))

    def test_odoo_empty_html_counts_as_unset(self):
        content = mod.listing_gate_content("X")
        cur = dict(content, description_ecommerce="<p><br></p>")
        self.assertIn("description_ecommerce", mod.missing_gate_content(cur, content))


if __name__ == "__main__":
    unittest.main()
