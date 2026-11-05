from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestAgentGroup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Agent Co", "country_id": cls.env.ref("base.us").id})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company, install_demo=False)
        cls.company.grove_owns_shared_bank = True
        cls.company.grove_schedule_f_setup()
        cls.group = cls.env.ref("grove_schedule_f.group_grove_agent")
        cls.agent = cls.env["res.users"].create(
            {
                "name": "Penny Agent",
                "login": "grove_agent_test",
                "company_id": cls.company.id,
                "company_ids": [Command.set([cls.company.id])],
                "group_ids": [Command.set([cls.group.id])],
            }
        )
        cls.acc_6280 = cls.company._grove_account("6280")
        cls.bank = (
            cls.env["account.journal"]
            .with_company(cls.company)
            .create({"name": "Agent Bank", "code": "GAGB", "type": "bank", "company_id": cls.company.id})
        )
        cls.misc = (
            cls.env["account.journal"]
            .with_company(cls.company)
            .create({"name": "Agent Misc", "code": "GAGM", "type": "general", "company_id": cls.company.id})
        )
        cls.partner = cls.env["res.partner"].create({"name": "Tractor Supply"})
        cls.line = (
            cls.env["account.bank.statement.line"]
            .with_company(cls.company)
            .create(
                {
                    "journal_id": cls.bank.id,
                    "date": fields.Date.today(),
                    "payment_ref": "TRACTOR SUPPLY",
                    "amount": -25.0,
                }
            )
        )

    def _entry(self, post):
        move = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self.misc.id,
                    "date": fields.Date.today(),
                    "line_ids": [
                        Command.create({"account_id": self.acc_6280.id, "debit": 10.0, "name": "d"}),
                        Command.create(
                            {"account_id": self.company._grove_account("1350").id, "credit": 10.0, "name": "c"}
                        ),
                    ],
                }
            )
        )
        if post:
            move.action_post()
        return move

    def _agent(self, record):
        return record.with_user(self.agent).with_company(self.company)

    def test_agent_can_write_a_proposal_and_state_flips(self):
        self.assertEqual(self.line.grove_proposal_state, "none")
        self._agent(self.line).write(
            {"grove_proposed_account_id": self.acc_6280.id, "grove_proposal_note": "Tractor Supply"}
        )
        self.assertEqual(self.line.grove_proposal_state, "proposed")
        self.assertEqual(self.line.grove_proposed_account_id, self.acc_6280)

    def test_accepted_state_is_not_reset_by_a_proposal_write(self):
        self.line.grove_proposal_state = "accepted"
        self.line.grove_proposal_note = "edited by owner"
        self.assertEqual(self.line.grove_proposal_state, "accepted")

    def test_agent_cannot_change_amount_or_date(self):
        with self.assertRaises(AccessError):
            self._agent(self.line).write({"amount": 1.0})
        with self.assertRaises(AccessError):
            self._agent(self.line).write({"date": "2020-01-01"})
        with self.assertRaises(AccessError):
            self._agent(self.line).write({"grove_proposal_state": "accepted"})
        self.assertEqual(self.line.amount, -25.0)

    def test_agent_cannot_post_or_reconcile(self):
        draft = self._entry(post=False)
        posted = self._entry(post=True)
        with self.assertRaises(AccessError):
            self._agent(draft).action_post()
        with self.assertRaises(AccessError):
            self._agent(posted).write({"ref": "x"})
        with self.assertRaises(AccessError):
            self._agent(posted.line_ids[0]).write({"account_id": self.acc_6280.id})
        with self.assertRaises(AccessError):
            self._agent(self.line).reconcile_bank_line()
        self.assertEqual(draft.state, "draft")

    def test_agent_can_create_a_draft_bill(self):
        bill = (
            self.env["account.move"]
            .with_user(self.agent)
            .with_company(self.company)
            .create(
                {
                    "move_type": "in_invoice",
                    "partner_id": self.partner.id,
                    "state": "posted",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Fencing staples",
                                "quantity": 1,
                                "price_unit": 10.0,
                                "account_id": self.acc_6280.id,
                            }
                        )
                    ],
                }
            )
        )
        self.assertEqual(bill.state, "draft")
        with self.assertRaises(AccessError):
            self._agent(bill).write({"state": "posted"})

    def test_agent_line_create_blocked_on_posted_allowed_on_draft(self):
        draft = self._entry(post=False)
        posted = self._entry(post=True)
        vals = {"account_id": self.acc_6280.id, "debit": 1.0, "name": "extra"}
        Line = self.env["account.move.line"].with_user(self.agent).with_company(self.company)
        with self.assertRaises(AccessError):
            Line.create({**vals, "move_id": posted.id})
        credit = {"account_id": self.company._grove_account("1350").id, "credit": 1.0, "name": "extra c"}
        lines = Line.create([{**vals, "move_id": draft.id}, {**credit, "move_id": draft.id}])
        self.assertEqual(lines.move_id, draft)

    def test_agent_models_stay_manual(self):
        Model = self.env["account.reconcile.model"].with_user(self.agent).with_company(self.company)
        with self.assertRaises(AccessError):
            Model.create({"name": "auto", "trigger": "auto_reconcile"})
        model = Model.create({"name": "manual one"})
        self.assertEqual(model.trigger, "manual")
        with self.assertRaises(AccessError):
            model.write({"trigger": "auto_reconcile"})
        self.assertEqual(model.trigger, "manual")

    def test_owner_is_unaffected(self):
        self.line.write({"amount": -2.0})
        self.assertEqual(self.line.amount, -2.0)
        draft = self._entry(post=False)
        draft.action_post()
        self.assertEqual(draft.state, "posted")
        auto = (
            self.env["account.reconcile.model"]
            .with_company(self.company)
            .create({"name": "owner auto", "trigger": "auto_reconcile"})
        )
        self.assertEqual(auto.trigger, "auto_reconcile")
