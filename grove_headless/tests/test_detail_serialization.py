"""Detail serializer: facts block + structured variants (catalog spec)."""

from odoo.addons.grove_headless.controllers.main import (
    PRODUCT_DETAIL_FIELDS,
    PRODUCT_LIST_FIELDS,
    _cultivar_count,
    _gate_guide_fields,
    _image_url,
    _ordered_variants,
    _serialize_facts,
    _serialize_images,
    _serialize_product,
    _structure_variant,
    _template_rootstock,
)
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged

# 1x1 transparent PNG.
_PNG_1X1 = b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@tagged("post_install", "-at_install")
class TestDetailSerialization(GroveTaxFixtureMixin, TransactionCase):
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

    def test_variants_ordered_bareroot_first(self):
        # GOL-1868: product_variant_ids has no `sequence`, so Odoo's recordset
        # order is a function of per-environment SKU/creation data (prod surfaced
        # Potted first, QA Bareroot first — GOL-1862). The controller must emit
        # shippable/preorderable tiers (bareroot) before pickup-only (potted),
        # independent of that order.
        ordered = _ordered_variants(self.tmpl)
        self.assertEqual(ordered.mapped("grove_effective_shipping_tier"), ["bareroot", "potted"])

    def test_variants_order_is_stable_on_id(self):
        # A second cultivar yields two bareroot + two potted variants; within a
        # tier the tiebreak is ascending id, so the order is fully deterministic.
        c_bart = self.env["product.attribute.value"].create({"name": "Bartlett", "attribute_id": self.cultivar.id})
        line = self.tmpl.attribute_line_ids.filtered(lambda al: al.attribute_id == self.cultivar)
        line.value_ids = [(4, c_bart.id)]
        ordered = _ordered_variants(self.tmpl)
        self.assertEqual(
            ordered.mapped("grove_effective_shipping_tier"),
            ["bareroot", "bareroot", "potted", "potted"],
        )
        bareroots = ordered.filtered(lambda v: v.grove_effective_shipping_tier == "bareroot")
        self.assertEqual(bareroots.ids, sorted(bareroots.ids))
        potteds = ordered.filtered(lambda v: v.grove_effective_shipping_tier == "potted")
        self.assertEqual(potteds.ids, sorted(potteds.ids))

    def test_structured_variant_rootstock_absent_reads_empty(self):
        # GOL-1117: a product with no Rootstock attribute line still carries the
        # key, as "" — the storefront treats that as "no propagation axis" and
        # renders no pill/selector. The field is always present (additive contract).
        variant = self.tmpl.product_variant_ids[0]
        data = _structure_variant(variant)
        self.assertIn("rootstock", data)
        self.assertEqual(data["rootstock"], "")

    def test_structured_variant_parses_rootstock_variant_axis(self):
        # GOL-1117: when a template carries a Rootstock axis that DOES create
        # variants, each variant's own rootstock value is parsed into the payload,
        # mirroring how the Cultivar/Format axes are already surfaced.
        rootstock = self.env["product.attribute"].create({"name": "Rootstock", "create_variant": "always"})
        r_graft = self.env["product.attribute.value"].create({"name": "M.111", "attribute_id": rootstock.id})
        self.tmpl.attribute_line_ids = [
            (0, 0, {"attribute_id": rootstock.id, "value_ids": [(6, 0, [r_graft.id])]}),
        ]
        variant = self.tmpl.product_variant_ids[0]
        data = _structure_variant(variant)
        self.assertEqual(data["rootstock"], "M.111")

    def test_template_rootstock_no_variant_axis_is_uniform(self):
        # GOL-1117: the QA/production model — a single-value no_variant Rootstock
        # line. It must NOT explode the Cultivar x Format variant grid, and its
        # value is surfaced uniformly on every variant (metadata pill).
        variants_before = len(self.tmpl.product_variant_ids)
        rootstock = self.env["product.attribute"].create({"name": "Rootstock", "create_variant": "no_variant"})
        r_m111 = self.env["product.attribute.value"].create({"name": "M.111", "attribute_id": rootstock.id})
        self.tmpl.attribute_line_ids = [
            (0, 0, {"attribute_id": rootstock.id, "value_ids": [(6, 0, [r_m111.id])]}),
        ]
        # No variant explosion: the two Potted/Bareroot variants are untouched.
        self.assertEqual(len(self.tmpl.product_variant_ids), variants_before)
        self.assertEqual(_template_rootstock(self.tmpl), "M.111")
        for variant in self.tmpl.product_variant_ids:
            data = _structure_variant(variant, _template_rootstock(self.tmpl))
            self.assertEqual(data["rootstock"], "M.111")

    def test_template_rootstock_multi_value_is_ambiguous(self):
        # A multi-value no_variant line can't map to a single per-variant value,
        # so it reads as no axis ("") rather than guessing.
        rootstock = self.env["product.attribute"].create({"name": "Rootstock", "create_variant": "no_variant"})
        r_a = self.env["product.attribute.value"].create({"name": "M.111", "attribute_id": rootstock.id})
        r_b = self.env["product.attribute.value"].create({"name": "Seedling", "attribute_id": rootstock.id})
        self.tmpl.attribute_line_ids = [
            (0, 0, {"attribute_id": rootstock.id, "value_ids": [(6, 0, [r_a.id, r_b.id])]}),
        ]
        self.assertEqual(_template_rootstock(self.tmpl), "")

    def test_template_rootstock_absent_reads_empty(self):
        self.assertEqual(_template_rootstock(self.tmpl), "")

    def test_cultivar_count_ignores_format_axis(self):
        # GOL-919: single-cultivar Pear (Magness) with a Potted/Bareroot Format
        # axis has TWO variants but is still ONE variety. The storefront count
        # must track distinct cultivars, not the Cultivar × Format variant grid.
        self.assertEqual(len(self.tmpl.product_variant_ids), 2)
        self.assertEqual(_cultivar_count(self.tmpl), 1)

    def test_cultivar_count_multi_cultivar(self):
        # Add a second cultivar -> two varieties (× two formats = four variants).
        c_bart = self.env["product.attribute.value"].create({"name": "Bartlett", "attribute_id": self.cultivar.id})
        line = self.tmpl.attribute_line_ids.filtered(lambda al: al.attribute_id == self.cultivar)
        line.value_ids = [(4, c_bart.id)]
        self.assertEqual(len(self.tmpl.product_variant_ids), 4)
        self.assertEqual(_cultivar_count(self.tmpl), 2)

    def test_cultivar_count_format_only_floors_at_one(self):
        # A Format-only product (Aronia-style: no Cultivar axis) has an empty
        # cultivar set -> floor at 1 so the card reads "1 variety", not "0".
        tmpl = self.env["product.template"].create(
            {
                "name": "Aronia",
                "type": "consu",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.f_pt.id, self.f_br.id])]}),
                ],
            }
        )
        self.assertEqual(len(tmpl.product_variant_ids), 2)
        self.assertEqual(_cultivar_count(tmpl), 1)

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

    def test_guide_fields_in_detail_contract(self):
        # PR A (GATH-130) contract: both the guide body and Wes's approval flag
        # are part of the detail read set the frontend GuideBlock consumes.
        self.assertIn("website_description", PRODUCT_DETAIL_FIELDS)
        self.assertIn("grove_guide_ready", PRODUCT_DETAIL_FIELDS)
        # ...but never leak into the leaner list payload (detail-only).
        self.assertNotIn("website_description", PRODUCT_LIST_FIELDS)
        self.assertNotIn("grove_guide_ready", PRODUCT_LIST_FIELDS)

    def test_guide_body_withheld_until_approved(self):
        # A drafted-but-unapproved guide must NOT cross the API boundary, even
        # though website_description is populated and in the read set.
        self.tmpl.website_description = "<p>How to grow a pear tree.</p>"
        self.tmpl.grove_guide_ready = False
        data = _gate_guide_fields(self.tmpl, _serialize_product(self.tmpl, PRODUCT_DETAIL_FIELDS))
        self.assertFalse(data["grove_guide_ready"])
        self.assertIsNone(data["website_description"])

    def test_guide_body_exposed_once_approved(self):
        self.tmpl.website_description = "<p>How to grow a pear tree.</p>"
        self.tmpl.grove_guide_ready = True
        data = _gate_guide_fields(self.tmpl, _serialize_product(self.tmpl, PRODUCT_DETAIL_FIELDS))
        self.assertTrue(data["grove_guide_ready"])
        self.assertIn("How to grow a pear tree.", data["website_description"])

    def test_guide_body_none_when_approved_but_empty(self):
        # Approved with no body -> None (not False/""), so the frontend renders
        # its "coming soon" placeholder rather than an empty guide block.
        self.tmpl.website_description = False
        self.tmpl.grove_guide_ready = True
        data = _gate_guide_fields(self.tmpl, _serialize_product(self.tmpl, PRODUCT_DETAIL_FIELDS))
        self.assertTrue(data["grove_guide_ready"])
        self.assertIsNone(data["website_description"])

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
