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
        # `company_id=False` keeps them shared across companies so the
        # multi-company isolation test below can reference these products
        # from any test-created company without tripping product/company
        # consistency constraints.
        self.source = Product.create({"name": "Honeycrisp 1gal Liner", "type": "consu", "company_id": False})
        self.target = Product.create({"name": "Honeycrisp 3gal Pot", "type": "consu", "company_id": False})

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
        # `PB/` is the deliberate prefix from data/grove_sequences.xml;
        # asserting on it catches both the "sequence didn't run" failure
        # (fallback would be the placeholder, no slash) and the "sequence
        # ran with the wrong prefix" silent regression.
        batch = self._make_batch()
        self.assertTrue(batch.name, "Reference must be set on create")
        self.assertTrue(batch.name.startswith("PB/"), f"Expected PB/ prefix, got {batch.name!r}")

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

    def test_bom_lookup_skips_reshuffled_match(self):
        """If the cached BOM gets reshuffled, the next batch creates a fresh one
        instead of repeatedly falling through to grow the BOM catalog forever
        (regression for the oldest-first search ordering bug)."""
        first = self._make_batch(quantity=10)
        first.action_confirm()
        original_bom = first.production_id.bom_id

        # Tamper with the cached BOM — add a second line so it no longer
        # matches the (one-line, source → target) shape we cache by.
        rogue_component = self.env["product.product"].create({"name": "Rogue Add-on", "type": "consu"})
        original_bom.write({"bom_line_ids": [(0, 0, {"product_id": rogue_component.id, "product_qty": 1.0})]})

        # Next batch must NOT reuse the tampered BOM.
        second = self._make_batch(quantity=5)
        second.action_confirm()
        self.assertNotEqual(
            second.production_id.bom_id,
            original_bom,
            "Reshuffled BOMs must be skipped, not silently reused",
        )

        # And a third batch should now reuse the second's (clean) BOM.
        third = self._make_batch(quantity=3)
        third.action_confirm()
        self.assertEqual(
            third.production_id.bom_id,
            second.production_id.bom_id,
            "After tamper-skip, the new clean BOM should be the cache target",
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

    def test_multi_company_isolation(self):
        """A user scoped to company A must not see batches from company B.

        Verifies the ir.rule in security/grove_security_rules.xml — without
        it, the CSV ACL grants stock.group_stock_user full CRUD across every
        company in the database, leaking nursery data into Goldberry and
        vice versa.
        """
        company_a = self.env["res.company"].create({"name": "Grove Test Co A"})
        company_b = self.env["res.company"].create({"name": "Grove Test Co B"})

        # A user who can only see company_a. Same stock.group_stock_user
        # group the CSV grants — this is the realistic threat model.
        user_a = self.env["res.users"].create(
            {
                "name": "Grove User A",
                "login": "grove_test_user_a",
                "company_id": company_a.id,
                "company_ids": [(6, 0, [company_a.id])],
                "groups_id": [(4, self.env.ref("stock.group_stock_user").id)],
            }
        )

        batch_a = self._make_batch(quantity=10, company_id=company_a.id)
        batch_b = self._make_batch(quantity=10, company_id=company_b.id)

        visible = (
            self.env["grove.potting.batch"]
            .with_user(user_a)
            .with_company(company_a)
            .search([("id", "in", [batch_a.id, batch_b.id])])
        )
        self.assertIn(batch_a, visible, "User A must see their own company's batch")
        self.assertNotIn(
            batch_b,
            visible,
            "User A must NOT see another company's batch — ir.rule isolation failed",
        )
