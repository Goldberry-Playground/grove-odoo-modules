from odoo.tests import TransactionCase, tagged


@tagged("grove_ops", "post_install", "-at_install")
class TestProjectFlags(TransactionCase):
    """The aspect / build flags on project.project (Task 8a model layer)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env["project.project"]

    def test_flags_default_false(self):
        proj = self.Project.create({"name": "Plain project"})
        self.assertFalse(proj.grove_is_aspect)
        self.assertFalse(proj.grove_is_build)

    def test_aspect_and_build_are_settable_and_independent(self):
        aspect = self.Project.create({"name": "Watering and plant care", "grove_is_aspect": True})
        build = self.Project.create({"name": "Footpad regrade", "grove_is_build": True})
        self.assertTrue(aspect.grove_is_aspect)
        self.assertFalse(aspect.grove_is_build)
        self.assertTrue(build.grove_is_build)
        self.assertFalse(build.grove_is_aspect)
