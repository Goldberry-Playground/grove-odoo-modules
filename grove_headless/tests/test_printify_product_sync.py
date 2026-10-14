"""Odoo-runtime tests for the Printify product sync (spec 2026-10-13-printify-merch-design.md §3)."""

import base64
import os
from unittest import mock

from odoo.addons.grove_headless.models import printify_client
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin

RAW = {
    "id": "6650abc",
    "title": "Grove Tee",
    "description": "<p>Soft tee.</p>",
    "images": [{"src": "https://img/2.png", "is_default": True}],
    "options": [
        {"type": "color", "values": [{"id": 521, "title": "Natural"}, {"id": 522, "title": "Sage"}]},
        {"type": "size", "values": [{"id": 14, "title": "M"}, {"id": 18, "title": "2XL"}]},
    ],
    "variants": [
        {"id": 11001, "price": 2800, "cost": 1250, "is_enabled": True, "options": [521, 14]},
        {"id": 11002, "price": 3100, "cost": 1500, "is_enabled": True, "options": [521, 18]},
        {"id": 11003, "price": 2800, "cost": 1250, "is_enabled": True, "options": [522, 14]},
        {"id": 11004, "price": 3100, "cost": 1500, "is_enabled": False, "options": [522, 18]},
    ],
}
ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifySync(GroveTaxFixtureMixin, TransactionCase):
    def _sync(self, raws=None):
        fetch = mock.Mock(return_value=mock.Mock(status_code=200, content=PNG))
        with (
            mock.patch.dict(os.environ, ENV, clear=False),
            mock.patch.object(printify_client, "list_products", return_value=raws if raws is not None else [RAW]),
        ):
            return self.env["product.template"]._grove_printify_sync(fetch=fetch)

    def _tmpl(self):
        return (
            self.env["product.template"]
            .with_context(active_test=False)
            .search([("printify_product_id", "=", "6650abc")])
        )

    def test_first_sync_creates_unpublished_farm_template(self):
        result = self._sync()
        tmpl = self._tmpl()
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.grove_fulfillment_route, "printify")
        self.assertFalse(tmpl.website_published)
        self.assertTrue(tmpl.grove_gate_exempt)
        self.assertFalse(tmpl.is_storable)
        self.assertEqual(tmpl.company_id, self.env.ref("base.main_company"))
        self.assertEqual(tmpl.list_price, 28.0)
        self.assertTrue(tmpl.image_1920)

    def test_variants_mapped_and_disabled_archived(self):
        self._sync()
        variants = self._tmpl().with_context(active_test=False).product_variant_ids
        by_id = {v.printify_variant_id: v for v in variants if v.printify_variant_id}
        self.assertEqual(set(by_id), {11001, 11002, 11003})
        self.assertEqual(by_id[11002].lst_price, 31.0)
        self.assertEqual(by_id[11001].standard_price, 12.5)
        unmapped = variants.filtered(lambda v: not v.printify_variant_id)
        self.assertTrue(unmapped, "Sage/2XL exists as an Odoo combination")
        self.assertFalse(any(unmapped.mapped("active")), "a disabled Printify combination is archived")

    def test_second_sync_is_idempotent_and_keeps_odoo_price(self):
        self._sync()
        tmpl = self._tmpl()
        tmpl.list_price = 30.0
        count = self.env["product.product"].with_context(active_test=False).search_count([])
        result = self._sync()
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.env["product.product"].with_context(active_test=False).search_count([]), count)
        self.assertEqual(self._tmpl().list_price, 30.0)

    def test_variant_disabled_later_is_archived_not_deleted(self):
        self._sync()
        raw = dict(RAW, variants=[dict(v, is_enabled=(v["id"] != 11003)) for v in RAW["variants"]])
        result = self._sync([raw])
        variant = (
            self.env["product.product"].with_context(active_test=False).search([("printify_variant_id", "=", 11003)])
        )
        self.assertEqual(len(variant), 1)
        self.assertFalse(variant.active)
        self.assertEqual(result["archived"], 1)

    def test_later_added_size_is_priced_above_base(self):
        # First sync WITHOUT the 2XL variants, then a sync that adds them: the new
        # size must price above base (its Printify upcharge), never at base and
        # possibly below cost (GOL-3314 review 2026-10-14). The operator's edited
        # base list_price still wins.
        first = dict(RAW, variants=[v for v in RAW["variants"] if v["id"] in (11001, 11003)])
        self._sync([first])
        self._tmpl().list_price = 30.0  # operator sets the base after first sync
        self._sync([RAW])
        by_id = {
            v.printify_variant_id: v
            for v in self._tmpl().with_context(active_test=False).product_variant_ids
            if v.printify_variant_id
        }
        # Printify base is $28 (min enabled); 2XL is $31 => +$3 above the base.
        self.assertEqual(by_id[11002].lst_price, 33.0)
        self.assertEqual(by_id[11001].lst_price, 30.0)

    def test_preexisting_no_variant_size_attribute_does_not_starve_variants(self):
        # A pre-existing "Size" attribute that does NOT spin up variants must not
        # leave the shirt with no variants (GOL-3314 review 2026-10-14).
        self.env["product.attribute"].create({"name": "Size", "create_variant": "no_variant"})
        self._sync()
        mapped = {
            v.printify_variant_id
            for v in self._tmpl().with_context(active_test=False).product_variant_ids
            if v.printify_variant_id
        }
        self.assertEqual(mapped, {11001, 11002, 11003})

    def test_missing_config_raises(self):
        from odoo.exceptions import UserError

        with mock.patch.dict(os.environ, {"PRINTIFY_API_TOKEN": "", "PRINTIFY_SHOP_ID": ""}, clear=False):
            with self.assertRaises(UserError):
                self.env["product.template"]._grove_printify_sync()

    def test_order_is_printify_compute(self):
        self._sync()
        variant = self.env["product.product"].search([("printify_variant_id", "=", 11001)])
        order = self.env["sale.order"].create(
            {
                "partner_id": self.env.ref("base.partner_admin").id,
                "order_line": [(0, 0, {"product_id": variant.id, "product_uom_qty": 1})],
            }
        )
        self.assertTrue(order.grove_is_printify)
