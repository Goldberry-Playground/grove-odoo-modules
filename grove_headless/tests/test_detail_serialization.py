"""Detail serializer: facts block + structured variants (catalog spec)."""

from odoo.addons.grove_headless.controllers.main import (
    _serialize_facts,
    _serialize_images,
    _structure_variant,
)
from odoo.tests import TransactionCase, tagged


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
