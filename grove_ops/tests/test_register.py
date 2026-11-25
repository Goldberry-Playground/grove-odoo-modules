from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_ops", "post_install", "-at_install")
class TestSweatEquityRegister(TransactionCase):
    """The register shows approved hours valued at $21 (Task 7, s11.4), and an
    approved line cannot be edited in place."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        cls.worker = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Rex Register",
                    "login": "rex.register@grove.test",
                    "email": "rex.register@grove.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, cls.company.ids)],
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_lead").id])],
                }
            )
        )
        cls.employee = cls.env["hr.employee"].create(
            {"name": "Rex Register", "user_id": cls.worker.id, "company_id": cls.company.id}
        )
        cls.approver = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Ada Approver",
                    "login": "ada.approver@grove.test",
                    "email": "ada.approver@grove.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, cls.company.ids)],
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_lead").id])],
                }
            )
        )
        cls.env["hr.employee"].create(
            {"name": "Ada Approver", "user_id": cls.approver.id, "company_id": cls.company.id}
        )
        plan = cls.env["account.analytic.plan"].search([], limit=1) or cls.env["account.analytic.plan"].create(
            {"name": "Default"}
        )
        cls.analytic = cls.env["account.analytic.account"].create({"name": "Orchard", "plan_id": plan.id})
        cls.project = cls.env["project.project"].create(
            {"name": "Orchard care", "account_id": cls.analytic.id, "company_id": cls.company.id}
        )
        cls.monday = fields.Date.today() - timedelta(days=fields.Date.today().weekday())
        cls.sheet = cls.env["hr_timesheet.sheet"].create(
            {
                "employee_id": cls.employee.id,
                "company_id": cls.company.id,
                "date_start": cls.monday,
                "date_end": cls.monday + timedelta(days=6),
            }
        )
        cls.line = cls.env["account.analytic.line"].create(
            {
                "name": "Orchard",
                "account_id": cls.analytic.id,
                "project_id": cls.project.id,
                "employee_id": cls.employee.id,
                "unit_amount": 4.0,
                "date": cls.monday,
                "company_id": cls.company.id,
            }
        )

    def _approve(self):
        self.sheet.with_user(self.approver).write({"state": "done"})
        self.sheet.flush_recordset()
        self.env["grove.sweat.equity.register"].invalidate_model()

    def test_register_shows_approved_hours_at_the_rate(self):
        self._approve()
        rows = self.env["grove.sweat.equity.register"].search([("employee_id", "=", self.employee.id)])
        self.assertTrue(rows, "register has no row for the approved week")
        self.assertAlmostEqual(sum(rows.mapped("hours")), 4.0, places=2)
        self.assertAlmostEqual(rows[0].rate, 21.0, places=2)
        self.assertAlmostEqual(sum(rows.mapped("value")), 84.0, places=2)

    def test_unapproved_hours_do_not_show(self):
        rows = self.env["grove.sweat.equity.register"].search([("employee_id", "=", self.employee.id)])
        self.assertFalse(rows, "unapproved hours must not appear in the register")

    def test_approved_line_is_locked(self):
        self._approve()
        with self.assertRaises(UserError):
            self.line.write({"unit_amount": 9.0})

    def test_register_is_readable_by_every_member(self):
        self._approve()
        rows = (
            self.env["grove.sweat.equity.register"]
            .with_user(self.worker)
            .search([("employee_id", "=", self.employee.id)])
        )
        self.assertTrue(rows, "members cannot read the register")
