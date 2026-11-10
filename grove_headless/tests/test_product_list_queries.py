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
