"""The planner catalog feed must not cost a phantom-BoM search per bundle.

``/grove/api/v1/planner/catalog`` serializes every Food Forest Package bundle.
Before GOL-3932 each bundle ran its own phantom-BoM search twice — once for the
``tree_count`` (``variant_tree_count``) and once for the ``component_ids`` — the
same per-variant N+1 gom#412 removed from product detail (206 → 39 queries). The
feed now resolves every bundle's BoM once (``_phantom_kit_boms``) and its tree
count in one batched pass (``variant_tree_counts``), then builds each row from
memory. GOL-3932 left one residual: the BoM explode itself — the count and the
component ids each called ``mrp.bom.explode``, and every explode runs one sub-kit
``_bom_find`` over its component products that top-level prefetch never satisfies
— so the feed still cost two explodes a bundle. GOL-4063 explodes each bundle
once (``explode_bundle_boms``) and both passes read the result, so each extra
bundle now costs a single explode. This test pins that shape: each extra bundle
may cost the one explode and nothing more.
"""

import json

from odoo.addons.grove_headless.controllers.main import FOOD_FOREST_PACKAGES_CATEGORY
from odoo.tests.common import HttpCase, tagged

from .common import GroveTaxFixtureMixin

# What one extra bundle may cost: the sub-kit ``_bom_find`` inside its single
# shared BoM explode (GOL-4063 collapses the count + component-id explodes into
# one). The two-explode feed cost two a bundle, and the unbatched feed four (its
# own phantom-BoM search twice on top) — this bound rejects both.
QUERIES_PER_EXTRA_BUNDLE = 1
# Room for ORM noise only (the batched BoM/line/product reads happen once for
# the whole page) — kept well under the per-bundle cost of the pre-GOL-4063 feed
# across the six extra bundles below.
EXTRA_QUERY_ALLOWANCE = 4


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerCatalogQueries(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.Template = self.env["product.template"]
        self.ff = self.env["product.public.category"].create({"name": FOOD_FOREST_PACKAGES_CATEGORY})
        # Two component plants shared by every bundle BoM so the per-bundle cost
        # under test is the phantom-BoM resolution, not distinct component reads.
        self.comp_a = self._plant("Comp Apple", "Malus domestica")
        self.comp_b = self._plant("Comp Plum", "Prunus americana")

    def _plant(self, name, botanical):
        # Left unpublished: the bundle's component ids and tree count come from
        # its BoM, not the plant list, and publishing a plant needs the full
        # listing content (the publish gate).
        return self.Template.create(
            {
                "name": name,
                "type": "consu",
                "categ_id": self.trees.id,
                "company_id": self.company.id,
                "list_price": 20.0,
                "grove_botanical_name": botanical,
            }
        )

    def _bundle(self, name):
        bundle = self.Template.create(
            {
                "name": name,
                "type": "consu",
                "list_price": 50.0,
                "company_id": self.company.id,
                "public_categ_ids": [(6, 0, [self.ff.id])],
                "website_published": True,
            }
        )
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": bundle.id,
                "product_qty": 1.0,
                "type": "phantom",
                "bom_line_ids": [
                    (0, 0, {"product_id": self.comp_a.product_variant_id.id, "product_qty": 2}),
                    (0, 0, {"product_id": self.comp_b.product_variant_id.id, "product_qty": 1}),
                ],
            }
        )
        return bundle

    def _fetch(self):
        resp = self.url_open("/grove/api/v1/planner/catalog")
        self.assertEqual(resp.status_code, 200, resp.content)
        return json.loads(resp.content)

    def _queries(self):
        self._fetch()  # warm ormcaches so only the per-request work is counted
        before = self.cr.sql_log_count
        body = self._fetch()
        return self.cr.sql_log_count - before, body

    def test_feed_query_count_grows_only_by_one_explode_per_bundle(self):
        self._bundle("Bundle 01")
        small, body = self._queries()
        self.assertEqual(len(body["bundles"]), 1)
        # Every bundle still carries the full shape built from the batched reads.
        self.assertEqual(
            sorted(body["bundles"][0]["component_ids"]), sorted([self.comp_a.id, self.comp_a.id, self.comp_b.id])
        )
        self.assertEqual(body["bundles"][0]["tree_count"], 3)

        extra_bundles = 6
        for i in range(2, 2 + extra_bundles):
            self._bundle(f"Bundle {i:02d}")
        large, body = self._queries()
        self.assertEqual(len(body["bundles"]), 1 + extra_bundles)
        self.assertLessEqual(
            large,
            small + extra_bundles * QUERIES_PER_EXTRA_BUNDLE + EXTRA_QUERY_ALLOWANCE,
            f"1 bundle cost {small} queries but 7 bundles cost {large}",
        )
