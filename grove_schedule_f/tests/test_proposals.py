from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestProposals(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Proposal Co", "country_id": cls.env.ref("base.us").id})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company, install_demo=False)
        cls.company.grove_owns_shared_bank = True
        cls.company.grove_schedule_f_setup()
        cls.bank = (
            cls.env["account.journal"]
            .with_company(cls.company)
            .create(
                {
                    "name": "Proposal Bank",
                    "code": "GPRB",
                    "type": "bank",
                    "company_id": cls.company.id,
                    "grove_enterprise_required": True,
                }
            )
        )
        plan = cls.env["account.analytic.plan"].search([("name", "=", "Enterprise")])
        by_name = {a.name: a for a in plan.account_ids.filtered(lambda a: a.company_id == cls.company)}
        cls.farm = by_name["Farm"]
        cls.a6280 = cls.company._grove_account("6280")
        cls.model = (
            cls.env["account.reconcile.model"]
            .with_company(cls.company)
            .create({"name": "Supplies", "company_id": cls.company.id})
        )
        cls.agent = cls.env["res.users"].create(
            {
                "name": "Proposal Agent",
                "login": "grove_proposal_agent",
                "company_id": cls.company.id,
                "company_ids": [Command.set([cls.company.id])],
                "group_ids": [Command.set([cls.env.ref("grove_schedule_f.group_grove_agent").id])],
            }
        )

    def _import_line(self, amount, label):
        return (
            self.env["account.bank.statement.line"]
            .with_company(self.company)
            .create(
                {
                    "journal_id": self.bank.id,
                    "date": fields.Date.today(),
                    "payment_ref": label,
                    "amount": amount,
                }
            )
        )

    def test_accept_reconciles_to_proposed_account_with_enterprise(self):
        line = self._import_line(amount=-42.10, label="TRACTOR SUPPLY")
        line.write({"grove_proposed_account_id": self.a6280.id, "grove_proposed_enterprise_id": self.farm.id})
        line.action_grove_accept_proposal()
        self.assertTrue(line.is_reconciled)
        cp = line.move_id.line_ids.filtered(lambda ln: ln.account_id == self.a6280)
        self.assertEqual(cp.debit, 42.10)
        self.assertEqual(cp.grove_enterprise, "farm")
        suspense = self.bank.suspense_account_id
        self.assertFalse(line.move_id.line_ids.filtered(lambda ln: ln.account_id == suspense and ln.balance))
        self.assertEqual(line.grove_proposal_state, "accepted")

    def test_accept_without_proposal_raises(self):
        line = self._import_line(amount=-5.0, label="NOTHING")
        with self.assertRaises(UserError):
            line.action_grove_accept_proposal()
        line.write({"grove_proposed_account_id": self.a6280.id})
        with self.assertRaisesRegex(UserError, "Pick an enterprise"):
            line.action_grove_accept_proposal()
        self.assertFalse(line.is_reconciled)

    def test_agent_cannot_accept(self):
        line = self._import_line(amount=-5.0, label="AGENT")
        line.write({"grove_proposed_account_id": self.a6280.id, "grove_proposed_enterprise_id": self.farm.id})
        agent_line = line.with_user(self.agent).with_company(self.company)
        with self.assertRaises(AccessError):
            agent_line.action_grove_accept_proposal()
        with self.assertRaises(AccessError):
            agent_line.action_grove_reject_proposal()
        self.assertFalse(line.is_reconciled)
        self.assertEqual(line.grove_proposal_state, "proposed")

    def test_reject_leaves_line_unreconciled(self):
        line = self._import_line(amount=-9.0, label="REJECT")
        line.write({"grove_proposed_account_id": self.a6280.id, "grove_proposed_enterprise_id": self.farm.id})
        line.action_grove_reject_proposal()
        self.assertEqual(line.grove_proposal_state, "rejected")
        self.assertFalse(line.is_reconciled)
        self.assertEqual(line.grove_proposed_account_id, self.a6280)

    def test_needs_review_rules(self):
        line = self._import_line(amount=-12.0, label="SMALL")
        line.write({"grove_proposed_model_id": self.model.id})
        self.assertFalse(line.grove_needs_review)
        line.grove_proposal_uncertain = True
        self.assertTrue(line.grove_needs_review)
        line.grove_proposal_uncertain = False
        line.amount = -600.0
        self.assertTrue(line.grove_needs_review)
        line.amount = -12.0
        line.grove_proposed_model_id = False
        self.assertTrue(line.grove_needs_review)

    def test_needs_review_is_searchable(self):
        Line = self.env["account.bank.statement.line"].with_company(self.company)
        big = self._import_line(amount=-700.0, label="BIG")
        big.grove_proposed_model_id = self.model
        small = self._import_line(amount=-3.0, label="SMALL")
        small.grove_proposed_model_id = self.model
        self.assertIn(big, Line.search([("grove_needs_review", "=", True)]))
        self.assertNotIn(small, Line.search([("grove_needs_review", "=", True)]))
        self.assertIn(small, Line.search([("grove_needs_review", "=", False)]))
        self.assertNotIn(big, Line.search([("grove_needs_review", "=", False)]))

    def _proposed(self, **vals):
        line = self._import_line(amount=-7.0, label="VALIDATE")
        line.write({"grove_proposed_account_id": self.a6280.id, "grove_proposed_enterprise_id": self.farm.id})
        if vals:
            line.write(vals)
        return line

    def test_accept_rejects_account_of_another_company(self):
        other = self.env["res.company"].create({"name": "Other Co", "country_id": self.env.ref("base.us").id})
        self.env["account.chart.template"].try_loading("generic_coa", other, install_demo=False)
        foreign = self.env["account.account"].with_company(other).search([("company_ids", "in", other.id)], limit=1)
        line = self._proposed()
        with self.assertRaises(UserError):
            line.write({"grove_proposed_account_id": foreign.id})
            line.action_grove_accept_proposal()
        self.assertFalse(line.is_reconciled)

    def test_accept_rejects_receivable_account(self):
        receivable = self.env["account.account"].search(
            [("company_ids", "in", self.company.id), ("account_type", "=", "asset_receivable")], limit=1
        )
        line = self._proposed(grove_proposed_account_id=receivable.id)
        with self.assertRaisesRegex(UserError, "Proposed account is not usable"):
            line.action_grove_accept_proposal()
        self.assertFalse(line.is_reconciled)

    def test_accept_rejects_non_enterprise_analytic_account(self):
        plan = self.env["account.analytic.plan"].create({"name": "Other Plan"})
        stray = self.env["account.analytic.account"].create(
            {"name": "Stray", "plan_id": plan.id, "company_id": self.company.id}
        )
        line = self._proposed(grove_proposed_enterprise_id=stray.id)
        with self.assertRaisesRegex(UserError, "Proposed enterprise is not usable"):
            line.action_grove_accept_proposal()
        self.assertFalse(line.is_reconciled)

    def test_reject_on_accepted_line_raises(self):
        line = self._proposed()
        line.action_grove_accept_proposal()
        with self.assertRaises(UserError):
            line.action_grove_reject_proposal()
        self.assertEqual(line.grove_proposal_state, "accepted")
