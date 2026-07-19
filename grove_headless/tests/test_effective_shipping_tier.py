"""Variant-level shipping tier (fixes bareroot variants quoting potted rates)."""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEffectiveShippingTier(TransactionCase):
    def setUp(self):
        super().setUp()
        self.fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        self.v_potted = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": self.fmt.id})
        self.v_bareroot = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": self.fmt.id})

    def test_bareroot_variant_overrides_template_tier(self):
        tmpl = self.env["product.template"].create(
            {
                "name": "Tier Pear",
                "type": "consu",
                "grove_shipping_tier": "potted",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.v_potted.id, self.v_bareroot.id])]})
                ],
            }
        )
        tiers = {
            v.product_template_variant_value_ids.name: v.grove_effective_shipping_tier
            for v in tmpl.product_variant_ids
        }
        self.assertEqual(tiers["Bareroot"], "bareroot")
        self.assertEqual(tiers["Potted"], "potted")

    def test_no_format_axis_falls_back_to_template(self):
        tmpl = self.env["product.template"].create(
            {"name": "Plain Aronia", "type": "consu", "grove_shipping_tier": "bareroot"}
        )
        self.assertEqual(tmpl.product_variant_id.grove_effective_shipping_tier, "bareroot")

    def test_default_is_potted(self):
        tmpl = self.env["product.template"].create({"name": "Untagged", "type": "consu"})
        self.assertEqual(tmpl.product_variant_id.grove_effective_shipping_tier, "potted")
