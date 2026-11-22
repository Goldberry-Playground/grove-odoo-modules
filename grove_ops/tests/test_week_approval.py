from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_ops", "post_install", "-at_install")
class TestWeekApproval(TransactionCase):
    """Non-self week approval, the approved-line lock, and the trail (Task 7,
    s11.3 / s11.4). The guards live in write(), so these drive the state field
    directly rather than depending on OCA action-method names."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        cls.owner = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Owner Oak",
                    "login": "owner.oak@grove.test",
                    "email": "owner.oak@grove.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, cls.company.ids)],
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_worker").id])],
                }
            )
        )
        cls.owner_emp = cls.env["hr.employee"].create(
            {"name": "Owner Oak", "user_id": cls.owner.id, "company_id": cls.company.id}
        )
        cls.approver = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Lead Linden",
                    "login": "lead.linden@grove.test",
                    "email": "lead.linden@grove.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, cls.company.ids)],
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_lead").id])],
                }
            )
        )
        cls.env["hr.employee"].create({"name": "Lead Linden", "user_id": cls.approver.id, "company_id": cls.company.id})
        monday = fields.Date.today() - timedelta(days=fields.Date.today().weekday())
        cls.sheet = cls.env["hr_timesheet.sheet"].create(
            {
                "employee_id": cls.owner_emp.id,
                "company_id": cls.company.id,
                "date_start": monday,
                "date_end": monday + timedelta(days=6),
            }
        )

    def test_owner_cannot_approve_own_week(self):
        with self.assertRaises(UserError):
            self.sheet.with_user(self.owner).write({"state": "done"})

    def test_other_owner_operator_can_approve(self):
        self.sheet.with_user(self.approver).write({"state": "done"})
        self.assertEqual(self.sheet.state, "done")

    def test_approval_is_written_to_the_trail(self):
        before = len(self.sheet.message_ids)
        self.sheet.with_user(self.approver).write({"state": "done"})
        bodies = self.sheet.message_ids.mapped("body")
        self.assertGreater(len(self.sheet.message_ids), before)
        self.assertTrue(any("approved" in (b or "").lower() for b in bodies))

    def test_reopen_is_written_to_the_trail(self):
        self.sheet.with_user(self.approver).write({"state": "done"})
        self.sheet.with_user(self.approver).write({"state": "draft"})
        bodies = self.sheet.message_ids.mapped("body")
        self.assertTrue(any("reopened" in (b or "").lower() for b in bodies))
