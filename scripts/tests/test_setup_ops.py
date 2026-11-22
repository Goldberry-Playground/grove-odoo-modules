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


if __name__ == "__main__":
    unittest.main()
