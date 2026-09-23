"""Listing-content gate: completeness rule, hard publish gate, serializer.

Section A + serializer half of E of the 2026-09-21 listing-content-gate spec
(GOL-2382). DB tests — Odoo runner only.
"""

from odoo.addons.grove_headless.controllers.main import _serialize_facts
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

# A fully-complete gated plant: all 12 required facts, storefront description,
# an approved care guide and the facts-reviewed sign-off.
_COMPLETE_VALS = {
    "grove_botanical_name": "Malus domestica",
    "grove_zone_min": 4,
    "grove_zone_max": 8,
    "grove_layer": "canopy",
    "grove_sun": "full",
    "grove_mature_size": "15–20 ft",
    "grove_mature_spread": "12–15 ft",
    "grove_spacing": "15 ft",
    "grove_soil": "well-drained loam",
    "grove_pollination": "Needs a second variety",
    "grove_years_to_fruit": "3–5 years",
    "grove_chill_hours": "800–1000",
    "description_ecommerce": "<p>A hardy heirloom apple for the food forest.</p>",
    "website_description": "<p>Plant in full sun; water weekly the first year.</p>",
    "grove_guide_ready": True,
    "grove_facts_reviewed": True,
}


@tagged("post_install", "-at_install")
class TestListingGate(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.supplies = self.env.ref("grove_headless.categ_supplies")

    def _plant(self, published=False, **overrides):
        vals = {
            "name": "Test Apple",
            "type": "consu",
            "categ_id": self.trees.id,
            "website_published": published,
        }
        vals.update(overrides)
        return self.env["product.template"].create(vals)

    def _complete_plant(self, published=False, **overrides):
        vals = dict(_COMPLETE_VALS)
        vals.update(overrides)
        return self._plant(published=published, **vals)

    # ── Completeness rule ───────────────────────────────────────────────

    def test_complete_plant_is_complete_with_no_missing(self):
        plant = self._complete_plant()
        self.assertTrue(plant.grove_listing_complete)
        self.assertFalse(plant.grove_listing_missing)

    def test_missing_names_every_unmet_item(self):
        plant = self._plant()  # nothing filled
        missing = plant.grove_listing_missing
        for label in (
            "Botanical Name",
            "USDA Zone Min",
            "Mature Spread",
            "Plant Spacing",
            "Chill Hours",
            "Description",
            "Care guide approval",
            "Facts reviewed",
        ):
            self.assertIn(label, missing)
        self.assertFalse(plant.grove_listing_complete)

    def test_zone_zero_counts_as_unset(self):
        # Integers count as set only when > 0.
        plant = self._complete_plant(grove_zone_min=0)
        self.assertIn("USDA Zone Min", plant.grove_listing_missing)
        self.assertFalse(plant.grove_listing_complete)

    def test_whitespace_char_counts_as_unset(self):
        plant = self._complete_plant(grove_spacing="   ")
        self.assertIn("Plant Spacing", plant.grove_listing_missing)

    def test_blank_html_description_counts_as_unset(self):
        # Odoo's "empty" rich-text markup must not count as filled.
        plant = self._complete_plant(description_ecommerce="<p><br></p>")
        self.assertIn("Description", plant.grove_listing_missing)

    def test_guide_needs_body_and_approval(self):
        # Both a non-blank body AND the approval flag are required.
        plant = self._complete_plant(grove_guide_ready=False)
        self.assertIn("Care guide approval", plant.grove_listing_missing)
        plant.grove_guide_ready = True
        plant.website_description = "<p><br></p>"
        self.assertIn("Care guide approval", plant.grove_listing_missing)

    def test_reading_missing_does_not_write_stored_gate(self):
        """Reading the non-stored banner must not recompute/write the stored gate.

        grove_listing_complete (stored, the hard publish gate) and
        grove_listing_missing (non-stored banner text) once shared one compute
        method, so *reading* the banner rewrote the stored flag as a side effect
        (GOL-2471). They now have distinct computes; prove the read is
        side-effect-free by poking the stored column to a value the compute would
        NOT produce, reading only the banner, then confirming the column is
        untouched on flush.
        """
        plant = self._complete_plant()
        plant.flush_recordset()
        self.assertTrue(plant.grove_listing_complete)

        # Set the stored flag to a value the compute would never produce for this
        # complete plant, straight in the DB so no recompute is scheduled.
        self.env.cr.execute(
            "UPDATE product_template SET grove_listing_complete = FALSE WHERE id = %s",
            (plant.id,),
        )
        plant.invalidate_recordset()

        # Touch ONLY the non-stored banner. Under the old shared compute this
        # recomputed grove_listing_complete=True and flushed it back.
        self.assertFalse(plant.grove_listing_missing)
        plant.flush_recordset()

        plant.invalidate_recordset()
        self.env.cr.execute(
            "SELECT grove_listing_complete FROM product_template WHERE id = %s",
            (plant.id,),
        )
        self.assertFalse(
            self.env.cr.fetchone()[0],
            "reading grove_listing_missing recomputed and rewrote the stored gate",
        )

    def test_stored_gate_still_computes_on_fact_change(self):
        """The split must not weaken the gate: it still recomputes on its deps."""
        plant = self._complete_plant()
        self.assertTrue(plant.grove_listing_complete)
        plant.grove_facts_reviewed = False
        self.assertFalse(plant.grove_listing_complete)
        self.assertIn("Facts reviewed", plant.grove_listing_missing)

    # ── Publish gate ────────────────────────────────────────────────────

    def test_publish_complete_plant_succeeds(self):
        plant = self._complete_plant()
        plant.website_published = True
        self.assertTrue(plant.website_published)

    def test_publish_incomplete_raises_naming_missing(self):
        plant = self._plant()
        with self.assertRaises(UserError) as ctx:
            plant.website_published = True
        message = str(ctx.exception)
        self.assertIn("Botanical Name", message)
        self.assertIn("Care guide approval", message)
        self.assertIn("Facts reviewed", message)

    def test_create_published_incomplete_raises(self):
        with self.assertRaises(UserError):
            self._plant(published=True)

    def test_is_published_write_is_gated(self):
        """Direct is_published=True write must not bypass the listing-content gate."""
        plant = self._plant()
        with self.assertRaises(UserError):
            plant.write({"is_published": True})

    def test_exempt_plant_publishes_incomplete(self):
        plant = self._plant(grove_gate_exempt=True)
        plant.website_published = True
        self.assertTrue(plant.website_published)

    def test_non_plant_category_publishes_incomplete(self):
        plant = self._plant(categ_id=self.supplies.id)
        plant.website_published = True
        self.assertTrue(plant.website_published)

    def test_service_type_publishes_incomplete(self):
        # Only physical goods (type 'consu') are gated.
        plant = self._plant(type="service")
        plant.website_published = True
        self.assertTrue(plant.website_published)

    def test_already_published_incomplete_accepts_single_field_edit(self):
        # Publish while exempt, then drop the exemption: now a gated, published,
        # incomplete product — a single-field edit must not re-run the gate.
        plant = self._plant(grove_gate_exempt=True, published=True)
        plant.grove_gate_exempt = False
        self.assertFalse(plant.grove_listing_complete)
        plant.grove_soil = "clay loam"  # must not raise
        self.assertEqual(plant.grove_soil, "clay loam")
        self.assertTrue(plant.website_published)

    def test_unpublish_republish_rechecks(self):
        plant = self._complete_plant(published=True)
        # Make it incomplete while published (no transition, allowed).
        plant.grove_facts_reviewed = False
        plant.website_published = False
        with self.assertRaises(UserError):
            plant.website_published = True

    # ── Facts-reviewed reset on machine writes ──────────────────────────

    def test_human_fact_write_keeps_reviewed(self):
        plant = self._complete_plant()
        self.assertTrue(plant.grove_facts_reviewed)
        plant.write({"grove_chill_hours": "500–600"})
        self.assertTrue(plant.grove_facts_reviewed)

    def test_machine_fact_write_clears_reviewed(self):
        plant = self._complete_plant()
        plant.write(
            {
                "grove_soil": "sandy loam",
                "grove_facts_provenance": {
                    "grove_soil": {"source": "perenual", "ref": "1234", "at": "2026-09-21T00:00:00Z"}
                },
            }
        )
        self.assertFalse(plant.grove_facts_reviewed)

    def test_machine_write_can_set_reviewed_explicitly(self):
        # An explicit grove_facts_reviewed in the same vals wins over the reset.
        plant = self._complete_plant(grove_facts_reviewed=False)
        plant.write(
            {
                "grove_soil": "sandy loam",
                "grove_facts_provenance": {"grove_soil": {"source": "human", "ref": "", "at": "x"}},
                "grove_facts_reviewed": True,
            }
        )
        self.assertTrue(plant.grove_facts_reviewed)

    # ── Serializer (section E half) ─────────────────────────────────────

    def test_serialize_facts_has_new_keys(self):
        plant = self._complete_plant(grove_growth_rate="fast", grove_watering="moderate")
        facts = _serialize_facts(plant)
        for key in (
            "growth_rate",
            "bloom_season",
            "harvest_season",
            "watering",
            "wildlife",
            "mature_spread",
            "chill_hours",
            "pollination",
            "years_to_fruit",
        ):
            self.assertIn(key, facts)
        self.assertEqual(facts["growth_rate"], "fast")
        self.assertEqual(facts["watering"], "moderate")
        self.assertEqual(facts["mature_spread"], "12–15 ft")

    def test_serialize_facts_normalizes_blanks(self):
        # Selections normalise to None (distinguishable from "not applicable"
        # chars), chars to "".
        plant = self._plant()
        facts = _serialize_facts(plant)
        self.assertIsNone(facts["growth_rate"])
        self.assertIsNone(facts["watering"])
        self.assertEqual(facts["chill_hours"], "")
        self.assertEqual(facts["pollination"], "")
        self.assertEqual(facts["wildlife"], "")
