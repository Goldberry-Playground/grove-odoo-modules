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
class TestBranchCompanyTaxBinding(GroveTaxFixtureMixin, TransactionCase):
    """GOL-2449 regression: the WV 6% default must bind for a NON-main (branch)
    company, not just the first company the hook iterates.

    The nursery and GGG companies are branches of the Farm hierarchy root
    (data/grove_companies.xml). Odoo 19 scopes account.tax name-uniqueness to
    that root, so the *old* per-company create raised "Tax names must be unique!"
    for the branches and its swallowed failure left them on the demo 15% default
    — the exact prod defect. This asserts the branch is bound (reusing the shared
    root tax), so the regression cannot silently return.
    """

    def setUp(self):
        super().setUp()
        # A branch company (parent_id = base.main_company). Falls back to GGG if
        # the nursery xmlid is unavailable in a given DB.
        self.branch = self.env.ref("grove_headless.company_nursery", raise_if_not_found=False) or self.env.ref(
            "grove_headless.company_ggg"
        )
        self.assertTrue(self.branch.parent_id, "test company must be a branch, not the root")

    def test_ensure_company_wv_taxes_reuses_root_tax_without_collision(self):
        """The helper returns a usable 6% state tax for the branch and never
        raises the root-scoped name-uniqueness error."""
        from odoo.addons.grove_headless.hooks import _accessible_companies, _ensure_company_wv_taxes

        # The hook runs under SUPERUSER at install/migration; mirror that so the
        # assertion exercises the binding, not test-only record-rule visibility.
        env = self.env(su=True)
        state = _ensure_company_wv_taxes(env, self.branch.with_env(env))
        self.assertTrue(state, "branch must resolve a WV state tax")
        self.assertEqual(state.amount, 6.0)
        self.assertEqual(state.amount_type, "percent")
        self.assertEqual(state.name, WV_STATE_NAME)
        # It is the shared record accessible to the branch (its own or an ancestor's).
        self.assertIn(state.company_id, _accessible_companies(self.branch.with_env(env)))

    def test_hook_binds_branch_product_default_to_wv_state_tax(self):
        """Running the full binder sets the branch's authoritative product-tax
        default (ir.default) to the WV 6% state tax — the bind the old hook skipped."""
        from odoo.addons.grove_headless.hooks import setup_wv_sales_tax

        env = self.env(su=True)
        setup_wv_sales_tax(env)

        default_ids = env["ir.default"]._get("product.template", "taxes_id", company_id=self.branch.id)
        self.assertTrue(default_ids, "branch company must get a product-tax default")
        default_taxes = env["account.tax"].browse(default_ids)
        self.assertEqual(
            default_taxes.mapped("name"),
            [WV_STATE_NAME],
            "branch default must be the WV 6% state tax, not the demo 15%",
        )
        # The single-valued company default also points at a WV 6% state tax.
        branch = self.branch.with_env(env)
        self.assertEqual(branch.account_sale_tax_id.name, WV_STATE_NAME)
        self.assertEqual(branch.account_sale_tax_id.amount, 6.0)


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


@tagged("post_install", "-at_install")
class TestBranchDuplicateTaxConvergence(GroveTaxFixtureMixin, TransactionCase):
    """GOL-2449 prod incident (2026-09-23): a pre-existing per-BRANCH tax with
    the same name as the root's makes the whole binder fail.

    This is the shape TestBranchCompanyTaxBinding does not cover. On prod the
    nursery owned its own "WV State Sales Tax 6%" (created 2026-08-08 by
    data/grove_taxes.xml) while the hook created a second one on the root.
    Odoo 19 scopes account.tax name-uniqueness to the hierarchy root AND
    validates @api.constrains at FLUSH, so both rows landed and every later
    flush raised "Tax names must be unique!" — swallowed per company, leaving
    prod at "bound for only 1 of 3" with two companies on the demo 15%.

    The binder must converge such a hierarchy to ONE root-owned record and bind
    every company.
    """

    def setUp(self):
        super().setUp()
        self.root = self.env.ref("base.main_company")
        self.branch = self.env.ref("grove_headless.company_nursery", raise_if_not_found=False) or self.env.ref(
            "grove_headless.company_ggg"
        )
        self.assertTrue(self.branch.parent_id, "test company must be a branch, not the root")

    def _subtree_state_taxes(self, env):
        return env["account.tax"].search(
            [
                ("name", "=", WV_STATE_NAME),
                ("company_id", "child_of", self.root.id),
                ("type_tax_use", "=", "sale"),
            ]
        )

    def test_binder_converges_a_branch_owned_duplicate_and_binds_every_company(self):
        from odoo.addons.grove_headless.hooks import setup_wv_sales_tax

        env = self.env(su=True)

        # Reproduce prod: make the BRANCH own the state tax, with the root
        # holding none. (Re-point an existing root record rather than creating a
        # second one, which the constraint would reject outright.)
        existing = self._subtree_state_taxes(env)
        self.assertTrue(existing, "fixture must provide a WV state tax to relocate")
        branch_tax = existing[0]
        branch_tax.company_id = self.branch.id
        env.flush_all()
        self.assertEqual(branch_tax.company_id, self.branch, "precondition: branch owns the tax")

        setup_wv_sales_tax(env)

        # Exactly one record survives in the hierarchy, owned by the ROOT so
        # every branch can use it (check_company is parent_of).
        taxes = self._subtree_state_taxes(env)
        self.assertEqual(len(taxes), 1, f"expected one shared WV state tax, got {taxes.mapped('company_id.name')}")
        self.assertEqual(taxes.company_id, self.root, "the surviving tax must be owned by the hierarchy root")

        # And every company is bound to it — the "1 of 3" regression.
        for company in env["res.company"].search([]):
            self.assertEqual(
                company.account_sale_tax_id.name,
                WV_STATE_NAME,
                f"{company.name} must be bound to the WV 6% state tax, not the demo default",
            )
            self.assertEqual(company.account_sale_tax_id.amount, 6.0)

    def test_convergence_preserves_the_oldest_record(self):
        """The keeper is the oldest row, because it carries the product and
        accounting history (on prod, 123 nursery templates referenced it)."""
        from odoo.addons.grove_headless.hooks import _converge_root_wv_taxes

        env = self.env(su=True)
        existing = self._subtree_state_taxes(env)
        self.assertTrue(existing)
        oldest = min(existing, key=lambda t: t.id)
        oldest_id = oldest.id
        oldest.company_id = self.branch.id
        env.flush_all()

        _converge_root_wv_taxes(env, self.root)

        taxes = self._subtree_state_taxes(env)
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes.id, oldest_id, "convergence must keep the oldest record, not recreate a new one")
