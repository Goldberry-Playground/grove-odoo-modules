"""Regression tests for the WV sales tax binding.

Guards the fix for the long-standing bug where WV 6% state + 1% municipal
tax was *defined* but never *applied* — orders fell back to the 15% Chart of
Accounts default. These tests assert the binding (default tax + a real sale
order line charging exactly 7%) so the regression cannot silently return.

post_install: needs the post_init_hook + data files to have run during init.
"""

from odoo.addons.grove_headless.controllers import main as gh_main
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
        default_tax_ids = self.env["ir.default"]._get("product.template", "taxes_id", company_id=self.company.id)
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


@tagged("post_install", "-at_install")
class TestDestinationTax(TransactionCase):
    """GOL-1021 defect 2 — WV sales tax is destination-based: it must apply only
    to WV-bound orders and be stripped for any other ship-to state (e.g. Ohio),
    since Grove's only sales-tax nexus is West Virginia."""

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.group = self.env["account.tax"].search(
            [
                ("name", "=", WV_GROUP_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "group"),
            ],
            limit=1,
        )
        self.assertTrue(self.group, "WV group tax must exist (post_init_hook)")

    def _order_with_wv_line(self):
        product = self.env["product.product"].create(
            {
                "name": "Test Fruit Tree",
                "type": "consu",
                "list_price": 100.0,
                "taxes_id": [(6, 0, self.group.ids)],
            }
        )
        partner = self.env["res.partner"].create({"name": "Dest Tax Customer"})
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "order_line": [
                        (0, 0, {"product_id": product.id, "product_uom_qty": 1.0, "price_unit": 100.0})
                    ],
                }
            )
        )
        # Sanity: the line starts WV-taxed via the product default.
        self.assertAlmostEqual(order.amount_tax, 7.0, places=2)
        return order

    def test_ohio_order_strips_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "OH"})
        self.assertFalse(order.order_line.tax_id, "WV tax must be removed for an OH destination")
        self.assertAlmostEqual(order.amount_tax, 0.0, places=2)
        self.assertAlmostEqual(order.amount_total, 100.0, places=2)

    def test_ohio_full_name_strips_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "Ohio"})
        self.assertAlmostEqual(order.amount_tax, 0.0, places=2)

    def test_wv_order_keeps_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "WV"})
        self.assertAlmostEqual(order.amount_tax, 7.0, places=2, msg="WV-bound order must keep WV tax")

    def test_unknown_state_conservatively_keeps_wv_tax(self):
        # An unresolvable ship-to state must not silently zero out the tax —
        # leave the default in place rather than guess a zero-tax order.
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": ""})
        self.assertAlmostEqual(order.amount_tax, 7.0, places=2)


@tagged("post_install", "-at_install")
class TestPartnerStateResolution(TransactionCase):
    """GOL-1021 defect 1 (partner side) — a US state given as a full name must
    still bind partner.state_id, or downstream billing + label buying mis-fire."""

    def test_full_state_name_resolves_state_id(self):
        vals = gh_main._partner_vals_from_payload(
            self.env,
            {"name": "Full Name Customer", "email": "fn@example.com"},
            {"country": "US", "state": "Ohio"},
        )
        state = self.env["res.country.state"].browse(vals.get("state_id"))
        self.assertTrue(vals.get("state_id"), "full state name should resolve to a state_id")
        self.assertEqual(state.code, "OH")

    def test_lowercase_code_resolves_state_id(self):
        vals = gh_main._partner_vals_from_payload(
            self.env,
            {"name": "Lower Code Customer", "email": "lc@example.com"},
            {"country": "US", "state": "wv"},
        )
        state = self.env["res.country.state"].browse(vals.get("state_id"))
        self.assertEqual(state.code, "WV")
