"""Regression tests for Kit-type Bills of Materials.

These tests guard the `mrp` dependency wired into __manifest__.py. If
someone accidentally drops `mrp` from depends, `self.env['mrp.bom']`
becomes KeyError at runtime — but more importantly, the storefront kit
products (e.g. "Spring Fruit Tree Starter Crate") would silently stop
exploding into their component variants for picking/delivery.

Post-install only: mrp.bom isn't available during at_install because
the dependency chain hasn't finished resolving yet.
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestKitBoms(TransactionCase):
    def setUp(self):
        super().setUp()
        # Two cheap components and one parent kit. We don't use the
        # real seeded products because tests should be hermetic — the
        # seeder may not have run on the test db.
        self.component_a = self.env["product.product"].create({"name": "Test Component A", "type": "consu"})
        self.component_b = self.env["product.product"].create({"name": "Test Component B", "type": "consu"})
        self.parent_template = self.env["product.template"].create({"name": "Test Kit Parent", "type": "consu"})

    def test_mrp_bom_model_is_registered(self):
        """The `mrp` manifest dependency must keep mrp.bom in the registry."""
        self.assertIn("mrp.bom", self.env.registry)

    def test_create_phantom_kit_bom(self):
        """A phantom BOM should bundle components under one parent template."""
        bom = self.env["mrp.bom"].create(
            {
                "product_tmpl_id": self.parent_template.id,
                "product_id": self.parent_template.product_variant_id.id,
                "type": "phantom",
                "product_qty": 1.0,
                "bom_line_ids": [
                    (0, 0, {"product_id": self.component_a.id, "product_qty": 1}),
                    (0, 0, {"product_id": self.component_b.id, "product_qty": 2}),
                ],
            }
        )
        self.assertEqual(bom.type, "phantom")
        self.assertEqual(len(bom.bom_line_ids), 2)
        # Quantities round-trip correctly — important because we ship
        # exactly what the kit promises (e.g. 3 shrubs, not "some shrubs").
        qty_by_component = {line.product_id.id: line.product_qty for line in bom.bom_line_ids}
        self.assertEqual(qty_by_component[self.component_a.id], 1.0)
        self.assertEqual(qty_by_component[self.component_b.id], 2.0)

    def test_phantom_bom_resolves_for_parent_template(self):
        """`_bom_find` must locate our kit when asked for the parent product."""
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": self.parent_template.id,
                "product_id": self.parent_template.product_variant_id.id,
                "type": "phantom",
                "product_qty": 1.0,
                "bom_line_ids": [
                    (0, 0, {"product_id": self.component_a.id, "product_qty": 1}),
                ],
            }
        )
        # _bom_find returns {product: bom} for the products it can resolve.
        found = self.env["mrp.bom"]._bom_find(self.parent_template.product_variant_id, bom_type="phantom")
        resolved = found.get(self.parent_template.product_variant_id)
        self.assertTrue(resolved, "Kit BOM should be discoverable via _bom_find")
        self.assertEqual(resolved.type, "phantom")
