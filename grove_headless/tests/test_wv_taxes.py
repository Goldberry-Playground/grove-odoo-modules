"""Regression tests for the WV sales tax binding.

Guards the fix for the long-standing bug where WV state tax was *defined* but
never *applied* — orders fell back to the demo 15% Chart of Accounts default
(GOL-1021, and again on prod as GOL-2449). Josh's 2026-09-22 ruling made the
web/POS tax **6% WV state only** (no municipal, no group), on goods AND shipping.
These tests assert the binding (default tax + a real sale order line charging
exactly 6%) plus the company-integrity guard so the regression cannot silently
return.

post_install: needs the post_init_hook + data files to have run during init.
"""

from odoo.addons.grove_headless.controllers import main as gh_main
from odoo.addons.grove_headless.hooks import WV_MUNI_NAME, WV_STATE_NAME
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestWvSalesTax(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")

    def _state_tax(self):
        return self.env["account.tax"].search(
            [
                ("name", "=", WV_STATE_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "percent"),
            ],
            limit=1,
        )

    def test_company_has_wv_state_default_tax(self):
        """A 6% WV state tax exists for the company (single percent tax, no group)."""
        state = self._state_tax()
        self.assertTrue(state, "WV State Sales Tax 6% should exist")
        self.assertEqual(state.amount, 6.0)
        self.assertEqual(state.amount_type, "percent")

    def test_municipal_record_kept_but_not_bound(self):
        """The 1% municipal record is kept for the books but never made the default."""
        muni = self.env["account.tax"].search(
            [("name", "=", WV_MUNI_NAME), ("company_id", "=", self.company.id)],
            limit=1,
        )
        self.assertTrue(muni, "WV Municipal Tax 1% record should still exist")
        default_ids = self.env["ir.default"]._get("product.template", "taxes_id", company_id=self.company.id)
        self.assertNotIn(muni.id, default_ids or [], "municipal tax must not be a product default")

    def test_new_product_defaults_to_wv_state_tax(self):
        """A product created in the company context defaults to the WV state tax."""
        default_tax_ids = self.env["ir.default"]._get("product.template", "taxes_id", company_id=self.company.id)
        self.assertTrue(default_tax_ids, "ir.default for product taxes_id should be set")
        default_taxes = self.env["account.tax"].browse(default_tax_ids)
        self.assertEqual(default_taxes.mapped("name"), [WV_STATE_NAME])

    def test_sale_order_line_charges_6_percent(self):
        """The end-to-end check: a $100 line is taxed exactly $6.00 (not $15, not $7)."""
        state = self._state_tax()
        product = self.env["product.product"].create(
            {
                "name": "Test Fruit Tree",
                "type": "consu",
                "list_price": 100.0,
                "taxes_id": [(6, 0, state.ids)],
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
        self.assertAlmostEqual(line.price_tax, 6.0, places=2, msg="line tax should be 6%, not 15% or 7%")
        self.assertAlmostEqual(order.amount_tax, 6.0, places=2)
        self.assertAlmostEqual(order.amount_total, 106.0, places=2)


@tagged("post_install", "-at_install")
class TestTaxCompanyIntegrity(GroveTaxFixtureMixin, TransactionCase):
    """GOL-2449 guard: no sale-able product may carry a tax owned by a DIFFERENT
    company (the prod defect where 52 nursery products carried company-1's demo
    15% tax), and the GROVE-SHIP shipping SKU must carry exactly the company's WV
    6% state tax."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def _state_tax(self):
        return self.env["account.tax"].search(
            [
                ("name", "=", WV_STATE_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "percent"),
            ],
            limit=1,
        )

    def test_no_saleable_product_carries_a_foreign_company_tax(self):
        templates = self.env["product.template"].search(
            [("sale_ok", "=", True), ("company_id", "in", [self.company.id, False])]
        )
        for tmpl in templates:
            foreign = tmpl.taxes_id.filtered(lambda t: t.company_id and t.company_id != self.company)
            self.assertFalse(
                foreign,
                f"{tmpl.display_name!r} carries a cross-company tax {foreign.mapped('name')} "
                f"(owner(s) {foreign.mapped('company_id.name')}) — should be this company's WV tax",
            )

    def test_shipping_product_carries_exactly_the_wv_state_tax(self):
        state = self._state_tax()
        self.assertTrue(state, "WV state tax must exist for the company")
        # Force the lazy shipping SKU into existence, then assert its tax.
        ship = gh_main._get_shipping_product(self.env, self.company)
        self.assertEqual(
            set(ship.taxes_id.ids),
            set(state.ids),
            f"{gh_main.SHIPPING_PRODUCT_CODE} must carry exactly the WV 6% state tax, "
            f"got {ship.taxes_id.mapped('name')}",
        )


@tagged("post_install", "-at_install")
class TestDestinationTax(GroveTaxFixtureMixin, TransactionCase):
    """GOL-1021 defect 2 — WV sales tax is destination-based: it must apply only
    to WV-bound orders and be stripped for any other ship-to state (e.g. Ohio),
    since Grove's only sales-tax nexus is West Virginia."""

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.state = self.env["account.tax"].search(
            [
                ("name", "=", WV_STATE_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "percent"),
            ],
            limit=1,
        )
        self.assertTrue(self.state, "WV state tax must exist (post_init_hook)")

    def _order_with_wv_line(self):
        product = self.env["product.product"].create(
            {
                "name": "Test Fruit Tree",
                "type": "consu",
                "list_price": 100.0,
                "taxes_id": [(6, 0, self.state.ids)],
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
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1.0, "price_unit": 100.0})],
                }
            )
        )
        # Sanity: the line starts WV-taxed via the product default.
        self.assertAlmostEqual(order.amount_tax, 6.0, places=2)
        return order

    def test_ohio_order_strips_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "OH"})
        self.assertFalse(order.order_line.tax_ids, "WV tax must be removed for an OH destination")
        self.assertAlmostEqual(order.amount_tax, 0.0, places=2)
        self.assertAlmostEqual(order.amount_total, 100.0, places=2)

    def test_ohio_full_name_strips_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "Ohio"})
        self.assertAlmostEqual(order.amount_tax, 0.0, places=2)

    def test_wv_order_keeps_wv_tax(self):
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": "WV"})
        self.assertAlmostEqual(order.amount_tax, 6.0, places=2, msg="WV-bound order must keep WV tax")

    def test_unknown_state_conservatively_keeps_wv_tax(self):
        # An unresolvable ship-to state must not silently zero out the tax —
        # leave the default in place rather than guess a zero-tax order.
        order = self._order_with_wv_line()
        gh_main._apply_destination_tax(self.env, order, {"state": ""})
        self.assertAlmostEqual(order.amount_tax, 6.0, places=2)


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
