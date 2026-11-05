import ast

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestCashline(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Cashline Co", "country_id": cls.env.ref("base.us").id})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company, install_demo=False)
        cls.company.grove_owns_shared_bank = True
        cls.company.grove_schedule_f_setup()
        Journal = cls.env["account.journal"].with_company(cls.company)
        cls.flagged_bank = Journal.create(
            {
                "name": "Cashline Flagged Bank",
                "code": "GCLF",
                "type": "bank",
                "company_id": cls.company.id,
                "grove_enterprise_required": True,
            }
        )
        cls.pay_bank = Journal.create(
            {"name": "Cashline Pay Bank", "code": "GCLP", "type": "bank", "company_id": cls.company.id}
        )
        plan = cls.env["account.analytic.plan"].search([("name", "=", "Enterprise")])
        by_name = {a.name: a for a in plan.account_ids.filtered(lambda a: a.company_id == cls.company)}
        cls.farm = by_name["Farm"]
        cls.partner = cls.env["res.partner"].create({"name": "Cashline Partner"})
        cls.a4020 = cls.company._grove_account("4020")
        cls.a6240 = cls.company._grove_account("6240")
        cls.a6280 = cls.company._grove_account("6280")

    def _doc(self, move_type, account, amount, invoice_date):
        move = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": move_type,
                    "partner_id": self.partner.id,
                    "invoice_date": invoice_date,
                    "date": invoice_date,
                    "invoice_line_ids": [
                        Command.create({"name": "item", "account_id": account.id, "quantity": 1, "price_unit": amount})
                    ],
                }
            )
        )
        move.action_post()
        return move

    def _pay(self, doc, amount, date):
        inbound = doc.move_type in ("out_invoice", "in_refund")
        payment = (
            self.env["account.payment"]
            .with_company(self.company)
            .create(
                {
                    "payment_type": "inbound" if inbound else "outbound",
                    "partner_type": "customer" if doc.move_type.startswith("out") else "supplier",
                    "partner_id": self.partner.id,
                    "amount": amount,
                    "date": date,
                    "journal_id": self.pay_bank.id,
                }
            )
        )
        payment.action_post()
        lines = payment.move_id.line_ids + doc.line_ids
        lines.filtered(lambda ln: ln.account_id.account_type in ("asset_receivable", "liability_payable")).reconcile()
        return payment

    def _rows(self, **domain):
        self.env.flush_all()
        dom = [("company_id", "=", self.company.id)] + [(k, "=", v) for k, v in domain.items()]
        return self.env["grove.schedulef.cashline"].search(dom)

    def test_bank_line_appears_at_bank_date(self):
        move = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self.flagged_bank.id,
                    "date": "2026-03-04",
                    "line_ids": [
                        Command.create(
                            {
                                "account_id": self.a6280.id,
                                "debit": 42.10,
                                "name": "debit",
                                "analytic_distribution": {str(self.farm.id): 100},
                            }
                        ),
                        Command.create(
                            {
                                "account_id": self.flagged_bank.default_account_id.id,
                                "credit": 42.10,
                                "name": "credit",
                            }
                        ),
                    ],
                }
            )
        )
        move.action_post()
        rows = self._rows(move_id=move.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.cash_date, fields.Date.to_date("2026-03-04"))
        self.assertAlmostEqual(rows.amount, 42.10)
        self.assertEqual(rows.kind, "expense")
        self.assertEqual(rows.enterprise, "farm")
        self.assertEqual(rows.source, "bank")

    def test_bill_paid_in_two_parts_splits_by_payment_date(self):
        bill = self._doc("in_invoice", self.a6240, 1000.0, "2026-12-10")
        self._pay(bill, 400.0, "2026-12-20")
        self._pay(bill, 600.0, "2027-01-05")
        rows = self._rows(move_id=bill.id)
        self.assertEqual(
            sorted((r.cash_date.isoformat(), r.amount) for r in rows),
            [("2026-12-20", 400.0), ("2027-01-05", 600.0)],
        )
        self.assertEqual(set(rows.mapped("source")), {"document"})
        self.assertEqual(set(rows.mapped("kind")), {"expense"})

    def test_single_payment_gives_single_row(self):
        inv = self._doc("out_invoice", self.a4020, 200.0, "2026-05-01")
        self._pay(inv, 200.0, "2026-05-10")
        self.assertEqual(len(self._rows(move_id=inv.id)), 1)

    def test_unpaid_bill_has_no_rows_and_is_listed(self):
        bill = self._doc("in_invoice", self.a6240, 75.0, "2026-11-01")
        self.assertFalse(self._rows(move_id=bill.id))
        action = self.env.ref("grove_schedule_f.action_unpaid_at_year_end")
        listed = self.env["account.move"].search(ast.literal_eval(action.domain))
        self.assertIn(bill, listed)

    def test_refund_flips_sign(self):
        refund = self._doc("out_refund", self.a4020, 50.0, "2026-06-01")
        self._pay(refund, 50.0, "2026-06-03")
        rows = self._rows(move_id=refund.id)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows.amount, -50.0)
        self.assertEqual(rows.kind, "income")

    def test_income_sign_is_positive_for_credits(self):
        inv = self._doc("out_invoice", self.a4020, 200.0, "2026-05-01")
        self._pay(inv, 200.0, "2026-05-10")
        rows = self._rows(move_id=inv.id)
        self.assertAlmostEqual(rows.amount, 200.0)
        self.assertEqual(rows.kind, "income")

    def test_sum_by_line_for_year(self):
        bill = self._doc("in_invoice", self.a6240, 1000.0, "2026-12-10")
        self._pay(bill, 400.0, "2026-12-20")
        self._pay(bill, 600.0, "2027-01-05")
        inv = self._doc("out_invoice", self.a4020, 200.0, "2026-05-01")
        self._pay(inv, 200.0, "2026-05-10")
        refund = self._doc("out_refund", self.a4020, 50.0, "2026-06-01")
        self._pay(refund, 50.0, "2026-06-03")
        self.env.flush_all()
        groups = self.env["grove.schedulef.cashline"]._read_group(
            [
                ("company_id", "=", self.company.id),
                ("cash_date", ">=", "2026-01-01"),
                ("cash_date", "<=", "2026-12-31"),
            ],
            groupby=["schedule_f_line"],
            aggregates=["amount:sum"],
        )
        totals = dict(groups)
        line_4020 = self.a4020.grove_schedule_f_line
        line_6240 = self.a6240.grove_schedule_f_line
        self.assertAlmostEqual(totals[line_4020], 150.0)
        self.assertAlmostEqual(totals[line_6240], 400.0)

    def test_credit_note_offset_emits_no_rows_and_payment_carries_cash(self):
        bill = self._doc("in_invoice", self.a6240, 1000.0, "2026-12-10")
        credit = self._doc("in_refund", self.a6240, 300.0, "2027-01-15")
        (bill.line_ids + credit.line_ids).filtered(
            lambda ln: ln.account_id.account_type == "liability_payable"
        ).reconcile()
        self._pay(bill, 700.0, "2026-12-20")
        rows = self._rows(move_id=bill.id)
        self.assertEqual([(r.cash_date.isoformat(), r.amount) for r in rows], [("2026-12-20", 700.0)])
        self.assertFalse(self._rows(move_id=credit.id))

    def test_rows_hidden_from_other_company_users(self):
        other = self.env["res.company"].create({"name": "Other Cashline Co"})
        user = self.env["res.users"].create(
            {
                "name": "Other Co User",
                "login": "grove_cashline_other",
                "company_id": other.id,
                "company_ids": [Command.set([other.id])],
                "group_ids": [Command.set([self.env.ref("account.group_account_readonly").id])],
            }
        )
        bill = self._doc("in_invoice", self.a6240, 100.0, "2026-05-01")
        self._pay(bill, 100.0, "2026-05-02")
        self.assertTrue(self._rows(move_id=bill.id))
        self.assertFalse(self.env["grove.schedulef.cashline"].with_user(user).search([("move_id", "=", bill.id)]))
