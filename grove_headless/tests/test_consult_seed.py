"""Consult seed data: the twelve goal modules and seven practices (GOL-3977,
GOL-3575 spec C.1 / C.2).

Checks the seed XML loads the full, correctly keyed set:
  * exactly the twelve module keys from spec C.1 / draft 6.6, each resolvable by
    external id and carrying the five derived fields (objective, approach,
    expected_results, section_title, section_template);
  * exactly the seven practice keys from spec C.2 / draft 6.7, each with a
    non-empty strata list and the documented invariants (silvopasture has no
    shrub stratum; forest farming and mycoforestry are shade-tolerant only, so
    no overstory-exclusive understory — mushroom/ground/understory only);
  * spacing is left unset (0.0) pending Josh's numbers; the test pins that so a
    later invented value is caught.

DB test (TransactionCase): listed in tests/__init__.py AND the root conftest
collect_ignore_glob so the pure-Python pytest collector skips it (GOL-1936).
"""

from odoo.tests import TransactionCase, tagged

MODULE_KEYS = (
    "lifecycle",
    "perennial_integration",
    "soil_and_carbon",
    "water_resilience",
    "microclimate_buffering",
    "habitat_and_pest_structuring",
    "certification_readiness",
    "monoculture_transition",
    "nutrient_cycling",
    "climate_adaptation",
    "site_remediation",
    "wildlife_and_deer",
)

PRACTICE_KEYS = (
    "homestead_food_forest",
    "alley_cropping",
    "silvopasture",
    "forest_farming",
    "mycoforestry",
    "riparian_buffer",
    "windbreak_hedgerow",
)


@tagged("post_install", "-at_install")
class TestConsultSeed(TransactionCase):
    def test_twelve_modules_are_seeded_with_the_spec_keys(self):
        modules = self.env["grove.consult.module"].search([])
        self.assertEqual(len(modules), 12)
        self.assertEqual(set(modules.mapped("key")), set(MODULE_KEYS))

    def test_each_module_resolves_by_external_id_with_derived_copy(self):
        for key in MODULE_KEYS:
            with self.subTest(module=key):
                record = self.env.ref(f"grove_headless.consult_module_{key}")
                self.assertEqual(record.key, key)
                self.assertTrue(record.name)
                # The five fields derived from the draft (GOL-3977).
                self.assertTrue(record.objective)
                self.assertTrue(record.approach)
                self.assertTrue(record.expected_results)
                self.assertTrue(record.section_title)
                self.assertTrue(record.section_template)

    def test_seven_practices_are_seeded_with_the_spec_keys(self):
        practices = self.env["grove.consult.practice"].search([])
        self.assertEqual(len(practices), 7)
        self.assertEqual(set(practices.mapped("key")), set(PRACTICE_KEYS))

    def test_each_practice_has_strata_and_retail_mapping(self):
        for key in PRACTICE_KEYS:
            with self.subTest(practice=key):
                record = self.env.ref(f"grove_headless.consult_practice_{key}")
                self.assertEqual(record.key, key)
                self.assertTrue(record.name)
                self.assertTrue(record.strata, "strata must be a non-empty list")
                self.assertIsInstance(record.strata, list)
                self.assertTrue(record.site_needs)
                self.assertTrue(record.retail_track)

    def test_shade_and_browse_invariants_from_the_draft(self):
        # Silvopasture lists no shrub stratum (draft 6.7 rule).
        silvopasture = self.env.ref("grove_headless.consult_practice_silvopasture")
        self.assertNotIn("shrub", silvopasture.strata)
        # Forest farming and mycoforestry force shade-tolerant species: no
        # sun-canopy-only emphasis — their strata stay within the shade set.
        shade_set = {"overstory", "understory", "ground", "mushroom"}
        for key in ("forest_farming", "mycoforestry"):
            record = self.env.ref(f"grove_headless.consult_practice_{key}")
            self.assertTrue(set(record.strata).issubset(shade_set))

    def test_spacing_is_unset_pending_josh_numbers(self):
        # The draft carries no spacing numbers; they are left at the model
        # default (0.0) until Josh supplies them (GOL-3977). Pin it so an
        # invented value is caught in review.
        for key in PRACTICE_KEYS:
            with self.subTest(practice=key):
                record = self.env.ref(f"grove_headless.consult_practice_{key}")
                self.assertEqual(record.row_spacing_m, 0.0)
                self.assertEqual(record.in_row_spacing_m, 0.0)
