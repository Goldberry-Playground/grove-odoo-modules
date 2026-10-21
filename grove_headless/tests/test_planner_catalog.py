"""Planner catalog feed GET /grove/api/v1/planner/catalog (GOL-3379, §B).

Two layers, both DB tests (Odoo runner only):

* ``TestPlannerCatalogSerializer`` drives the row serializers directly, so the
  blank conventions, the carve-out ``blocked_states`` lookup (including the Morus
  alba vs rubra split), the ``grove_compliance_exempt`` empty case, the
  unparseable-botanical fail-safe and the substitute-id resolution are pinned on
  plain records without standing up a website.
* ``TestPlannerCatalogFeed`` drives the HTTP route end to end: ready/published
  filtering, the cache header, the green-list ``states``/``state_zones``, the
  in-catalog substitute resolution and the bundle shape.

The pure genus/``blocked_states`` helpers and the STATE_ZONE_BANDS coverage also
have fast, DB-free coverage in test_plant_compliance.py / test_shipping_zones.py.
"""

import json

from odoo.addons.grove_headless.controllers.main import (
    FOOD_FOREST_PACKAGES_CATEGORY,
    _serialize_planner_plant,
)
from odoo.addons.grove_headless.models.plant_compliance import REGULATED_STATES
from odoo.addons.grove_headless.models.shipping_zones import GREEN_STATES, STATE_ZONE_BANDS
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase

from .common import GroveTaxFixtureMixin

# A fully listing-complete plant (mirrors test_planner_facts._COMPLETE_VALS). No
# planner facts set, so it is complete but not yet planner-ready.
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

# The scoreable facts that lift a complete plant to planner-ready.
_READY_VALS = {
    "grove_years_to_fruit_min": 4,
    "grove_pollination_min": 2,
    "grove_harvest_months": "9,10,11",
}


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerCatalogSerializer(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]

    def _plant(self, **overrides):
        vals = {"name": "Serializer Plant", "type": "consu", "categ_id": self.trees.id, "list_price": 12.0}
        vals.update(overrides)
        return self.Template.create(vals)

    def test_blocked_states_match_carve_outs_per_genus(self):
        cases = {
            "Diospyros virginiana": ["CA"],
            "Castanea mollissima": ["FL", "OR", "WA"],
            "Morus alba 'Maple Leaf'": ["IN", "OH", "WI"],  # species-level block
            "Morus rubra": [],  # red mulberry is clean — the alba/rubra split
            "Malus domestica": [],  # unrestricted genus
        }
        for botanical, expected in cases.items():
            plant = self._plant(grove_botanical_name=botanical)
            row = _serialize_planner_plant(plant, {}, has_stock_field=False)
            self.assertEqual(row["blocked_states"], expected, botanical)
            self.assertEqual(row["genus"], botanical.split()[0].lower(), botanical)

    def test_compliance_exempt_empties_blocked_states(self):
        plant = self._plant(grove_botanical_name="Castanea mollissima", grove_compliance_exempt=True)
        row = _serialize_planner_plant(plant, {}, has_stock_field=False)
        self.assertEqual(row["blocked_states"], [])

    def test_unparseable_botanical_yields_regulated_set(self):
        for botanical in ("", "123 mystery"):
            plant = self._plant(grove_botanical_name=botanical)
            row = _serialize_planner_plant(plant, {}, has_stock_field=False)
            self.assertEqual(row["blocked_states"], sorted(REGULATED_STATES), repr(botanical))
            self.assertIsNone(row["genus"])

    def test_blank_conventions_no_false_where_none(self):
        # A plant with only a botanical name: every unset typed fact must be the
        # blank sentinel (None / [] / bool), never a stray JSON False.
        plant = self._plant(grove_botanical_name="Malus domestica")
        row = _serialize_planner_plant(plant, {}, has_stock_field=False)
        for key in (
            "zone_min",
            "zone_max",
            "wildlife_value",
            "layer",
            "years_to_fruit_min",
            "pollination_min",
            "substitute_id",
        ):
            self.assertIsNone(row[key], key)
        self.assertEqual(row["harvest_months"], [])
        for flag in ("wet", "poor_soil", "shade"):
            self.assertIsInstance(row["tolerates"][flag], bool)
        # No False anywhere a None is expected, and the row round-trips as JSON.
        for key in ("genus", "layer", "zone_min", "wildlife_value", "substitute_id"):
            self.assertIsNot(row[key], False, key)
        json.dumps(row)

    def test_harvest_months_parsed_to_int_list(self):
        plant = self._plant(grove_botanical_name="Malus domestica", grove_harvest_months="9, 10 ,11")
        row = _serialize_planner_plant(plant, {}, has_stock_field=False)
        self.assertEqual(row["harvest_months"], [9, 10, 11])

    def test_variant_id_and_price_default_to_sole_variant(self):
        # A single-variant template: the planner line adds its only variant, and
        # the variant price mirrors the (equal) template list price.
        plant = self._plant(grove_botanical_name="Malus domestica", list_price=18.5)
        row = _serialize_planner_plant(plant, {}, has_stock_field=False)
        self.assertEqual(row["variant_id"], plant.product_variant_id.id)
        self.assertEqual(row["variant_price"], 18.5)

    def test_variant_id_prefers_the_tier_matching_variant(self):
        # A bareroot-tier tree sold in Bareroot + Potted formats: the planner line
        # adds the Bareroot variant (effective tier == template tier), not the
        # pickup-only Potted sibling.
        fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        v_potted = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": fmt.id})
        v_bareroot = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": fmt.id})
        plant = self._plant(
            grove_botanical_name="Malus domestica",
            grove_shipping_tier="bareroot",
            attribute_line_ids=[(0, 0, {"attribute_id": fmt.id, "value_ids": [(6, 0, [v_potted.id, v_bareroot.id])]})],
        )
        bareroot_variant = plant.product_variant_ids.filtered(lambda v: v.grove_effective_shipping_tier == "bareroot")
        row = _serialize_planner_plant(plant, {}, has_stock_field=False)
        self.assertEqual(row["variant_id"], bareroot_variant.id)
        self.assertEqual(row["variant_price"], bareroot_variant.lst_price)

    def test_substitute_id_resolves_via_genus_map(self):
        chestnut = self._plant(grove_botanical_name="Castanea mollissima")
        row = _serialize_planner_plant(chestnut, {"carya": 4242}, has_stock_field=False)
        self.assertEqual(row["substitute_id"], 4242)
        # A genus with no defined substitute stays null even when the map is non-empty.
        persimmon = self._plant(grove_botanical_name="Diospyros virginiana")
        self.assertIsNone(_serialize_planner_plant(persimmon, {"carya": 4242}, has_stock_field=False)["substitute_id"])


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerCatalogFeed(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]

    def _plant(self, published=True, ready=False, **overrides):
        vals = {
            "name": "Feed Plant",
            "type": "consu",
            "categ_id": self.trees.id,
            "company_id": self.company.id,
        }
        vals.update(_COMPLETE_VALS)
        if ready:
            vals.update(_READY_VALS)
        vals.update(overrides)
        tmpl = self.Template.create(vals)
        tmpl.website_published = published
        return tmpl

    def _fetch(self):
        resp = self.url_open("/grove/api/v1/planner/catalog")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp

    def test_feed_excludes_non_ready_and_unpublished(self):
        ready = self._plant(ready=True, name="Ready Persimmon", grove_botanical_name="Diospyros virginiana")
        incomplete = self._plant(ready=False, name="Incomplete Pawpaw", grove_botanical_name="Asimina triloba")
        hidden = self._plant(
            ready=True, published=False, name="Hidden Chestnut", grove_botanical_name="Castanea mollissima"
        )
        ids = [p["id"] for p in self._fetch().json()["plants"]]
        self.assertIn(ready.id, ids)
        self.assertNotIn(incomplete.id, ids)
        self.assertNotIn(hidden.id, ids)
        row = next(p for p in self._fetch().json()["plants"] if p["id"] == ready.id)
        self.assertEqual(row["genus"], "diospyros")
        self.assertEqual(row["blocked_states"], ["CA"])

    def test_cache_header_present(self):
        self.assertEqual(self._fetch().headers.get("Cache-Control"), "public, max-age=300")

    def test_states_and_state_zones_cover_the_green_list(self):
        body = self._fetch().json()
        self.assertEqual(body["states"], sorted(GREEN_STATES))
        self.assertEqual(set(body["state_zones"]), set(GREEN_STATES))
        for state, band in body["state_zones"].items():
            self.assertEqual(band, STATE_ZONE_BANDS[state], state)

    def test_substitute_id_points_at_the_in_catalog_substitute(self):
        chestnut = self._plant(ready=True, name="Chinese Chestnut", grove_botanical_name="Castanea mollissima")
        hickory = self._plant(ready=True, name="Shagbark Hickory", grove_botanical_name="Carya ovata")
        row = next(p for p in self._fetch().json()["plants"] if p["id"] == chestnut.id)
        self.assertEqual(row["substitute_id"], hickory.id)

    def test_bundles_expose_components_consult_flag_and_tree_count(self):
        ff = self.env["product.public.category"].create({"name": FOOD_FOREST_PACKAGES_CATEGORY})
        comp_a = self._plant(ready=True, name="Comp Apple", grove_botanical_name="Malus domestica")
        comp_b = self._plant(ready=True, name="Comp Plum", grove_botanical_name="Prunus americana")
        bom_bundle = self.Template.create(
            {
                "name": "Mountain Mama",
                "type": "consu",
                "list_price": 50.0,
                "company_id": self.company.id,
                "public_categ_ids": [(6, 0, [ff.id])],
                "website_published": True,
            }
        )
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": bom_bundle.id,
                "product_qty": 1.0,
                "type": "phantom",
                "bom_line_ids": [
                    (0, 0, {"product_id": comp_a.product_variant_id.id, "product_qty": 2}),
                    (0, 0, {"product_id": comp_b.product_variant_id.id, "product_qty": 1}),
                ],
            }
        )
        consult_bundle = self.Template.create(
            {
                "name": "Food Forest 50",
                "type": "consu",
                "list_price": 200.0,
                "company_id": self.company.id,
                "public_categ_ids": [(6, 0, [ff.id])],
                "grove_consult_built": True,
                "website_published": True,
            }
        )
        bundles = {b["id"]: b for b in self._fetch().json()["bundles"]}
        self.assertIn(bom_bundle.id, bundles)
        self.assertIn(consult_bundle.id, bundles)
        # Neither bundle leaks into the plant list.
        plant_ids = [p["id"] for p in self._fetch().json()["plants"]]
        self.assertNotIn(bom_bundle.id, plant_ids)
        self.assertNotIn(consult_bundle.id, plant_ids)

        mm = bundles[bom_bundle.id]
        self.assertFalse(mm["consult_built"])
        # Component template ids, repeated per unit qty (2× apple, 1× plum).
        self.assertEqual(sorted(mm["component_ids"]), sorted([comp_a.id, comp_a.id, comp_b.id]))
        self.assertIn("tree_count", mm)
        # A bundle adds its default variant; price mirrors the template list price.
        self.assertEqual(mm["variant_id"], bom_bundle.product_variant_id.id)
        self.assertEqual(mm["variant_price"], 50.0)

        ff50 = bundles[consult_bundle.id]
        self.assertTrue(ff50["consult_built"])
        self.assertEqual(ff50["component_ids"], [])
        self.assertIn("tree_count", ff50)
        self.assertEqual(ff50["variant_id"], consult_bundle.product_variant_id.id)
        self.assertEqual(ff50["variant_price"], 200.0)

    def test_feed_has_no_stray_false_where_none_expected(self):
        persimmon = self._plant(ready=True, name="Serialisable Persimmon", grove_botanical_name="Diospyros virginiana")
        body = self._fetch().json()
        for plant in body["plants"]:
            for key in (
                "zone_min",
                "zone_max",
                "wildlife_value",
                "layer",
                "years_to_fruit_min",
                "pollination_min",
                "substitute_id",
                "genus",
                "tier",
                "variant_id",
                "variant_price",
            ):
                self.assertIsNot(plant[key], False, f"{plant['id']}.{key}")
            self.assertIsInstance(plant["harvest_months"], list)
        # The planner line variant is carried so the storefront BFF skips the
        # per-plant /products/<id> detail fetch (GOL-3524).
        row = next(p for p in body["plants"] if p["id"] == persimmon.id)
        self.assertEqual(row["variant_id"], persimmon.product_variant_id.id)
        self.assertEqual(row["variant_price"], persimmon.product_variant_id.lst_price)
