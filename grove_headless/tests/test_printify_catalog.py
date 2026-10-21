"""Odoo-runtime catalog tests for the Printify farm-tenant rule (spec §4).

The product_list payload wraps items under ``results`` (not ``products``); the
``_names`` helper reads that actual shape.
"""

import json
from datetime import datetime, timedelta

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.tests import tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyCatalog(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        color = self.env["product.attribute"].create({"name": "Color", "create_variant": "always"})
        size = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        natural = self.env["product.attribute.value"].create({"attribute_id": color.id, "name": "Natural"})
        medium = self.env["product.attribute.value"].create({"attribute_id": size.id, "name": "M"})
        self.shirt = self.env["product.template"].create(
            {
                "name": "Grove Tee",
                "type": "consu",
                "is_storable": False,
                "list_price": 28.0,
                "company_id": False,
                "website_published": True,
                "grove_fulfillment_route": "printify",
                "grove_gate_exempt": True,
                "printify_lead_time": "3 to 7 business days",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": color.id, "value_ids": [(6, 0, natural.ids)]}),
                    (0, 0, {"attribute_id": size.id, "value_ids": [(6, 0, medium.ids)]}),
                ],
            }
        )
        self.shared_tree = self.env["product.template"].create(
            {
                "name": "Shared Tree",
                "type": "consu",
                "list_price": 12.0,
                "company_id": False,
                "website_published": True,
            }
        )
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-printify-catalog", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"API keys unavailable: {exc}")

    def _get(self, path, tenant):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": tenant,
            "Authorization": f"Bearer {self.api_key}",
        }
        return json.loads(self.url_open(path, headers=headers).content)

    def _names(self, body):
        return {p["name"] for p in body.get("results", [])}

    def test_farm_lists_shirt_but_not_shared_tree(self):
        names = self._names(self._get("/grove/api/v1/products", "goldberry"))
        self.assertIn("Grove Tee", names)
        self.assertNotIn("Shared Tree", names)

    def test_nursery_still_lists_shared_tree(self):
        self.assertIn("Shared Tree", self._names(self._get("/grove/api/v1/products", "nursery")))

    def test_farm_detail_refuses_shared_tree(self):
        resp = self.url_open(
            f"/grove/api/v1/products/{self.shared_tree.id}",
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        self.assertEqual(resp.status_code, 404)

    def test_flags_and_variant_axes(self):
        flags = grove_main._fulfillment_flags(self.shirt)
        self.assertEqual(flags["fulfillment_route"], "printify")
        self.assertEqual(flags["lead_time"], "3 to 7 business days")
        variant = grove_main._structure_variant(self.shirt.product_variant_ids[0])
        self.assertEqual((variant["color"], variant["size"]), ("Natural", "M"))
