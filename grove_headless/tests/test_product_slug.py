"""Tests for the grove_slug computed field on product.template.

Run via:
    odoo --addons-path=... --test-enable --stop-after-init -i grove_headless

The CI install-smoke-test job in .github/workflows/ci.yml runs them on
every PR.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged("grove_headless", "grove_slug", "post_install", "-at_install")
class TestGroveSlug(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Product = self.env["product.template"]

    def test_slug_derives_from_name(self):
        """A product named 'Shagbark Hickory Syrup' gets slug 'shagbark-hickory-syrup'."""
        product = self.Product.create({"name": "Shagbark Hickory Syrup"})
        self.assertEqual(product.grove_slug, "shagbark-hickory-syrup")

    def test_slug_lowercases_and_dashifies(self):
        """Spaces, capitals, and punctuation normalize."""
        product = self.Product.create({"name": "Black Walnut Halves (6oz)"})
        self.assertEqual(product.grove_slug, "black-walnut-halves-6oz")

    def test_slug_collapses_multiple_separators(self):
        """Consecutive non-alphanumerics collapse to one dash."""
        product = self.Product.create({"name": "Hickory  Syrup   &   Bourbon"})
        self.assertEqual(product.grove_slug, "hickory-syrup-bourbon")

    def test_slug_trims_leading_and_trailing_dashes(self):
        """Leading/trailing separators are stripped."""
        product = self.Product.create({"name": "!! Pawpaw Butter !!"})
        self.assertEqual(product.grove_slug, "pawpaw-butter")

    def test_collision_appends_id_suffix(self):
        """Two products with the same name in the same company get different slugs."""
        c1 = self.Product.create({"name": "Hickory Syrup"})
        c2 = self.Product.create({"name": "Hickory Syrup"})
        self.assertEqual(c1.grove_slug, "hickory-syrup")
        # The second one is suffixed with the id to break the tie deterministically.
        self.assertEqual(c2.grove_slug, f"hickory-syrup-{c2.id}")

    def test_same_slug_allowed_across_companies(self):
        """Two products with the same name in DIFFERENT companies both get the bare slug."""
        Company = self.env["res.company"]
        c1 = Company.create({"name": "Test Company A"})
        c2 = Company.create({"name": "Test Company B"})
        p1 = self.Product.create({"name": "Hickory Syrup", "company_id": c1.id})
        p2 = self.Product.create({"name": "Hickory Syrup", "company_id": c2.id})
        self.assertEqual(p1.grove_slug, "hickory-syrup")
        self.assertEqual(p2.grove_slug, "hickory-syrup")

    def test_punctuation_only_name_yields_false_slug(self):
        """A name that slugifies to empty produces grove_slug=False (not empty string)."""
        product = self.Product.create({"name": "!!!"})
        self.assertFalse(product.grove_slug)

    def test_empty_name_yields_false_slug(self):
        """A name that is just whitespace produces grove_slug=False."""
        # Odoo requires name on product.template; the closest valid edge is whitespace-only,
        # which still slugifies to empty via the regex collapse + strip.
        product = self.Product.create({"name": "   "})
        self.assertFalse(product.grove_slug)

    def test_slug_recomputes_on_name_change(self):
        """Renaming a product updates its slug."""
        product = self.Product.create({"name": "Hickory Syrup"})
        product.name = "Shagbark Hickory Syrup"
        # Stored compute fields refresh on next access; flush + invalidate forces it.
        product.flush_recordset()
        product.invalidate_recordset(["grove_slug"])
        self.assertEqual(product.grove_slug, "shagbark-hickory-syrup")
