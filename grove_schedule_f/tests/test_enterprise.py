from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestEnterprise(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Enterprise Co", "country_id": cls.env.ref("base.us").id})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company, install_demo=False)
        cls.company.grove_owns_shared_bank = True
        cls.company.grove_schedule_f_setup()
        Journal = cls.env["account.journal"].with_company(cls.company)
        cls.bank = Journal.create(
            {
                "name": "Shared Bank",
                "code": "GSHB",
                "type": "bank",
                "company_id": cls.company.id,
                "grove_enterprise_required": True,
            }
        )
        cls.misc = Journal.create(
            {"name": "Plain Misc", "code": "GPLM", "type": "general", "company_id": cls.company.id}
        )
        plan = cls.env["account.analytic.plan"].search([("name", "=", "Enterprise")])
        by_name = {a.name: a for a in plan.account_ids.filtered(lambda a: a.company_id == cls.company)}
        cls.farm, cls.nursery, cls.personal = by_name["Farm"], by_name["Nursery"], by_name["Personal"]

    def _post_entry(self, journal, debit_account, distribution):
        company = self.company
        if journal.type == "bank":
            credit = journal.default_account_id
        else:
            credit = company._grove_account("1350")
        debit_vals = {"account_id": company._grove_account(debit_account).id, "debit": 10.0, "name": "debit"}
        if distribution:
            debit_vals["analytic_distribution"] = {str(k): v for k, v in distribution.items()}
        move = (
            self.env["account.move"]
            .with_company(company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": journal.id,
                    "date": fields.Date.today(),
                    "line_ids": [
                        Command.create(debit_vals),
                        Command.create({"account_id": credit.id, "credit": 10.0, "name": "credit"}),
                    ],
                }
            )
        )
        move.action_post()
        return move

    def test_setup_creates_plan_with_three_accounts(self):
        plan = self.env["account.analytic.plan"].search([("name", "=", "Enterprise")])
        self.assertEqual(len(plan), 1)
        accounts = plan.account_ids.filtered(lambda a: a.company_id == self.company)
        self.assertEqual(sorted(accounts.mapped("name")), ["Farm", "Nursery", "Personal"])
        again = self.company.grove_schedule_f_setup()
        self.assertFalse(again["plan_created"])
        self.assertEqual(self.env["account.analytic.plan"].search_count([("name", "=", "Enterprise")]), 1)
        self.assertEqual(len(plan.account_ids.filtered(lambda a: a.company_id == self.company)), 3)

    def test_bank_counterpart_without_enterprise_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "pick an enterprise"):
            self._post_entry(self.bank, "6280", None)

    def test_bank_counterpart_with_enterprise_posts_and_stores_it(self):
        move = self._post_entry(self.bank, "6280", {self.nursery.id: 100})
        line = move.line_ids.filtered(lambda ln: ln.account_id.code == "6280")
        self.assertEqual(line.grove_enterprise, "nursery")

    def test_suspense_and_liquidity_lines_are_exempt(self):
        line = (
            self.env["account.bank.statement.line"]
            .with_company(self.company)
            .create(
                {
                    "journal_id": self.bank.id,
                    "date": fields.Date.today(),
                    "payment_ref": "Unsorted bank row",
                    "amount": -25.0,
                }
            )
        )
        self.assertEqual(line.move_id.state, "posted")

    def test_personal_must_go_to_owner_draw(self):
        with self.assertRaisesRegex(ValidationError, "3100"):
            self._post_entry(self.bank, "6280", {self.personal.id: 100})
        with self.assertRaisesRegex(ValidationError, "3100"):
            self._post_entry(self.bank, "3100", {self.farm.id: 100})
        move = self._post_entry(self.bank, "3100", {self.personal.id: 100})
        self.assertEqual(move.state, "posted")

    def test_unflagged_journal_is_untouched(self):
        move = self._post_entry(self.misc, "6280", None)
        self.assertEqual(move.state, "posted")

    def test_partial_personal_split_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "pick an enterprise"):
            self._post_entry(self.bank, "6280", {self.personal.id: 40, self.farm.id: 60})

    def test_unknown_enterprise_account_is_rejected(self):
        other = self.env["account.analytic.account"].create(
            {"name": "Other", "plan_id": self.farm.plan_id.id, "company_id": self.company.id}
        )
        with self.assertRaisesRegex(ValidationError, "pick an enterprise"):
            self._post_entry(self.bank, "6280", {other.id: 100})

    def test_payment_outstanding_account_is_exempt(self):
        outstanding = (
            self.env["account.account"]
            .with_company(self.company)
            .create(
                {
                    "name": "Outstanding Receipts",
                    "code": "199901",
                    "account_type": "asset_current",
                    "company_ids": [Command.link(self.company.id)],
                }
            )
        )
        method_line = self.bank.inbound_payment_method_line_ids[:1]
        method_line.payment_account_id = outstanding
        partner = self.env["res.partner"].create({"name": "Payer"})
        payment = (
            self.env["account.payment"]
            .with_company(self.company)
            .create(
                {
                    "payment_type": "inbound",
                    "partner_type": "customer",
                    "partner_id": partner.id,
                    "amount": 10.0,
                    "journal_id": self.bank.id,
                    "payment_method_line_id": method_line.id,
                }
            )
        )
        payment.action_post()
        self.assertEqual(payment.state, "in_process")

    def test_receivable_counterpart_is_exempt(self):
        receivable = self.env["account.account"].search(
            [("company_ids", "in", self.company.id), ("account_type", "=", "asset_receivable")], limit=1
        )
        self.assertTrue(receivable)
        move = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self.bank.id,
                    "date": fields.Date.today(),
                    "line_ids": [
                        Command.create({"account_id": receivable.id, "credit": 10.0, "name": "settle"}),
                        Command.create({"account_id": self.bank.default_account_id.id, "debit": 10.0, "name": "bank"}),
                    ],
                }
            )
        )
        move.action_post()
        self.assertEqual(move.state, "posted")
