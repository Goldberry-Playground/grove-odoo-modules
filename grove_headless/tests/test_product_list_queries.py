"""The product list must cost the same number of queries for 2 cards or 8.

Prod (2026-11-03) showed ``/grove/api/v1/products?limit=200`` as the slow tail of
the ``odoo-products-latency-warning`` p95: each card ran its own ``read()``,
which pulled ``image_128`` from ``ir.attachment`` and the filestore one product at
a time, only to throw the bytes away. These tests pin the batched shape.
"""

import base64
import io
import json

from odoo.tests.common import HttpCase, get_db_name, tagged

from .common import GroveTaxFixtureMixin


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (60, 120, 70)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


@tagged("grove_headless", "post_install", "-at_install")
class TestProductListQueries(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.image = _png()

    def _add_products(self, n, start):
        return self.env["product.template"].create(
            [
                {
                    "name": f"Query Count Tree {start + i:02d}",
                    "website_published": True,
                    "sale_ok": True,
                    "list_price": 20.0,
                    "company_id": self.company.id,
                    "image_1920": self.image,
                }
                for i in range(n)
            ]
        )

    def _get(self):
        headers = {"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"}
        resp = self.url_open("/grove/api/v1/products?limit=200", headers=headers)
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)

    def _queries(self):
        self._get()  # warm ormcaches so only the per-request work is counted
        before = self.cr.sql_log_count
        body = self._get()
        return self.cr.sql_log_count - before, body

    def test_query_count_does_not_grow_with_page_size(self):
        self._add_products(2, 0)
        small, body = self._queries()
        self._add_products(6, 2)
        large, body = self._queries()
        self.assertEqual(len([p for p in body["results"] if p["name"].startswith("Query Count Tree")]), 8)
        # Equal, not "close": any per-card query is an N+1 that scales with the grid.
        self.assertEqual(large, small, f"2 cards cost {small} queries but 8 cards cost {large}")

    def _variant_template(self, name, cultivars):
        """A card with a Cultivar x Bareroot/Potted variant grid and a shared pool.

        Mirrors the apple/pear shape prod serves: each Bareroot variant's live
        stock signal reads its potted sibling's pool. ``_list_in_stock`` ran that
        lookup per variant (GOL-3933), so this is the fixture that exercises the
        per-variant work the batched map removes.
        """
        A = self.env["product.attribute"]
        V = self.env["product.attribute.value"]
        cultivar = A.create({"name": f"{name} Cultivar", "create_variant": "always"})
        fmt = A.create({"name": f"{name} Format", "create_variant": "always"})
        formats = V.create([{"name": n, "attribute_id": fmt.id} for n in ("Potted", "Bareroot")])
        values = V.create([{"name": f"{name} {i:02d}", "attribute_id": cultivar.id} for i in range(cultivars)])
        return self.env["product.template"].create(
            {
                "name": name,
                "type": "consu",
                "website_published": True,
                "sale_ok": True,
                "list_price": 35.0,
                "company_id": self.company.id,
                "grove_shipping_tier": "bareroot",
                "image_1920": self.image,
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": cultivar.id, "value_ids": [(6, 0, values.ids)]}),
                    (0, 0, {"attribute_id": fmt.id, "value_ids": [(6, 0, formats.ids)]}),
                ],
            }
        )

    def test_query_count_does_not_grow_with_variants_per_card(self):
        # GOL-3933: the slow tail of the card loop on prod was per-variant work
        # (the shared-pool stock scan, the cultivar/attribute reads), not per
        # card. Two few-variant cards vs two many-variant cards must cost the
        # same request queries, not one-per-extra-variant.
        self._variant_template("List Pear", 1)  # 2 variants each
        self._variant_template("List Plum", 1)
        few, _ = self._queries()
        self._variant_template("List Apple", 10)  # 20 variants each
        self._variant_template("List Cherry", 10)
        many, body = self._queries()
        self.assertEqual(len([p for p in body["results"] if p["name"].startswith("List ")]), 4)
        # EXTRA_QUERY_ALLOWANCE mirrors the detail query test: a per-variant read
        # adds at least one query per extra variant (36 here), so 2 leaves room
        # for ORM noise only.
        self.assertLessEqual(
            many,
            few + 2,
            f"few-variant page cost {few} queries, many-variant page cost {many}",
        )

    def test_image_fields_keep_their_shape(self):
        with_image = self._add_products(1, 0)
        without_image = self.env["product.template"].create(
            {"name": "Query Count Bare", "website_published": True, "company_id": self.company.id}
        )
        cards = {p["id"]: p for p in self._get()["results"]}
        card = cards[with_image.id]
        self.assertEqual(card["image_url"], f"/web/image/product.template/{with_image.id}/image_128")
        # The list never ships image bytes (or a bin_size string like "1.2 Kb").
        self.assertIsNone(card["image_128"])
        bare = cards[without_image.id]
        self.assertIsNone(bare["image_url"])
        self.assertFalse(bare["image_128"])
