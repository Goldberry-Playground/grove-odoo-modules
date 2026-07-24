"""Detail serializer: facts block + structured variants (catalog spec)."""

from odoo.addons.grove_headless.controllers.main import (
    PRODUCT_DETAIL_FIELDS,
    PRODUCT_LIST_FIELDS,
    _image_url,
    _serialize_facts,
    _serialize_images,
    _serialize_product,
    _structure_variant,
)
from odoo.tests import TransactionCase, tagged

# 1x1 transparent PNG.
_PNG_1X1 = b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@tagged("post_install", "-at_install")
class TestDetailSerialization(TransactionCase):
    def setUp(self):
        super().setUp()
        self.cultivar = self.env["product.attribute"].create({"name": "Cultivar", "create_variant": "always"})
        self.fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        self.c_mag = self.env["product.attribute.value"].create({"name": "Magness", "attribute_id": self.cultivar.id})
        self.f_pt = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": self.fmt.id})
        self.f_br = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": self.fmt.id})
        self.tmpl = self.env["product.template"].create(
            {
                "name": "Pear",
                "type": "consu",
                "grove_botanical_name": "Pyrus communis",
                "grove_zone_min": 5,
                "grove_zone_max": 8,
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.cultivar.id, "value_ids": [(6, 0, [self.c_mag.id])]}),
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.f_pt.id, self.f_br.id])]}),
                ],
            }
        )

    def test_facts_block(self):
        facts = _serialize_facts(self.tmpl)
        self.assertEqual(facts["botanical_name"], "Pyrus communis")
        self.assertEqual(facts["zone_min"], 5)
        self.assertEqual(facts["layer"], "")

    def test_structured_variant(self):
        bareroot = self.tmpl.product_variant_ids.filtered(
            lambda v: "Bareroot" in v.product_template_variant_value_ids.mapped("name")
        )
        data = _structure_variant(bareroot)
        self.assertEqual(data["cultivar"], "Magness")
        self.assertEqual(data["format"], "Bareroot")
        self.assertEqual(data["shipping_tier"], "bareroot")
        self.assertIn("price", data)
        self.assertIn("qty_available", data)

    def test_images_hero_first_and_empty_ok(self):
        self.assertEqual(_serialize_images(self.tmpl), [])  # no image set
        self.tmpl.image_1920 = (
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        images = _serialize_images(self.tmpl)
        self.assertEqual(images[0]["url"], f"/web/image/product.template/{self.tmpl.id}/image_1024")
        self.assertEqual(images[0]["thumb_url"], f"/web/image/product.template/{self.tmpl.id}/image_256")

    def test_detail_exposes_sale_ok_for_coming_soon(self):
        # A published-but-not-for-sale "coming soon" placeholder (GOL-760): the
        # detail payload must carry sale_ok=False so the storefront can lock the
        # buy box. Without it the frontend infers purchasability from stock alone
        # and a qty-0 Bareroot placeholder leaks a live "Reserve" deposit.
        self.assertIn("sale_ok", PRODUCT_DETAIL_FIELDS)
        # GOL-760: coming-soon products now appear in the grid too, so the LIST
        # payload must carry sale_ok as well for the card to read as not-for-sale.
        self.assertIn("sale_ok", PRODUCT_LIST_FIELDS)
        self.tmpl.sale_ok = False
        data = _serialize_product(self.tmpl, PRODUCT_DETAIL_FIELDS)
        self.assertIn("sale_ok", data)
        self.assertFalse(data["sale_ok"])
        self.assertFalse(_serialize_product(self.tmpl, PRODUCT_LIST_FIELDS)["sale_ok"])
        self.tmpl.sale_ok = True
        self.assertTrue(_serialize_product(self.tmpl, PRODUCT_DETAIL_FIELDS)["sale_ok"])

    def test_image_url_null_when_empty(self):
        # Imageless product: list/detail image_url must be null, not the gray
        # placeholder PNG Odoo's /web/image route serves at HTTP 200 (GOL-684),
        # so the frontend can render its branded botanical placeholder.
        self.assertIsNone(_image_url("product.template", self.tmpl, "image_128"))
        self.assertIsNone(_image_url("product.template", self.tmpl, "image_1920"))
        # A variant with no image (template also imageless) is null too.
        variant = self.tmpl.product_variant_ids[0]
        self.assertIsNone(_structure_variant(variant)["image_url"])

    def test_image_url_present_when_set(self):
        self.tmpl.image_1920 = _PNG_1X1
        self.assertEqual(
            _image_url("product.template", self.tmpl, "image_128"),
            f"/web/image/product.template/{self.tmpl.id}/image_128",
        )
        self.assertEqual(
            _image_url("product.template", self.tmpl, "image_1920"),
            f"/web/image/product.template/{self.tmpl.id}/image_1920",
        )
        variant = self.tmpl.product_variant_ids[0]
        variant.image_variant_1920 = _PNG_1X1
        self.assertEqual(
            _structure_variant(variant)["image_url"],
            f"/web/image/product.product/{variant.id}/image_128",
        )
