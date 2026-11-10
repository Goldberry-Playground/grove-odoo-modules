"""The product detail must not cost a query per variant.

Prod (2026-11-10) served ``/grove/api/v1/products/3`` (apple, 84 variants) in
0.8-0.9s while few-variant products took 0.3-0.4s. A local profile of an
apple-shaped template (42 cultivars x Potted/Bareroot, a Rootstock metadata line)
put the detail at 206 queries against 42 for two variants: each variant's
``tree_count`` ran its own phantom-BoM search (two queries a variant), and each
Bareroot variant's shared-pool lookup rescanned every sibling in Python. These
tests pin the batched shape.
"""

import json

from odoo.tests.common import HttpCase, get_db_name, tagged

from .common import GroveTaxFixtureMixin

# Queries a larger template may cost over a 2-variant one of the same shape.
# The batched detail measured equal counts; a per-variant read adds at least
# one query per extra variant (18 here), so 2 leaves room for ORM noise only.
EXTRA_QUERY_ALLOWANCE = 2


@tagged("grove_headless", "post_install", "-at_install")
class TestProductDetailQueries(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        A = self.env["product.attribute"]
        V = self.env["product.attribute.value"]
        self.cultivar = A.create({"name": "Cultivar", "create_variant": "always"})
        self.fmt = A.create({"name": "Format", "create_variant": "always"})
        self.formats = V.create([{"name": n, "attribute_id": self.fmt.id} for n in ("Potted", "Bareroot")])
        self.rootstock = A.create({"name": "Rootstock", "create_variant": "no_variant"})
        self.m111 = V.create({"name": "M.111", "attribute_id": self.rootstock.id})

    def _template(self, name, cultivars):
        values = self.env["product.attribute.value"].create(
            [{"name": f"{name} {i:02d}", "attribute_id": self.cultivar.id} for i in range(cultivars)]
        )
        return self.env["product.template"].create(
            {
                "name": name,
                "type": "consu",
                "website_published": True,
                "sale_ok": True,
                "list_price": 35.0,
                "company_id": self.company.id,
                "grove_shipping_tier": "bareroot",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.cultivar.id, "value_ids": [(6, 0, values.ids)]}),
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, self.formats.ids)]}),
                    (0, 0, {"attribute_id": self.rootstock.id, "value_ids": [(6, 0, self.m111.ids)]}),
                ],
            }
        )

    def _get(self, template):
        headers = {"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"}
        resp = self.url_open(f"/grove/api/v1/products/{template.id}", headers=headers)
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)

    def _queries(self, template):
        self._get(template)  # warm ormcaches so only the per-request work is counted
        before = self.cr.sql_log_count
        body = self._get(template)
        return self.cr.sql_log_count - before, body

    def test_detail_query_count_does_not_grow_with_variants(self):
        small, body = self._queries(self._template("Detail Pear", 1))
        self.assertEqual(len(body["variants"]), 2)
        large, body = self._queries(self._template("Detail Apple", 10))
        self.assertEqual(len(body["variants"]), 20)
        self.assertLessEqual(
            large,
            small + EXTRA_QUERY_ALLOWANCE,
            f"2 variants cost {small} queries but 20 variants cost {large}",
        )
