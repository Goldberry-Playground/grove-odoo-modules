"""Consultant facts, retail projections, the consult feed and backfill (GOL-3574 §B).

Covers the consult-dashboard section B on product.template: the new consultant
fields (eight-value stratum, four-value stage, nitrogen-fixer, and the typed
design facts) with tracking; the read-only projections of the retail stratum and
succession once both required facts are set, and the planner-beta editable
behaviour when they are not; the computed/stored grove_consult_ready; the
nitrogen-fixer computed default and its override; the consult feed shape
(?scope=consult: consult keys per plant + a guarded rules object); the
catalog_version bump on a consult write; the audit's consult-ready section; and a
unit case per backfill parse rule. DB tests — Odoo runner only, except the pure
backfill cases.
"""

import importlib.util
import os

from odoo.addons.grove_headless.controllers.main import (
    _planner_is_nitrogen_fixer,
    _planner_rules_for_scope,
    _serialize_consult_facts,
)
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import HttpCase, TransactionCase, tagged

# Load the backfill script's pure parse functions by file path — scripts/ is not
# an importable package, same importlib pattern the other grove_headless tests use.
_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "backfill_consult_facts.py")
_spec = importlib.util.spec_from_file_location("backfill_consult_facts", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# A fully-complete, planner-ready gated plant (mirrors test_planner_roles).
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
_READY_VALS = {
    "grove_years_to_fruit_min": 4,
    "grove_pollination_min": 2,
    "grove_harvest_months": "9,10,11",
    "grove_planner_role": "anchor",
    "grove_succession": "long",
}

# The editable consultant facts that must carry chatter tracking.
_NEW_EDITABLE_FIELDS = [
    "grove_syntropic_stratum",
    "grove_succession_stage",
    "grove_nitrogen_fixer",
    "grove_mature_height_m",
    "grove_canopy_closure_years",
    "grove_coppice_tolerant",
    "grove_pruning_response",
    "grove_root_type",
    "grove_allelopathy",
    "grove_log_suitability",
]


@tagged("grove_headless", "post_install", "-at_install")
class TestConsultFacts(GroveTaxFixtureMixin, TransactionCase):
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
    def test_new_editable_fields_track(self):
        for name in _NEW_EDITABLE_FIELDS:
            self.assertIn(name, self.Template._fields, f"{name} missing")
            self.assertTrue(self.Template._fields[name].tracking, f"{name} is not tracked")

    def test_stratum_and_stage_selection_values(self):
        stratum = dict(self.Template._fields["grove_syntropic_stratum"].selection)
        for key in (
            "emergent",
            "high_canopy",
            "low_canopy",
            "shrub",
            "herbaceous",
            "ground_cover",
            "root_tuber",
            "fungal",
        ):
            self.assertIn(key, stratum)
        stage = dict(self.Template._fields["grove_succession_stage"].selection)
        for key in ("placenta", "accumulation_1", "accumulation_2", "abundance"):
            self.assertIn(key, stage)

    # ── Projections (§B) ──────────────────────────────────────────────────
    def test_projection_active_when_both_set(self):
        # low_canopy -> understory; abundance -> long.
        tmpl = self._plant(
            ready=True,
            grove_layer="canopy",  # beta mapping would be overstory
            grove_succession="fast",  # beta value; projection should override
            grove_syntropic_stratum="low_canopy",
            grove_succession_stage="abundance",
        )
        self.assertEqual(tmpl.grove_planner_stratum, "understory")
        self.assertEqual(tmpl.grove_succession, "long")

    def test_projection_stage_mapping(self):
        cases = {"placenta": "fast", "accumulation_1": "mid", "accumulation_2": "mid", "abundance": "long"}
        for stage, word in cases.items():
            tmpl = self._plant(grove_syntropic_stratum="shrub", grove_succession_stage=stage)
            self.assertEqual(tmpl.grove_succession, word, f"{stage} -> {word}")

    def test_projection_stratum_mapping(self):
        cases = {
            "emergent": "overstory",
            "high_canopy": "overstory",
            "low_canopy": "understory",
            "shrub": "understory",
            "herbaceous": "understory",
            "ground_cover": "understory",
            "root_tuber": "understory",
            "fungal": "mushroom",
        }
        for stratum, retail in cases.items():
            tmpl = self._plant(grove_syntropic_stratum=stratum, grove_succession_stage="placenta")
            self.assertEqual(tmpl.grove_planner_stratum, retail, f"{stratum} -> {retail}")

    def test_projection_inactive_keeps_beta_behaviour(self):
        # Only the stratum set (no stage): the retail stratum stays layer-derived
        # and the succession word stays the editable beta value.
        tmpl = self._plant(ready=True, grove_layer="canopy", grove_succession="mid", grove_syntropic_stratum="emergent")
        self.assertEqual(tmpl.grove_planner_stratum, "overstory")  # from layer, not emergent
        self.assertEqual(tmpl.grove_succession, "mid")  # manual value persists
        # Now set the stage too: the projection takes over both.
        tmpl.grove_succession_stage = "placenta"
        self.assertEqual(tmpl.grove_planner_stratum, "overstory")  # emergent -> overstory
        self.assertEqual(tmpl.grove_succession, "fast")  # placenta -> fast

    def test_projection_releases_when_stage_cleared(self):
        tmpl = self._plant(grove_syntropic_stratum="low_canopy", grove_succession_stage="abundance")
        self.assertEqual(tmpl.grove_succession, "long")
        tmpl.grove_succession_stage = False
        # Projection off again; the last projected value remains and is editable.
        tmpl.grove_succession = "fast"
        self.assertEqual(tmpl.grove_succession, "fast")

    # ── grove_consult_ready ───────────────────────────────────────────────
    def test_consult_ready_requires_both_facts_and_planner_ready(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        self.assertFalse(tmpl.grove_consult_ready)  # consult facts unset
        tmpl.grove_syntropic_stratum = "high_canopy"
        self.assertFalse(tmpl.grove_consult_ready)  # stage still unset
        tmpl.grove_succession_stage = "abundance"
        self.assertTrue(tmpl.grove_consult_ready)
        # Lose planner-readiness -> consult-ready drops too.
        tmpl.grove_planner_role = False
        self.assertFalse(tmpl.grove_planner_ready)
        self.assertFalse(tmpl.grove_consult_ready)

    # ── nitrogen fixer (computed default, editable) ───────────────────────
    def test_nitrogen_fixer_default_from_prose_and_override(self):
        fixer = self._plant(grove_soil="a nitrogen-fixing cover", grove_wildlife="Bees")
        self.assertTrue(fixer.grove_nitrogen_fixer)
        self.assertTrue(_planner_is_nitrogen_fixer(fixer))
        plain = self._plant(grove_soil="well-drained loam", grove_wildlife="Birds")
        self.assertFalse(plain.grove_nitrogen_fixer)
        # A manual override persists until the prose changes.
        plain.grove_nitrogen_fixer = True
        self.assertTrue(plain.grove_nitrogen_fixer)
        plain.grove_wildlife = "Attracts songbirds"  # still no "nitrogen" -> recompute to False
        self.assertFalse(plain.grove_nitrogen_fixer)

    # ── catalog_version (§B) ──────────────────────────────────────────────
    def test_catalog_version_bumps_on_consult_write(self):
        tmpl = self._plant()
        before = self.Template._grove_planner_catalog_version()
        tmpl.grove_pruning_response = "strong"
        self.assertEqual(self.Template._grove_planner_catalog_version(), before + 1)

    # ── consult-facts serializer (§B) ─────────────────────────────────────
    def test_serialize_consult_facts_blank_and_set(self):
        blank = self._plant(grove_layer="canopy")
        row = _serialize_consult_facts(blank)
        for key in (
            "syntropic_stratum",
            "succession_stage",
            "mature_height_m",
            "canopy_closure_years",
            "coppice_tolerant",
            "pruning_response",
            "root_type",
            "allelopathy",
            "log_suitability",
            "consult_ready",
        ):
            self.assertIn(key, row, f"missing {key}")
        self.assertIsNone(row["syntropic_stratum"])
        self.assertIsNone(row["mature_height_m"])
        self.assertIs(row["coppice_tolerant"], False)
        self.assertIs(row["consult_ready"], False)
        # nitrogen_fixer is on the base row, not duplicated here.
        self.assertNotIn("nitrogen_fixer", row)

        full = self._plant(
            ready=True,
            grove_syntropic_stratum="high_canopy",
            grove_succession_stage="abundance",
            grove_mature_height_m=12.5,
            grove_canopy_closure_years=7,
            grove_coppice_tolerant=True,
            grove_pruning_response="strong",
            grove_root_type="tap",
            grove_allelopathy="minor",
            grove_log_suitability="oak_maple",
        )
        row = _serialize_consult_facts(full)
        self.assertEqual(row["syntropic_stratum"], "high_canopy")
        self.assertEqual(row["succession_stage"], "abundance")
        self.assertEqual(row["mature_height_m"], 12.5)
        self.assertEqual(row["canopy_closure_years"], 7)
        self.assertIs(row["coppice_tolerant"], True)
        self.assertEqual(row["pruning_response"], "strong")
        self.assertEqual(row["root_type"], "tap")
        self.assertEqual(row["allelopathy"], "minor")
        self.assertEqual(row["log_suitability"], "oak_maple")
        self.assertIs(row["consult_ready"], True)

    def test_rules_for_scope_guarded(self):
        # grove.planner.rule ships in planner beta B; absent here -> {}.
        if "grove.planner.rule" not in self.env:
            self.assertEqual(_planner_rules_for_scope(self.env, "consult"), {})

    # ── audit consult-ready section ───────────────────────────────────────
    def test_audit_not_consult_ready(self):
        ready_plant = self._plant(ready=True, name="Needs consult depth", grove_botanical_name="Diospyros virginiana")
        self.assertIn(ready_plant, self.Template._grove_audit_not_consult_ready())
        self.assertEqual(ready_plant._grove_consult_missing(), ["Syntropic stratum", "Succession stage"])
        text = self.Template._grove_audit_discord_text(ready_plant, consult=True)
        self.assertIn("not consult-ready", text)
        self.assertIn("Syntropic stratum", text)
        # Fill the consult facts -> drops out of the section.
        ready_plant.write({"grove_syntropic_stratum": "high_canopy", "grove_succession_stage": "abundance"})
        self.assertNotIn(ready_plant, self.Template._grove_audit_not_consult_ready())


@tagged("grove_headless", "post_install", "-at_install")
class TestConsultFeed(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]

    def _plant(self, **overrides):
        vals = {
            "name": "Feed Consult Plant",
            "type": "consu",
            "categ_id": self.trees.id,
            "company_id": self.company.id,
        }
        vals.update(_COMPLETE_VALS)
        vals.update(_READY_VALS)
        vals.update(overrides)
        tmpl = self.Template.create(vals)
        tmpl.website_published = True
        return tmpl

    def test_retail_feed_has_no_consult_keys(self):
        plant = self._plant(grove_botanical_name="Diospyros virginiana")
        body = self.url_open("/grove/api/v1/planner/catalog").json()
        self.assertNotIn("rules", body)
        self.assertNotIn("scope", body)
        row = next(p for p in body["plants"] if p["id"] == plant.id)
        self.assertNotIn("syntropic_stratum", row)
        # nitrogen_fixer stays on the base row in both scopes.
        self.assertIn("nitrogen_fixer", row)

    def test_consult_feed_merges_facts_and_rules(self):
        plant = self._plant(
            grove_botanical_name="Diospyros virginiana",
            grove_syntropic_stratum="high_canopy",
            grove_succession_stage="abundance",
            grove_pruning_response="moderate",
        )
        body = self.url_open("/grove/api/v1/planner/catalog?scope=consult").json()
        self.assertEqual(body["scope"], "consult")
        self.assertIn("rules", body)
        self.assertIsInstance(body["rules"], dict)
        row = next(p for p in body["plants"] if p["id"] == plant.id)
        self.assertEqual(row["syntropic_stratum"], "high_canopy")
        self.assertEqual(row["succession_stage"], "abundance")
        self.assertEqual(row["pruning_response"], "moderate")
        self.assertTrue(row["consult_ready"])
        # The projection shows through the base row too.
        self.assertEqual(row["stratum"], "overstory")
        self.assertEqual(row["succession"], "long")


@tagged("grove_headless", "post_install", "-at_install")
class TestBackfillConsultFactsRules(TransactionCase):
    """Unit cases for the pure parse rules in scripts/backfill_consult_facts.py."""

    def test_parse_stratum_canopy_splits_on_role(self):
        self.assertEqual(backfill.parse_syntropic_stratum("canopy", "anchor"), "high_canopy")
        self.assertEqual(backfill.parse_syntropic_stratum("canopy", "companion"), "low_canopy")
        self.assertEqual(backfill.parse_syntropic_stratum("canopy", False), "low_canopy")

    def test_parse_stratum_fixed_layers(self):
        self.assertEqual(backfill.parse_syntropic_stratum("understory", "anchor"), "low_canopy")
        self.assertEqual(backfill.parse_syntropic_stratum("shrub", False), "shrub")
        self.assertEqual(backfill.parse_syntropic_stratum("vine", False), "herbaceous")
        self.assertEqual(backfill.parse_syntropic_stratum("ground", False), "ground_cover")
        self.assertEqual(backfill.parse_syntropic_stratum("fungal", False), "fungal")

    def test_parse_stratum_unset_or_unknown(self):
        self.assertIs(backfill.parse_syntropic_stratum(False, False), False)
        self.assertIs(backfill.parse_syntropic_stratum("mystery", "anchor"), False)

    def test_parse_consult_facts_shape(self):
        self.assertEqual(
            backfill.parse_consult_facts({"grove_layer": "canopy", "grove_planner_role": "anchor"}),
            {"grove_syntropic_stratum": "high_canopy"},
        )
        # No proposal when the layer maps to nothing.
        self.assertEqual(backfill.parse_consult_facts({"grove_layer": False}), {})

    def test_changed_vals_is_idempotent(self):
        parsed = {"grove_syntropic_stratum": "shrub"}
        self.assertEqual(backfill.changed_vals({"grove_syntropic_stratum": False}, parsed), parsed)
        self.assertEqual(backfill.changed_vals({"grove_syntropic_stratum": "shrub"}, parsed), {})
