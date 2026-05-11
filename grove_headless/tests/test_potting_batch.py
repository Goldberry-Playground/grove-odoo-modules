"""Regression tests for grove.potting.batch.

We use type='consu' products throughout so MO completion doesn't require
seeding stock.quants — the math we care about (consumption + production +
scrap) is identical between consumable and storable products.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPottingBatch(TransactionCase):
    def setUp(self):
        super().setUp()
        Product = self.env["product.product"]
        # Two variants of the "same" nursery plant — small pot vs larger pot.
        self.source = Product.create({"name": "Honeycrisp 1gal Liner", "type": "consu"})
        self.target = Product.create({"name": "Honeycrisp 3gal Pot", "type": "consu"})

    def _make_batch(self, **overrides):
        vals = {
            "source_product_id": self.source.id,
            "target_product_id": self.target.id,
            "quantity": 50.0,
            "mortality": 0.0,
        }
        vals.update(overrides)
        return self.env["grove.potting.batch"].create(vals)

    def test_sequence_assigns_reference(self):
        batch = self._make_batch()
        self.assertTrue(batch.name and batch.name != "New")
        self.assertIn("PB/", batch.name)

    def test_successful_qty_computed(self):
        batch = self._make_batch(quantity=50, mortality=3)
        self.assertEqual(batch.successful_qty, 47.0)

    def test_source_target_must_differ(self):
        with self.assertRaises(ValidationError):
            self._make_batch(target_product_id=self.source.id)

    def test_quantity_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self._make_batch(quantity=0)

    def test_mortality_cannot_exceed_quantity(self):
        with self.assertRaises(ValidationError):
            self._make_batch(quantity=10, mortality=11)

    def test_confirm_creates_production(self):
        """A clean batch with no mortality produces an MO but no scrap."""
        batch = self._make_batch(quantity=20, mortality=0)
        batch.action_confirm()
        self.assertEqual(batch.state, "done")
        self.assertTrue(batch.production_id, "MO should be created for successful potting")
        self.assertEqual(batch.production_id.product_qty, 20.0)
        self.assertEqual(batch.production_id.product_id, self.target)
        self.assertFalse(batch.scrap_id, "No mortality means no scrap order")

    def test_confirm_with_mortality_creates_both(self):
        batch = self._make_batch(quantity=50, mortality=3)
        batch.action_confirm()
        self.assertEqual(batch.state, "done")
        self.assertTrue(batch.scrap_id, "Mortality > 0 must create a scrap order")
        self.assertEqual(batch.scrap_id.product_id, self.source)
        self.assertEqual(batch.scrap_id.scrap_qty, 3.0)
        self.assertTrue(batch.production_id, "47 successful plants still need an MO")
        self.assertEqual(batch.production_id.product_qty, 47.0)

    def test_total_loss_skips_production(self):
        """If every plant dies, we scrap them all and skip the MO entirely."""
        batch = self._make_batch(quantity=10, mortality=10)
        batch.action_confirm()
        self.assertEqual(batch.state, "done")
        self.assertTrue(batch.scrap_id)
        self.assertEqual(batch.scrap_id.scrap_qty, 10.0)
        self.assertFalse(batch.production_id, "No survivors means no production order should exist")

    def test_bom_reused_across_batches(self):
        """Second batch with the same (source, target) reuses the first BOM."""
        first = self._make_batch(quantity=10)
        first.action_confirm()
        second = self._make_batch(quantity=5)
        second.action_confirm()
        self.assertEqual(
            first.production_id.bom_id,
            second.production_id.bom_id,
            "Repeat potting days must not create duplicate BOMs",
        )

    def test_cannot_confirm_twice(self):
        from odoo.exceptions import UserError

        batch = self._make_batch(quantity=5)
        batch.action_confirm()
        with self.assertRaises(UserError):
            batch.action_confirm()

    def test_cannot_cancel_done_batch(self):
        from odoo.exceptions import UserError

        batch = self._make_batch(quantity=5)
        batch.action_confirm()
        with self.assertRaises(UserError):
            batch.action_cancel()
