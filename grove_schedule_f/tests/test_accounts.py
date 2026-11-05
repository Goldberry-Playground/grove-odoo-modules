from odoo.addons.grove_schedule_f import schedule_f_lines
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestAccounts(TransactionCase):
    def _company(self, name="Setup Co", generic_coa=False):
        company = self.env["res.company"].create({"name": name, "country_id": self.env.ref("base.us").id})
        if generic_coa:
            self.env["account.chart.template"].try_loading("generic_coa", company, install_demo=False)
        return company

    def test_setup_creates_every_mapped_account_once(self):
        company = self._company()
        first = company.grove_schedule_f_setup()
        self.assertEqual(first["accounts_created"], len(schedule_f_lines.ACCOUNTS))
        second = company.grove_schedule_f_setup()
        self.assertEqual(second["accounts_created"], 0)
        self.assertEqual(second["accounts_existing"], len(schedule_f_lines.ACCOUNTS))
        self.assertEqual(second["tags_created"], 0)

    def test_mapped_accounts_carry_line_and_tag(self):
        company = self._company()
        company.grove_schedule_f_setup()
        acc = company._grove_account("6280")
        self.assertEqual(acc.grove_schedule_f_line, "28")
        self.assertIn("Schedule F 28", acc.tag_ids.mapped("name"))
        self.assertFalse(company._grove_account("3100").grove_schedule_f_line)

    def test_tags_are_shared_across_companies(self):
        first = self._company("Tag Co A")
        second = self._company("Tag Co B")
        first.grove_schedule_f_setup()
        second.grove_schedule_f_setup()
        tags = self.env["account.account.tag"].search([("name", "=", "Schedule F 28")])
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags.applicability, "accounts")

    def test_missing_account_raises_naming_code(self):
        company = self._company()
        with self.assertRaisesRegex(UserError, "6280"):
            company._grove_account("6280")

    def test_journal_defaults_point_at_schedule_f(self):
        company = self._company(generic_coa=True)
        company.grove_schedule_f_setup()
        sale = self.env["account.journal"].search([("company_id", "=", company.id), ("type", "=", "sale")], limit=1)
        purchase = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "purchase")], limit=1
        )
        self.assertTrue(sale and purchase)
        self.assertEqual(sale.default_account_id, company._grove_account("4020"))
        self.assertEqual(purchase.default_account_id, company._grove_account("6320"))

    def test_generic_catch_alls_are_deprecated(self):
        company = self._company(generic_coa=True)
        accounts = self.env["account.account"].with_company(company).with_context(active_test=False)
        catch_alls = accounts.search([("company_ids", "in", company.id), ("name", "in", ["Product Sales", "Expenses"])])
        self.assertTrue(catch_alls)
        result = company.grove_schedule_f_setup()
        self.assertTrue(all(catch_alls.mapped(lambda a: not a.active)))
        self.assertEqual(sorted(result["deprecated"]), sorted(catch_alls.mapped("code")))

    def test_protected_catch_all_stays_active(self):
        company = self._company(generic_coa=True)
        expenses = (
            self.env["account.account"]
            .with_company(company)
            .search([("company_ids", "in", company.id), ("name", "=", "Expenses")], limit=1)
        )
        general = self.env["account.journal"].create(
            {
                "name": "Keep Expenses",
                "code": "KEEP",
                "type": "general",
                "company_id": company.id,
                "default_account_id": expenses.id,
            }
        )
        result = company.grove_schedule_f_setup()
        self.assertTrue(expenses.active)
        self.assertEqual(general.default_account_id, expenses)
        self.assertNotIn(expenses.code, result["deprecated"])
        sale = self.env["account.journal"].search([("company_id", "=", company.id), ("type", "=", "sale")], limit=1)
        purchase = self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "purchase")], limit=1
        )
        self.assertEqual(sale.default_account_id, company._grove_account("4020"))
        self.assertEqual(purchase.default_account_id, company._grove_account("6320"))

    def test_product_category_defaults_repointed_and_refinements_kept(self):
        company = self._company(generic_coa=True)
        company.grove_schedule_f_setup()
        Category = self.env["product.category"].with_company(company)
        refined = Category.create({"name": "Refined"})
        generic = Category.create({"name": "Generic"})
        refined.property_account_expense_categ_id = company._grove_account("6280")
        result = company.grove_schedule_f_setup()
        self.assertEqual(result["categories_repointed"], 2)  # Generic (both) and Refined (income only)
        for category in Category.search([]):
            self.assertEqual(category.property_account_income_categ_id, company._grove_account("4020"))
        self.assertEqual(generic.property_account_expense_categ_id, company._grove_account("6320"))
        self.assertEqual(refined.property_account_expense_categ_id, company._grove_account("6280"))
        self.assertEqual(company.grove_schedule_f_setup()["categories_repointed"], 0)
