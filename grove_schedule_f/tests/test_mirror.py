from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestMirror(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Company = cls.env["res.company"]
        us = cls.env.ref("base.us")
        cls.farm_co = Company.create({"name": "Mirror Farm", "country_id": us.id})
        cls.nursery_co = Company.create({"name": "Mirror Nursery", "country_id": us.id})
        for company in (cls.farm_co, cls.nursery_co):
            cls.env["account.chart.template"].try_loading("generic_coa", company, install_demo=False)
        cls.farm_co.grove_owns_shared_bank = True
        cls.setup_result = cls.farm_co.grove_schedule_f_setup()
        cls.nursery_co.grove_schedule_f_setup()
        cls.bank = (
            cls.env["account.journal"]
            .with_company(cls.farm_co)
            .create(
                {
                    "name": "Mirror Shared Bank",
                    "code": "MSHB",
                    "type": "bank",
                    "company_id": cls.farm_co.id,
                    "grove_enterprise_required": True,
                }
            )
        )
        plan = cls.env["account.analytic.plan"].search([("name", "=", "Enterprise")])
        by_name = {a.name: a for a in plan.account_ids.filtered(lambda a: a.company_id == cls.farm_co)}
        cls.farm_tag, cls.nursery_tag, cls.personal_tag = by_name["Farm"], by_name["Nursery"], by_name["Personal"]
        cls.Run = cls.env["grove.mirror.run"]

    def _post_bank_entry(self, code, tag, amount, day=10, month=3):
        company = self.farm_co
        move = (
            self.env["account.move"]
            .with_company(company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self.bank.id,
                    "date": date(2026, month, day),
                    "line_ids": [
                        Command.create(
                            {
                                "account_id": company._grove_account(code).id,
                                "debit": amount,
                                "name": "expense",
                                "analytic_distribution": {str(tag.id): 100},
                            }
                        ),
                        Command.create(
                            {"account_id": self.bank.default_account_id.id, "credit": amount, "name": "bank"}
                        ),
                    ],
                }
            )
        )
        move.action_post()
        return move

    def _seed_march(self):
        self._post_bank_entry("6280", self.nursery_tag, 100.0, day=3)
        self._post_bank_entry("6280", self.nursery_tag, 25.0, day=12)
        self._post_bank_entry("6260", self.nursery_tag, 40.0, day=20)
        self._post_bank_entry("6280", self.farm_tag, 10.0, day=21)
        self._post_bank_entry("3100", self.personal_tag, 55.0, day=22)

    def _cash_total(self, company, line, year, enterprise=None):
        self.env.flush_all()
        domain = [
            ("company_id", "=", company.id),
            ("schedule_f_line", "=", line),
            ("cash_date", ">=", date(year, 1, 1)),
            ("cash_date", "<=", date(year, 12, 31)),
        ]
        if enterprise:
            domain.append(("enterprise", "=", enterprise))
        return sum(self.env["grove.schedulef.cashline"].search(domain).mapped("amount"))

    def _farm_cash_total(self, line, enterprise, year):
        return self._cash_total(self.farm_co, line, year, enterprise)

    def _nursery_cash_total(self, line, year):
        return self._cash_total(self.nursery_co, line, year)

    def _run(self):
        return self.Run.grove_run_month(date(2026, 3, 1), self.farm_co, self.nursery_co)

    def test_setup_creates_mirror_journal(self):
        self.assertIn("mirror_journal_created", self.setup_result)
        for company in (self.farm_co, self.nursery_co):
            journal = self.env["account.journal"].search([("company_id", "=", company.id), ("code", "=", "MIRR")])
            self.assertEqual(len(journal), 1)
            self.assertTrue(journal.grove_is_mirror)
            self.assertEqual(journal.type, "general")
        self.assertFalse(self.farm_co.grove_schedule_f_setup()["mirror_journal_created"])

    def test_run_mirrors_nursery_tagged_expenses_and_zeroes_the_farm(self):
        self._seed_march()
        self.assertEqual(self._farm_cash_total("28", "nursery", 2026), 125.0)
        run = self._run()
        self.assertEqual(run.state, "posted")
        self.assertEqual(run.summary, [{"code": "6260", "amount": 40.0}, {"code": "6280", "amount": 125.0}])
        self.assertEqual(self._farm_cash_total("28", "nursery", 2026), 0.0)
        self.assertEqual(self._nursery_cash_total("28", 2026), 125.0)
        source, target = run.source_move_id, run.target_move_id
        self.assertEqual(source.company_id, self.farm_co)
        self.assertEqual(target.company_id, self.nursery_co)
        self.assertEqual(source.state, "posted")
        self.assertEqual(target.state, "posted")
        self.assertEqual(source.date, date(2026, 3, 31))
        self.assertEqual(source.ref, "MIRROR 2026-03 nursery")
        self.assertEqual(target.ref, "MIRROR 2026-03 nursery")
        self.assertEqual(source.journal_id.code, "MIRR")
        self.assertEqual(
            source.line_ids.filtered(lambda ln: ln.account_id.with_company(self.farm_co).code == "1350").debit, 165.0
        )
        self.assertEqual(
            target.line_ids.filtered(lambda ln: ln.account_id.with_company(self.nursery_co).code == "1350").credit,
            165.0,
        )
        self.assertEqual(sum(source.line_ids.mapped("balance")), 0.0)
        self.assertEqual(sum(target.line_ids.mapped("balance")), 0.0)

    def test_second_run_same_month_raises(self):
        self._seed_march()
        self._run()
        with self.assertRaisesRegex(UserError, "already exists"):
            self._run()

    def test_reverse_then_rerun(self):
        self._seed_march()
        run = self._run()
        run.action_reverse()
        self.assertEqual(run.state, "reversed")
        self.assertEqual(self._farm_cash_total("28", "nursery", 2026), 125.0)
        again = self._run()
        self.assertEqual(again.state, "posted")
        self.assertEqual(self._farm_cash_total("28", "nursery", 2026), 0.0)

    def test_retag_after_run_marks_stale(self):
        self._seed_march()
        run = self._run()
        run.action_check()
        self.assertFalse(run.is_stale)
        self._post_bank_entry("6280", self.nursery_tag, 7.0, day=25)
        run.action_check()
        self.assertTrue(run.is_stale)

    def test_farm_tagged_and_personal_lines_are_ignored(self):
        self._post_bank_entry("6280", self.farm_tag, 10.0)
        self._post_bank_entry("3100", self.personal_tag, 55.0)
        self.assertEqual(self.Run._grove_collect(date(2026, 3, 1), self.farm_co), {})
        run = self._run()
        self.assertEqual(run.state, "posted")
        self.assertFalse(run.summary)
        self.assertFalse(run.source_move_id)
        self.assertFalse(run.target_move_id)

    def test_other_month_lines_are_ignored(self):
        self._post_bank_entry("6280", self.nursery_tag, 30.0, day=5, month=4)
        self.assertEqual(self.Run._grove_collect(date(2026, 3, 1), self.farm_co), {})
        self.assertEqual(self.Run._grove_collect(date(2026, 4, 1), self.farm_co), {"6280": 30.0})
