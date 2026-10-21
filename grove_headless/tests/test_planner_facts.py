"""Planner facts on product.template (GOL-3378, order-builder spec §A).

Covers: the eight editable planner fields + the computed/stored grove_planner_ready
flag with tracking; the ready rule (listing complete + scoreable facts set, never
for bundles / consult-built); the editable grove_tolerates_shade default from
grove_sun; the serializer's nine planner keys with blank conventions; the audit's
"not planner-ready" Discord section; and a unit case per parser rule. DB tests —
Odoo runner only.
"""

import importlib.util
import os
from unittest.mock import patch

from odoo.addons.grove_headless.controllers.main import _serialize_facts
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged

# Load the backfill script's pure parse functions by file path — scripts/ is not
# an importable package, same importlib pattern the other grove_headless tests use.
_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "backfill_planner_facts.py")
_spec = importlib.util.spec_from_file_location("backfill_planner_facts", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# A fully-complete gated plant (mirrors test_listing_audit._COMPLETE_VALS). No
# planner facts set, so the listing is complete but the plant is not yet ready.
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

# The planner core facts that make a complete plant ready.
_READY_VALS = {
    "grove_years_to_fruit_min": 4,
    "grove_pollination_min": 2,
    "grove_harvest_months": "9,10,11",
    # Role and succession now also gate readiness (GOL-3548 §A).
    "grove_planner_role": "anchor",
    "grove_succession": "long",
}

_EDITABLE_PLANNER_FIELDS = [
    "grove_years_to_fruit_min",
    "grove_pollination_min",
    "grove_harvest_months",
    "grove_tolerates_wet",
    "grove_tolerates_poor_soil",
    "grove_tolerates_shade",
    "grove_wildlife_value",
    "grove_planner_priority",
]


@tagged("post_install", "-at_install")
class TestPlannerFacts(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]

    def _plant(self, published=True, ready=False, **overrides):
        vals = {"name": "Test Apple", "type": "consu", "categ_id": self.trees.id}
        vals.update(_COMPLETE_VALS)
        if ready:
            vals.update(_READY_VALS)
        vals.update(overrides)
        tmpl = self.Template.create(vals)
        if published:
            tmpl.website_published = True
        return tmpl

    # ── Fields + tracking ────────────────────────────────────────────────
    def test_all_editable_planner_fields_track(self):
        for name in _EDITABLE_PLANNER_FIELDS:
            self.assertIn(name, self.Template._fields, f"{name} missing")
            self.assertTrue(self.Template._fields[name].tracking, f"{name} is not tracked")

    def test_planner_priority_defaults_to_50(self):
        tmpl = self._plant()
        self.assertEqual(tmpl.grove_planner_priority, 50)

    # ── grove_planner_ready rule ──────────────────────────────────────────
    def test_ready_only_when_listing_complete_and_core_facts_set(self):
        # Complete listing but no planner facts: not ready.
        tmpl = self._plant(ready=False)
        self.assertTrue(tmpl.grove_listing_complete)
        self.assertFalse(tmpl.grove_planner_ready)

        # Set the scoreable facts: now ready.
        tmpl.write(_READY_VALS)
        self.assertTrue(tmpl.grove_planner_ready)

    def test_ready_recomputes_when_a_core_fact_clears(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        # Clear each core fact in turn; each flip drops readiness, restoring it lifts it back.
        for name, cleared in (
            ("grove_years_to_fruit_min", 0),
            ("grove_pollination_min", 0),
            ("grove_harvest_months", ""),
        ):
            original = tmpl[name]
            tmpl.write({name: cleared})
            self.assertFalse(tmpl.grove_planner_ready, f"still ready after clearing {name}")
            tmpl.write({name: original})
            self.assertTrue(tmpl.grove_planner_ready, f"not ready after restoring {name}")

    def test_ready_recomputes_when_listing_completeness_changes(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        # Break the listing (clear the sign-off): planner_ready must follow.
        tmpl.grove_facts_reviewed = False
        self.assertFalse(tmpl.grove_listing_complete)
        self.assertFalse(tmpl.grove_planner_ready)

    def test_tolerates_wet_boolean_does_not_block_readiness(self):
        # The 4th planner field is a Boolean with no unset sentinel, so False
        # (the default) must not keep a plant out of the ready set.
        tmpl = self._plant(ready=True)
        self.assertFalse(tmpl.grove_tolerates_wet)
        self.assertTrue(tmpl.grove_planner_ready)

    def test_consult_built_is_never_ready(self):
        tmpl = self._plant(ready=True, grove_consult_built=True)
        self.assertFalse(tmpl.grove_planner_ready)
        # Clearing the flag makes an otherwise-ready plant ready again.
        tmpl.grove_consult_built = False
        self.assertTrue(tmpl.grove_planner_ready)

    def test_template_with_a_bom_is_never_ready(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        component = self.Template.create({"name": "Mulch Bag", "type": "consu"})
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": tmpl.id,
                "product_qty": 1.0,
                "type": "phantom",
                "bom_line_ids": [(0, 0, {"product_id": component.product_variant_id.id, "product_qty": 1})],
            }
        )
        # BoM membership is not an @api.depends trigger; a dependent write forces
        # the recompute that reads _grove_has_bom().
        tmpl.write({"grove_harvest_months": "9,10"})
        self.assertFalse(tmpl.grove_planner_ready)

    # ── grove_tolerates_shade editable default ────────────────────────────
    def test_tolerates_shade_defaults_from_sun_and_stays_editable(self):
        shady = self._plant(grove_sun="partial")
        self.assertTrue(shady.grove_tolerates_shade)
        sunny = self._plant(grove_sun="full")
        self.assertFalse(sunny.grove_tolerates_shade)
        # A manual override persists while grove_sun is unchanged.
        sunny.grove_tolerates_shade = True
        self.assertTrue(sunny.grove_tolerates_shade)
        # Changing the sun re-seeds the computed default, overriding the manual value.
        shady.grove_tolerates_shade = False
        self.assertFalse(shady.grove_tolerates_shade)
        shady.grove_sun = "shade"
        self.assertTrue(shady.grove_tolerates_shade)

    # ── Serializer ────────────────────────────────────────────────────────
    def test_serializer_blank_conventions(self):
        tmpl = self._plant(ready=False)
        facts = _serialize_facts(tmpl)
        for key in (
            "years_to_fruit_min",
            "pollination_min",
            "harvest_months",
            "tolerates_wet",
            "tolerates_poor_soil",
            "tolerates_shade",
            "wildlife_value",
            "planner_priority",
            "planner_ready",
        ):
            self.assertIn(key, facts, f"serializer missing {key}")
        # Unset integers -> None, unset char -> "", booleans -> bool, selection -> None.
        self.assertIsNone(facts["years_to_fruit_min"])
        self.assertIsNone(facts["pollination_min"])
        self.assertEqual(facts["harvest_months"], "")
        self.assertIs(facts["tolerates_wet"], False)
        self.assertIsNone(facts["wildlife_value"])
        self.assertEqual(facts["planner_priority"], 50)  # default, not 0
        self.assertIs(facts["planner_ready"], False)

    def test_serializer_set_values(self):
        tmpl = self._plant(ready=True, grove_wildlife_value="high", grove_tolerates_wet=True)
        facts = _serialize_facts(tmpl)
        self.assertEqual(facts["years_to_fruit_min"], 4)
        self.assertEqual(facts["pollination_min"], 2)
        self.assertEqual(facts["harvest_months"], "9,10,11")
        self.assertIs(facts["tolerates_wet"], True)
        self.assertEqual(facts["wildlife_value"], "high")
        self.assertIs(facts["planner_ready"], True)

    # ── Audit: "not planner-ready" section ────────────────────────────────
    def test_audit_selects_published_complete_not_ready_non_bundle(self):
        gap = self._plant(ready=False, name="Gap Apple")  # complete listing, no planner facts
        self._plant(ready=True, name="Ready Apple")  # ready -> excluded
        self._plant(ready=False, name="Consult Box", grove_consult_built=True)  # bundle -> excluded
        # Listing-incomplete plant: chased by section 1, excluded from this one.
        incomplete = self._plant(ready=False, name="Incomplete Apple")
        incomplete.grove_facts_reviewed = False

        selected = self.Template._grove_audit_not_planner_ready()
        self.assertEqual(selected, gap)

    def test_audit_planner_discord_text_shape(self):
        gap = self._plant(ready=False, name="Shape Apple")
        text = self.Template._grove_audit_discord_text(gap, planner=True)
        self.assertTrue(text.startswith("1 published plant(s) not planner-ready: "))
        self.assertIn("Shape Apple — ", text)
        self.assertIn("Years to first crop (min)", text)
        self.assertIn("Harvest months", text)

    def test_cron_posts_planner_section(self):
        self._plant(ready=False, name="Cron Gap Apple")  # complete listing, not ready
        with (
            patch.dict(os.environ, {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/hook"}),
            patch("odoo.addons.grove_headless.models.product_template.requests") as mock_requests,
        ):
            self.Template.cron_audit_listing_content()
        # Only the planner section fires (no listing-incomplete plant).
        self.assertEqual(mock_requests.post.call_count, 1)
        _, kwargs = mock_requests.post.call_args
        self.assertIn("not planner-ready", kwargs["json"]["content"])
        self.assertIn("Cron Gap Apple", kwargs["json"]["content"])

    # ── Parser rules (pure unit cases) ────────────────────────────────────
    def test_parse_years_to_fruit_min(self):
        self.assertEqual(backfill.parse_years_to_fruit_min("4–6 years"), 4)
        self.assertEqual(backfill.parse_years_to_fruit_min("First nuts at 4 to 6 years"), 4)
        self.assertEqual(backfill.parse_years_to_fruit_min("Not applicable"), 0)
        self.assertEqual(backfill.parse_years_to_fruit_min(""), 0)

    def test_parse_pollination_min(self):
        self.assertEqual(backfill.parse_pollination_min("Self-fertile"), 1)
        self.assertEqual(backfill.parse_pollination_min("Self-pollinating"), 1)
        self.assertEqual(backfill.parse_pollination_min("Parthenocarpic"), 1)
        self.assertEqual(backfill.parse_pollination_min("Needs a second variety"), 2)
        self.assertEqual(backfill.parse_pollination_min("Needs a pollination partner"), 2)
        self.assertEqual(backfill.parse_pollination_min("Cross-pollination required"), 2)
        self.assertEqual(backfill.parse_pollination_min("Needs two others (2+)"), 2)
        self.assertEqual(backfill.parse_pollination_min("Plant three varieties"), 3)
        self.assertEqual(backfill.parse_pollination_min("3+ needed"), 3)
        # "three" wins over "partner" when both appear.
        self.assertEqual(backfill.parse_pollination_min("needs three partners"), 3)
        self.assertEqual(backfill.parse_pollination_min("unknown"), 0)

    def test_parse_harvest_months(self):
        self.assertEqual(backfill.parse_harvest_months("Summer"), "7,8")
        self.assertEqual(backfill.parse_harvest_months("Summer–Fall"), "8,9,10")
        self.assertEqual(backfill.parse_harvest_months("Fall"), "9,10,11")
        self.assertEqual(backfill.parse_harvest_months("Late fall"), "10,11,12")
        self.assertEqual(backfill.parse_harvest_months("Spring"), "4,5,6")
        self.assertEqual(backfill.parse_harvest_months("Winter"), "")
        # Month numbers already in the text win over the table.
        self.assertEqual(backfill.parse_harvest_months("Sept–Nov (9,10,11)"), "9,10,11")

    def test_parse_tolerates_wet(self):
        self.assertTrue(backfill.parse_tolerates_wet("Moist bottomland soil", "moderate"))
        self.assertTrue(backfill.parse_tolerates_wet("rich loam", "high"))  # high watering
        self.assertFalse(backfill.parse_tolerates_wet("well-drained loam", "moderate"))

    def test_parse_tolerates_poor_soil(self):
        self.assertTrue(backfill.parse_tolerates_poor_soil("Tolerates rocky, clay ground"))
        self.assertTrue(backfill.parse_tolerates_poor_soil("drought tolerant"))
        self.assertFalse(backfill.parse_tolerates_poor_soil("rich bottomland loam"))

    def test_parse_tolerates_shade(self):
        self.assertTrue(backfill.parse_tolerates_shade("partial"))
        self.assertTrue(backfill.parse_tolerates_shade("shade"))
        self.assertFalse(backfill.parse_tolerates_shade("full"))
        self.assertFalse(backfill.parse_tolerates_shade(""))

    def test_parse_wildlife_value(self):
        self.assertEqual(backfill.parse_wildlife_value("Attracts deer and turkey"), "high")
        self.assertEqual(backfill.parse_wildlife_value("Food for birds and squirrels"), "medium")
        self.assertIsNone(backfill.parse_wildlife_value("Attracts bees"))
        self.assertIsNone(backfill.parse_wildlife_value(""))

    def test_parse_planner_facts_only_includes_set_values_and_clears_signoff(self):
        rec = {
            "grove_years_to_fruit": "4–6 years",
            "grove_pollination": "Needs a second variety",
            "grove_harvest_season": "Fall",
            "grove_soil": "Moist bottomland",
            "grove_watering": "moderate",
            "grove_sun": "partial",
            "grove_wildlife": "Deer, turkey",
        }
        vals = backfill.parse_planner_facts(rec)
        self.assertEqual(vals["grove_years_to_fruit_min"], 4)
        self.assertEqual(vals["grove_pollination_min"], 2)
        self.assertEqual(vals["grove_harvest_months"], "9,10,11")
        self.assertTrue(vals["grove_tolerates_wet"])
        self.assertTrue(vals["grove_tolerates_shade"])
        self.assertEqual(vals["grove_wildlife_value"], "high")
        # No poor-soil word -> the False tolerance is omitted, not written as False.
        self.assertNotIn("grove_tolerates_poor_soil", vals)
        # An all-blank row yields nothing to write (and so keeps its sign-off).
        self.assertEqual(backfill.parse_planner_facts({}), {})
