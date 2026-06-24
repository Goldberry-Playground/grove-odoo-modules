"""Regression tests for the WV sales tax binding.

Guards the fix for the long-standing bug where WV 6% state + 1% municipal
tax was *defined* but never *applied* — orders fell back to the 15% Chart of
Accounts default. These tests assert the binding (default tax + a real sale
order line charging exactly 7%) so the regression cannot silently return.

post_install: needs the post_init_hook + data files to have run during init.
"""

from odoo.addons.grove_headless.hooks import WV_GROUP_NAME
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWvSalesTax(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")

    def test_company_has_wv_group_default_tax(self):
        """A combined WV 7% group tax exists for the company with 6%+1% split."""
        group = self.env["account.tax"].search(
            [
                ("name", "=", WV_GROUP_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "group"),
            ],
            limit=1,
        )
        self.assertTrue(group, "WV Sales Tax 7% group tax should exist")
        amounts = sorted(group.children_tax_ids.mapped("amount"))
        self.assertEqual(amounts, [1.0, 6.0], "group should combine 6% state + 1% municipal")

    def test_new_product_defaults_to_wv_tax(self):
        """A product created in the company context defaults to the WV tax."""
        default_tax_ids = self.env["ir.default"]._get(
            "product.template", "taxes_id", company_id=self.company.id
        )
        self.assertTrue(default_tax_ids, "ir.default for product taxes_id should be set")
        default_taxes = self.env["account.tax"].browse(default_tax_ids)
        self.assertEqual(default_taxes.mapped("name"), [WV_GROUP_NAME])

    def test_sale_order_line_charges_7_percent(self):
        """The end-to-end check: a $100 line is taxed exactly $7.00 (not $15)."""
        group = self.env["account.tax"].search(
            [
                ("name", "=", WV_GROUP_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "group"),
            ],
            limit=1,
        )
        product = self.env["product.product"].create(
            {
                "name": "Test Fruit Tree",
                "type": "consu",
                "list_price": 100.0,
                "taxes_id": [(6, 0, group.ids)],
            }
        )
        partner = self.env["res.partner"].create({"name": "Market Customer"})
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "order_line": [
                        (
                            0,
                            0,
                            {
                                "product_id": product.id,
                                "product_uom_qty": 1.0,
                                "price_unit": 100.0,
                            },
                        )
                    ],
                }
            )
        )
        line = order.order_line
        self.assertAlmostEqual(line.price_tax, 7.0, places=2, msg="line tax should be 7%, not 15%")
        self.assertAlmostEqual(order.amount_tax, 7.0, places=2)
        self.assertAlmostEqual(order.amount_total, 107.0, places=2)
