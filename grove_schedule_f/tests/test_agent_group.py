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

    def test_agent_cannot_reconcile_or_unreconcile_journal_items(self):
        invoice = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": "out_invoice",
                    "partner_id": self.partner.id,
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Chestnut seedling",
                                "quantity": 1,
                                "price_unit": 40.0,
                                "account_id": self.company._grove_account("4020").id,
                                "tax_ids": [Command.clear()],
                            }
                        )
                    ],
                }
            )
        )
        invoice.action_post()
        receivable = invoice.line_ids.account_id.filtered(lambda a: a.account_type == "asset_receivable")
        self.assertEqual(len(receivable), 1)
        payment = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self.misc.id,
                    "date": fields.Date.today(),
                    "line_ids": [
                        Command.create(
                            {"account_id": self.company._grove_account("1350").id, "debit": 40.0, "name": "pay"}
                        ),
                        Command.create(
                            {
                                "account_id": receivable.id,
                                "partner_id": self.partner.id,
                                "credit": 40.0,
                                "name": "pay",
                            }
                        ),
                    ],
                }
            )
        )
        payment.action_post()
        inv_line = invoice.line_ids.filtered(lambda ln: ln.account_id == receivable)
        pay_line = payment.line_ids.filtered(lambda ln: ln.account_id == receivable)
        with self.assertRaises(AccessError):
            self._agent(inv_line | pay_line).reconcile()
        with self.assertRaises(AccessError):
            self._agent(invoice).js_assign_outstanding_line(pay_line.id)
        self.assertFalse(inv_line.reconciled)
        (inv_line | pay_line).reconcile()
        partial = inv_line.matched_credit_ids
        self.assertTrue(partial)
        with self.assertRaises(AccessError):
            self._agent(inv_line).remove_move_reconcile()
        with self.assertRaises(AccessError):
            self._agent(invoice).js_remove_outstanding_partial(partial.id)
        self.assertTrue(inv_line.reconciled)
        self.assertEqual(inv_line.matched_credit_ids, partial)

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

    def test_agent_cannot_arm_auto_post(self):
        today = fields.Date.today()
        vals = {
            "move_type": "entry",
            "journal_id": self.misc.id,
            "date": today,
            "auto_post": "at_date",
            "line_ids": [
                Command.create({"account_id": self.acc_6280.id, "debit": 5.0, "name": "d"}),
                Command.create({"account_id": self.company._grove_account("1350").id, "credit": 5.0, "name": "c"}),
            ],
        }
        move = self.env["account.move"].with_user(self.agent).with_company(self.company).create(vals)
        self.assertEqual(move.auto_post, "no")
        for key, value in (("auto_post", "at_date"), ("auto_post_until", today)):
            with self.assertRaises(AccessError):
                self._agent(move).write({key: value})
        self.assertEqual(move.auto_post, "no")

    def test_agent_line_context_default_and_reassignment_bypasses(self):
        draft = self._entry(post=False)
        posted = self._entry(post=True)
        Line = self.env["account.move.line"].with_user(self.agent).with_company(self.company)
        vals = {"account_id": self.acc_6280.id, "debit": 1.0, "name": "x"}
        with self.assertRaises(AccessError):
            Line.with_context(default_move_id=posted.id).create(vals)
        with self.assertRaises(AccessError):
            self._agent(draft.line_ids[0]).write({"move_id": posted.id})
        self.assertEqual(draft.line_ids[0].move_id, draft)

    def test_agent_cannot_touch_owner_auto_post_armed_draft(self):
        armed = self._entry(post=False)
        armed.auto_post = "at_date"
        with self.assertRaises(AccessError):
            self._agent(armed).write({"ref": "x"})
        with self.assertRaises(AccessError):
            self._agent(armed.line_ids[0]).write({"name": "y"})
        vals = {"account_id": self.acc_6280.id, "debit": 1.0, "name": "x", "move_id": armed.id}
        with self.assertRaises(AccessError):
            self.env["account.move.line"].with_user(self.agent).with_company(self.company).create(vals)

    def test_agent_cannot_use_context_default_trigger(self):
        Model = self.env["account.reconcile.model"].with_user(self.agent).with_company(self.company)
        with self.assertRaises(AccessError):
            Model.with_context(default_trigger="auto_reconcile").create({"name": "ctx auto"})

    def test_agent_cannot_edit_or_delete_auto_model_lines(self):
        Model = self.env["account.reconcile.model"].with_company(self.company)
        line_vals = [
            Command.create({"account_id": self.acc_6280.id, "amount_type": "percentage", "amount_string": "100"})
        ]
        auto = Model.create({"name": "owner auto", "trigger": "auto_reconcile", "line_ids": line_vals})
        manual = Model.create({"name": "owner manual", "line_ids": line_vals})
        other = self.company._grove_account("1350")
        with self.assertRaises(AccessError):
            self._agent(auto.line_ids).write({"account_id": other.id})
        with self.assertRaises(AccessError):
            self._agent(auto).write({"name": "renamed"})
        with self.assertRaises(AccessError):
            self._agent(auto.line_ids).unlink()
        with self.assertRaises(AccessError):
            self._agent(auto).unlink()
        self._agent(manual.line_ids).write({"account_id": other.id})
        self.assertEqual(manual.line_ids.account_id, other)

    def test_agent_cannot_rewrite_an_accepted_proposal(self):
        self.line.grove_proposal_state = "accepted"
        with self.assertRaisesRegex(AccessError, "already accepted"):
            self._agent(self.line).write({"grove_proposal_note": "again"})
        with self.assertRaisesRegex(AccessError, "already accepted"):
            self._agent(self.line).write({"grove_proposal_state": "proposed"})
        self.assertEqual(self.line.grove_proposal_state, "accepted")

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
