"""Tests for the in-person POS configuration (GOL-13).

Verifies that the post_init/upgrade hook stands up the two in-person sales
channels correctly:

  * "Farmer's Market"  → crm.team "Farmer's Market"
  * "Nursery Counter"  → crm.team "Direct to Nursery"

each wired to the CSH1 (cash) / CARD / CHCK (bank) payment journals, and that a
POS sale of a WV-taxed product is charged exactly 7% (the same tax POS applies
server-side via account.tax). Full POS-session opening + browser ringing is
CEO-arranged QA (no QA hire yet); this covers the config + tax wiring that the
session depends on.

Runs against a self-contained accounted company (AccountTestInvoicingCommon)
so the wiring is proven deterministically regardless of whether the init-time
database had a chart of accounts.
"""

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.grove_headless.hooks import (
    POS_CONFIG_SPECS,
    WV_GROUP_NAME,
    _ensure_company_wv_taxes,
    _setup_company_pos,
)
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestPosConfig(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        # Ensure the WV 7% group tax exists in this company (products in the
        # tax test bind to it explicitly), then stand up the POS channels.
        _ensure_company_wv_taxes(cls.env, cls.company)
        cls.configs = _setup_company_pos(cls.env, cls.company)

    def _config(self, name):
        return self.env["pos.config"].search([("name", "=", name), ("company_id", "=", self.company.id)], limit=1)

    def _count(self, model, name):
        return self.env[model].search_count([("name", "=", name), ("company_id", "=", self.company.id)])

    def test_both_channels_created(self):
        """One pos.config exists per in-person channel, in the farm company."""
        for config_name, _team in POS_CONFIG_SPECS:
            config = self._config(config_name)
            self.assertTrue(config, f"pos.config {config_name!r} should exist")
            self.assertEqual(config.company_id, self.company)

    def test_channels_map_to_sales_teams(self):
        """Each channel points at its seeded sales team."""
        for config_name, team_name in POS_CONFIG_SPECS:
            config = self._config(config_name)
            self.assertEqual(
                config.crm_team_id.name,
                team_name,
                f"{config_name!r} should map to sales team {team_name!r}",
            )

    def test_payment_methods_wired_to_journals(self):
        """Both channels expose Cash/Card/Check, bound to CSH1/CARD/CHCK."""
        expected = {"CSH1": "cash", "CARD": "bank", "CHCK": "bank"}
        for config_name, _team in POS_CONFIG_SPECS:
            config = self._config(config_name)
            journals = config.payment_method_ids.mapped("journal_id")
            codes = {j.code: j.type for j in journals}
            self.assertEqual(
                codes,
                expected,
                f"{config_name!r} payment methods should settle to CSH1/CARD/CHCK",
            )
        # The cash method must be recognised as cash (POS opening balance).
        cash_method = self.env["pos.payment.method"].search(
            [("name", "=", "Cash"), ("company_id", "=", self.company.id)], limit=1
        )
        self.assertTrue(cash_method, "Cash payment method should exist")
        self.assertEqual(cash_method.journal_id.type, "cash")

    def test_pos_sale_charges_7_percent(self):
        """A $100 POS line is taxed exactly $7.00 via the WV group tax.

        POS computes line tax with account.tax the same way sale orders do, so
        asserting the tax the product carries proves the market-sale charge.
        """
        group = self.env["account.tax"].search(
            [
                ("name", "=", WV_GROUP_NAME),
                ("company_id", "=", self.company.id),
                ("amount_type", "=", "group"),
            ],
            limit=1,
        )
        self.assertTrue(group, "WV Sales Tax 7% group tax should exist")
        product = self.env["product.product"].create(
            {
                "name": "Market Test Item",
                "type": "consu",
                "list_price": 100.0,
                "taxes_id": [(6, 0, group.ids)],
            }
        )
        result = product.taxes_id.compute_all(100.0, currency=self.company.currency_id)
        self.assertAlmostEqual(result["total_included"], 107.0, places=2)
        self.assertAlmostEqual(result["total_included"] - result["total_excluded"], 7.0, places=2)

    def test_idempotent(self):
        """Re-running the setup does not duplicate configs, methods, or teams."""
        _setup_company_pos(self.env, self.company)
        for config_name, team_name in POS_CONFIG_SPECS:
            self.assertEqual(self._count("pos.config", config_name), 1, f"{config_name!r} not duplicated")
            self.assertEqual(self._count("crm.team", team_name), 1, f"team {team_name!r} not duplicated")
        for label in ("Cash", "Card", "Check"):
            self.assertEqual(self._count("pos.payment.method", label), 1, f"method {label!r} not duplicated")
