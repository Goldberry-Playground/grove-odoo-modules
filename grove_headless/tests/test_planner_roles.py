"""Syntropic roles, strata, succession and the catalog version (GOL-3548, §A).

Covers the planner winter-beta section A on product.template: the new editable
fields (role, succession, living mulch, substrate, review flag) with tracking;
the computed/stored stratum and its fixed layer mapping; the extended
grove_planner_ready rule (role + succession always, substrate when fungal) and
the audit's missing-name list; the catalog_version bump on a planner write; the
planner-plant serializer's new keys with blank conventions and the nitrogen-fixer
text match; and a unit case per backfill parse rule. DB tests — Odoo runner only.
"""

import importlib.util
import os

from odoo.addons.grove_headless.controllers.main import (
    _planner_is_nitrogen_fixer,
    _serialize_planner_plant,
)
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import HttpCase, TransactionCase, tagged

# Load the backfill script's pure parse functions by file path — scripts/ is not
# an importable package, same importlib pattern the other grove_headless tests use.
_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "backfill_planner_roles.py")
_spec = importlib.util.spec_from_file_location("backfill_planner_roles", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# A fully-complete gated plant (mirrors test_planner_facts._COMPLETE_VALS).
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

# The scoreable + syntropic facts that lift a complete plant to planner-ready.
_READY_VALS = {
    "grove_years_to_fruit_min": 4,
    "grove_pollination_min": 2,
    "grove_harvest_months": "9,10,11",
    "grove_planner_role": "anchor",
    "grove_succession": "long",
}

_NEW_EDITABLE_FIELDS = [
    "grove_planner_role",
    "grove_succession",
    "grove_living_mulch",
    "grove_substrate",
    "grove_planner_flagged",
]


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerRoles(GroveTaxFixtureMixin, TransactionCase):
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

    def test_layer_has_fungal_value(self):
        values = dict(self.Template._fields["grove_layer"].selection)
        self.assertIn("fungal", values)
        # Existing values are untouched.
        for existing in ("canopy", "understory", "shrub", "ground", "vine"):
            self.assertIn(existing, values)

    # ── Stratum mapping (§A.1, fixed) ─────────────────────────────────────
    def test_stratum_mapping(self):
        cases = {
            "canopy": "overstory",
            "understory": "understory",
            "shrub": "understory",
            "vine": "understory",
            "ground": "understory",
            "fungal": "mushroom",
        }
        for layer, stratum in cases.items():
            tmpl = self._plant(grove_layer=layer)
            self.assertEqual(tmpl.grove_planner_stratum, stratum, f"{layer} -> {stratum}")

    def test_stratum_blank_when_layer_unset(self):
        tmpl = self.Template.create({"name": "No Layer", "type": "consu", "categ_id": self.trees.id})
        self.assertFalse(tmpl.grove_layer)
        self.assertFalse(tmpl.grove_planner_stratum)

    def test_stratum_recomputes_when_layer_changes(self):
        tmpl = self._plant(grove_layer="canopy")
        self.assertEqual(tmpl.grove_planner_stratum, "overstory")
        tmpl.grove_layer = "fungal"
        self.assertEqual(tmpl.grove_planner_stratum, "mushroom")

    # ── grove_planner_ready now also requires role + succession ───────────
    def test_ready_requires_role(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        tmpl.grove_planner_role = False
        self.assertFalse(tmpl.grove_planner_ready)
        tmpl.grove_planner_role = "companion"
        self.assertTrue(tmpl.grove_planner_ready)

    def test_ready_requires_succession(self):
        tmpl = self._plant(ready=True)
        self.assertTrue(tmpl.grove_planner_ready)
        tmpl.grove_succession = False
        self.assertFalse(tmpl.grove_planner_ready)
        tmpl.grove_succession = "mid"
        self.assertTrue(tmpl.grove_planner_ready)

    def test_substrate_required_only_for_fungal(self):
        # A fungal (spawn) plant needs a substrate line to be ready.
        spawn = self._plant(ready=True, grove_layer="fungal")
        self.assertFalse(spawn.grove_planner_ready)
        self.assertIn("Substrate", spawn._grove_planner_missing())
        spawn.grove_substrate = "oak or maple logs from your thinnings"
        self.assertTrue(spawn.grove_planner_ready)
        # A non-fungal plant with no substrate is still ready.
        tree = self._plant(ready=True, grove_layer="canopy")
        self.assertFalse(tree.grove_substrate)
        self.assertTrue(tree.grove_planner_ready)

    def test_audit_lists_new_missing_names(self):
        # Complete + scoreable facts set, but role and succession unset.
        tmpl = self._plant(
            grove_years_to_fruit_min=4,
            grove_pollination_min=2,
            grove_harvest_months="9,10,11",
        )
        self.assertTrue(tmpl.grove_listing_complete)
        self.assertFalse(tmpl.grove_planner_ready)
        missing = tmpl._grove_planner_missing()
        self.assertEqual(missing, ["Planner role", "Succession"])

    # ── catalog_version (§A.3) ────────────────────────────────────────────
    def test_catalog_version_bumps_on_planner_write(self):
        tmpl = self._plant()
        before = self.Template._grove_planner_catalog_version()
        tmpl.grove_planner_role = "anchor"
        self.assertEqual(self.Template._grove_planner_catalog_version(), before + 1)

    def test_catalog_version_not_bumped_by_unrelated_write(self):
        tmpl = self._plant()
        before = self.Template._grove_planner_catalog_version()
        tmpl.grove_seo_title = "Heirloom apple"
        self.assertEqual(self.Template._grove_planner_catalog_version(), before)

    def test_catalog_version_bumps_on_create_with_planner_field(self):
        before = self.Template._grove_planner_catalog_version()
        self._plant()  # _COMPLETE_VALS carries grove_layer/grove_zone_* (versioned)
        self.assertGreater(self.Template._grove_planner_catalog_version(), before)

    # ── Serializer keys + blank conventions (§A.3) ────────────────────────
    def test_serializer_syntropy_keys_blank(self):
        tmpl = self._plant(grove_layer="canopy")  # role/succession/substrate unset
        row = _serialize_planner_plant(tmpl, {}, has_stock_field=False)
        for key in ("role", "succession", "stratum", "living_mulch", "substrate", "flagged", "nitrogen_fixer"):
            self.assertIn(key, row, f"serializer missing {key}")
        self.assertIsNone(row["role"])
        self.assertIsNone(row["succession"])
        self.assertEqual(row["stratum"], "overstory")  # from layer, even when role unset
        self.assertIs(row["living_mulch"], False)
        self.assertIsNone(row["substrate"])
        self.assertIs(row["flagged"], False)
        self.assertIs(row["nitrogen_fixer"], False)

    def test_serializer_syntropy_keys_set(self):
        tmpl = self._plant(
            ready=True,
            grove_layer="fungal",
            grove_planner_role="companion",
            grove_succession="fast",
            grove_living_mulch=True,
            grove_substrate="oak logs",
            grove_planner_flagged=True,
        )
        row = _serialize_planner_plant(tmpl, {}, has_stock_field=False)
        self.assertEqual(row["role"], "companion")
        self.assertEqual(row["succession"], "fast")
        self.assertEqual(row["stratum"], "mushroom")
        self.assertIs(row["living_mulch"], True)
        self.assertEqual(row["substrate"], "oak logs")
        self.assertIs(row["flagged"], True)

    def test_nitrogen_fixer_text_match(self):
        from_wildlife = self._plant(grove_wildlife="A nitrogen fixer that feeds the soil")
        self.assertTrue(_planner_is_nitrogen_fixer(from_wildlife))
        self.assertTrue(_serialize_planner_plant(from_wildlife, {}, has_stock_field=False)["nitrogen_fixer"])
        from_soil = self._plant(grove_soil="fixes nitrogen in poor ground", grove_wildlife="Bees")
        self.assertTrue(_planner_is_nitrogen_fixer(from_soil))
        neither = self._plant(grove_wildlife="Bees and birds", grove_soil="well-drained loam")
        self.assertFalse(_planner_is_nitrogen_fixer(neither))


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerRolesFeed(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]

    def _plant(self, **overrides):
        vals = {
            "name": "Feed Role Plant",
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

    def test_feed_carries_catalog_version_and_syntropy_keys(self):
        plant = self._plant(name="Overstory Anchor", grove_botanical_name="Diospyros virginiana")
        body = self.url_open("/grove/api/v1/planner/catalog").json()
        self.assertIn("catalog_version", body)
        self.assertIsInstance(body["catalog_version"], int)
        row = next(p for p in body["plants"] if p["id"] == plant.id)
        self.assertEqual(row["role"], "anchor")
        self.assertEqual(row["succession"], "long")
        self.assertEqual(row["stratum"], "overstory")


@tagged("grove_headless", "post_install", "-at_install")
class TestBackfillPlannerRolesRules(TransactionCase):
    """Unit cases for the pure parse rules in scripts/backfill_planner_roles.py."""

    def test_parse_role_canopy_is_anchor(self):
        self.assertEqual(backfill.parse_role("canopy", 1, "Apple"), "anchor")
        self.assertEqual(backfill.parse_role("canopy", None, "Apple"), "anchor")

    def test_parse_role_understory_anchor_by_years(self):
        self.assertEqual(backfill.parse_role("understory", 3, "Pawpaw"), "anchor")
        self.assertEqual(backfill.parse_role("understory", 2, "Currant"), "companion")
        self.assertEqual(backfill.parse_role("understory", None, "Currant"), "companion")

    def test_parse_role_other_layers_are_companion(self):
        for layer in ("shrub", "vine", "ground", None):
            self.assertEqual(backfill.parse_role(layer, 10, "Clover"), "companion")

    def test_parse_role_spawn(self):
        self.assertEqual(backfill.parse_role("fungal", 0, "Wine Cap Spawn"), "anchor")
        self.assertEqual(backfill.parse_role("fungal", 0, "Shiitake Plug Spawn"), "companion")

    def test_parse_succession(self):
        self.assertEqual(backfill.parse_succession("canopy", 1), "fast")
        self.assertEqual(backfill.parse_succession("canopy", 2), "fast")
        self.assertEqual(backfill.parse_succession("canopy", 5), "long")
        self.assertEqual(backfill.parse_succession("understory", 5), "mid")
        self.assertEqual(backfill.parse_succession("canopy", 3), "mid")
        self.assertEqual(backfill.parse_succession("fungal", None), "fast")

    def test_parse_living_mulch(self):
        self.assertTrue(backfill.parse_living_mulch("ground", "", "clover fixes nitrogen"))
        self.assertTrue(backfill.parse_living_mulch("ground", "ground cover for pollinators", ""))
        self.assertFalse(backfill.parse_living_mulch("ground", "just a pretty flower", "loam"))
        # Only ground-layer plants are living mulch, even with a mulch word.
        self.assertFalse(backfill.parse_living_mulch("canopy", "comfrey", "mulch"))

    def test_parse_planner_roles_shape(self):
        rec = {"name": "Chinese Chestnut", "grove_layer": "canopy", "grove_years_to_fruit_min": 5}
        vals = backfill.parse_planner_roles(rec)
        self.assertEqual(vals, {"grove_planner_role": "anchor", "grove_succession": "long"})
        # A ground living mulch adds the flag.
        rec2 = {
            "name": "Dutch Clover",
            "grove_layer": "ground",
            "grove_years_to_fruit_min": 1,
            "grove_soil": "nitrogen-fixing cover crop",
        }
        self.assertEqual(backfill.parse_planner_roles(rec2)["grove_living_mulch"], True)

    def test_changed_vals_is_idempotent(self):
        parsed = {"grove_planner_role": "companion", "grove_succession": "mid"}
        # First run: record holds unset (False) values -> everything changes.
        fresh = {"grove_planner_role": False, "grove_succession": False}
        self.assertEqual(backfill.changed_vals(fresh, parsed), parsed)
        # Second run: record already holds the parsed values -> nothing changes.
        done = {"grove_planner_role": "companion", "grove_succession": "mid"}
        self.assertEqual(backfill.changed_vals(done, parsed), {})

    def test_changed_vals_living_mulch_boolean(self):
        parsed = {"grove_planner_role": "companion", "grove_succession": "fast", "grove_living_mulch": True}
        rec = {"grove_planner_role": "companion", "grove_succession": "fast", "grove_living_mulch": False}
        self.assertEqual(backfill.changed_vals(rec, parsed), {"grove_living_mulch": True})
