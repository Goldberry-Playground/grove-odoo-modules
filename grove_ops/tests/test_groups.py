from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("grove_ops", "post_install", "-at_install")
class TestWorkerScope(TransactionCase):
    """The worker scope (Task 6, s11.2). This is the Done criterion: a worker
    logs hours on their own task but cannot reach Accounting, a sale order, or
    another person's hours."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        cls.worker = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Pat Worker",
                    "login": "pat.worker@grove.test",
                    "email": "pat.worker@grove.test",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, cls.company.ids)],
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_worker").id])],
                }
            )
        )
        cls.worker_employee = cls.env["hr.employee"].create(
            {"name": "Pat Worker", "user_id": cls.worker.id, "company_id": cls.company.id}
        )
        cls.other = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Sam Other",
                    "login": "sam.other@grove.test",
                    "email": "sam.other@grove.test",
                    "groups_id": [(6, 0, [cls.env.ref("grove_ops.group_worker").id])],
                }
            )
        )
        cls.other_employee = cls.env["hr.employee"].create(
            {"name": "Sam Other", "user_id": cls.other.id, "company_id": cls.company.id}
        )
        cls.analytic = cls.env["account.analytic.account"].create(
            {"name": "Packing", "plan_id": cls._analytic_plan().id}
        )
        cls.project = cls.env["project.project"].create(
            {"name": "Order packing", "account_id": cls.analytic.id, "company_id": cls.company.id}
        )
        cls.my_task = cls.env["project.task"].create(
            {"name": "Pack the Tuesday wave", "project_id": cls.project.id, "user_ids": [(6, 0, cls.worker.ids)]}
        )
        cls.other_task = cls.env["project.task"].create(
            {"name": "Pack the Friday wave", "project_id": cls.project.id, "user_ids": [(6, 0, cls.other.ids)]}
        )

    @classmethod
    def _analytic_plan(cls):
        plan = cls.env["account.analytic.plan"].search([], limit=1)
        return plan or cls.env["account.analytic.plan"].create({"name": "Default"})

    def _line_vals(self, employee, task):
        return {
            "name": "Packing",
            "account_id": self.analytic.id,
            "project_id": self.project.id,
            "task_id": task.id,
            "employee_id": employee.id,
            "unit_amount": 3.0,
        }

    def test_worker_logs_own_hours(self):
        line = (
            self.env["account.analytic.line"]
            .with_user(self.worker)
            .create(self._line_vals(self.worker_employee, self.my_task))
        )
        self.assertEqual(line.unit_amount, 3.0)
        self.assertEqual(line.user_id, self.worker)

    def test_worker_cannot_open_accounting(self):
        with self.assertRaises(AccessError):
            self.env["account.move"].with_user(self.worker).search([], limit=1)

    def test_worker_cannot_open_a_sale_order(self):
        # sale is installed in the QA/prod image but not in grove_ops's own
        # dependency tree, so skip where the model is absent.
        if "sale.order" not in self.env:
            self.skipTest("sale not installed in this database")
        with self.assertRaises(AccessError):
            self.env["sale.order"].with_user(self.worker).search([], limit=1)

    def test_worker_cannot_see_another_persons_hours(self):
        other_line = self.env["account.analytic.line"].create(self._line_vals(self.other_employee, self.other_task))
        visible = self.env["account.analytic.line"].with_user(self.worker).search([])
        self.assertNotIn(other_line, visible)

    def test_worker_sees_only_own_tasks(self):
        visible = self.env["project.task"].with_user(self.worker).search([])
        self.assertIn(self.my_task, visible)
        self.assertNotIn(self.other_task, visible)
