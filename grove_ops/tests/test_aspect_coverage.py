from odoo.tests import TransactionCase, tagged


@tagged("grove_ops", "post_install", "-at_install")
class TestAspectCoverage(TransactionCase):
    """Employee aspect-coverage m2m (Task 5): the planner input in s8.3."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Project = cls.env["project.project"]
        cls.aspect_a = Project.create({"name": "Order packing and shipping", "grove_is_aspect": True})
        cls.aspect_b = Project.create({"name": "Hoophouse", "grove_is_aspect": True})
        cls.plain = Project.create({"name": "Footpad regrade", "grove_is_aspect": False})
        cls.employee = cls.env["hr.employee"].create({"name": "Crew Member"})

    def test_employee_covers_several_aspects(self):
        self.employee.grove_aspect_project_ids = self.aspect_a + self.aspect_b
        self.assertEqual(self.employee.grove_aspect_project_ids, self.aspect_a + self.aspect_b)

    def test_domain_excludes_non_aspect_projects(self):
        field = self.employee._fields["grove_aspect_project_ids"]
        domain = field.get_domain_list(self.employee)
        self.assertIn(("grove_is_aspect", "=", True), domain)
        # A non-aspect project is not among the selectable set under that domain.
        selectable = self.env["project.project"].search(domain)
        self.assertIn(self.aspect_a, selectable)
        self.assertNotIn(self.plain, selectable)
