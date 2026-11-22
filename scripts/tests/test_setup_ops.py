"""Pure tests for scripts/setup_ops.py (no Odoo, no network).

The script is loaded by file path so importing it never touches XML-RPC.
"""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "setup_ops.py")
_spec = importlib.util.spec_from_file_location("setup_ops", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestSweatEquityRate(unittest.TestCase):
    def test_rate_is_exactly_21(self):
        self.assertEqual(mod.SWEAT_EQUITY_RATE, 21.00)


class TestEmployeeSpecs(unittest.TestCase):
    def test_one_spec_per_person_per_company(self):
        users = [
            {"name": "Ada Worker", "login": "ada@grove.test", "company_keys": ["farm"]},
            {"name": "Bo Nursery", "login": "bo@grove.test", "company_keys": ["nursery"]},
        ]
        specs = mod.employee_specs(users)
        self.assertEqual(len(specs), 2)
        self.assertEqual({s["login"] for s in specs}, {"ada@grove.test", "bo@grove.test"})

    def test_person_in_two_companies_yields_two_specs_sharing_login(self):
        users = [{"name": "Jo Both", "login": "jo@grove.test", "company_keys": ["farm", "nursery"]}]
        specs = mod.employee_specs(users)
        self.assertEqual(len(specs), 2)
        self.assertEqual({s["login"] for s in specs}, {"jo@grove.test"})
        self.assertEqual({s["company_key"] for s in specs}, {"farm", "nursery"})

    def test_cost_is_the_rate_and_never_a_gl_account(self):
        users = [{"name": "Jo Both", "login": "jo@grove.test", "company_keys": ["woodworking"]}]
        (spec,) = mod.employee_specs(users)
        self.assertEqual(spec["hourly_cost"], mod.SWEAT_EQUITY_RATE)
        # A timesheet line never posts to the GL (s11.4): no account key on the spec.
        self.assertNotIn("account_id", spec)
        self.assertNotIn("general_account_id", spec)

    def test_unknown_company_key_raises(self):
        users = [{"name": "X", "login": "x@grove.test", "company_keys": ["mars"]}]
        with self.assertRaises(ValueError):
            mod.employee_specs(users)


class TestPlatformAnalytic(unittest.TestCase):
    def test_platform_account_under_farm(self):
        spec = mod.platform_analytic_spec()
        self.assertEqual(spec["name"], "Platform")
        self.assertEqual(spec["company_key"], "farm")


class TestAspectSpecs(unittest.TestCase):
    def setUp(self):
        self.specs = mod.aspect_specs()

    def test_one_project_per_aspect_per_company(self):
        # Nursery 6 + Farm 6 + Woodworking 2 = 14 (s8.1 starting list).
        self.assertEqual(len(self.specs), 14)
        by_company = {}
        for s in self.specs:
            by_company.setdefault(s["company_key"], []).append(s["name"])
        self.assertEqual(len(by_company["nursery"]), 6)
        self.assertEqual(len(by_company["farm"]), 6)
        self.assertEqual(len(by_company["woodworking"]), 2)

    def test_every_aspect_flagged_and_has_dedicated_analytic(self):
        for s in self.specs:
            self.assertTrue(s["grove_is_aspect"], s["name"])
            self.assertFalse(s["grove_is_build"], s["name"])
            self.assertTrue(s["analytic_name"], s["name"])

    def test_company_keys_are_known(self):
        self.assertTrue({s["company_key"] for s in self.specs} <= set(mod.COMPANY_NAMES))

    def test_marketing_in_both_farm_and_nursery(self):
        marketing = {s["company_key"] for s in self.specs if s["name"] == "Marketing"}
        self.assertEqual(marketing, {"farm", "nursery"})

    def test_catch_all_in_every_company(self):
        catch = {s["company_key"] for s in self.specs if s["name"] == "Catch-all"}
        self.assertEqual(catch, {"farm", "nursery", "woodworking"})

    def test_specs_are_deterministic(self):
        # Idempotency rests on a stable spec: same names, same order, every run.
        self.assertEqual(self.specs, mod.aspect_specs())


class TestFootpadBuildSpec(unittest.TestCase):
    def setUp(self):
        self.spec = mod.footpad_build_spec()

    def test_build_under_the_nursery(self):
        self.assertEqual(self.spec["company_key"], "nursery")
        self.assertTrue(self.spec["grove_is_build"])
        self.assertFalse(self.spec["grove_is_aspect"])

    def test_exactly_seven_milestones(self):
        self.assertEqual(len(self.spec["milestones"]), 7)

    def test_has_its_own_analytic(self):
        self.assertTrue(self.spec["analytic_name"])

    def test_dates_and_budget_are_placeholders(self):
        # Ship the structure; dates/budget fill on prod from Josh's doc.
        self.assertTrue(all(m["deadline"] is None for m in self.spec["milestones"]))
        self.assertIsNone(self.spec["budget_amount"])


class TestGroveOdooSprintSpec(unittest.TestCase):
    def setUp(self):
        self.spec = mod.grove_odoo_sprint_spec()

    def test_on_the_farm_with_platform_analytic(self):
        self.assertEqual(self.spec["company_key"], "farm")
        self.assertEqual(self.spec["analytic_name"], mod.platform_analytic_spec()["name"])

    def test_neither_aspect_nor_build(self):
        self.assertFalse(self.spec["grove_is_aspect"])
        self.assertFalse(self.spec["grove_is_build"])

    def test_tasks_carry_code_and_pull_request(self):
        self.assertIn("code", self.spec["task_fields"])
        self.assertIn("pull_request_url", self.spec["task_fields"])

    def test_milestones_are_trains(self):
        self.assertIn("Train #6", [m["name"] for m in self.spec["milestones"]])


class TestProjectSeedRows(unittest.TestCase):
    def test_rows_cover_aspects_plus_footpad_plus_sprint(self):
        rows = mod.project_seed_rows()
        # 14 aspects + footpad + sprint.
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[-2]["name"], mod.FOOTPAD_PROJECT_NAME)
        self.assertEqual(rows[-1]["name"], mod.SPRINT_PROJECT_NAME)


if __name__ == "__main__":
    unittest.main()
