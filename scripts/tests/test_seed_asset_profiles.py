"""Pure tests for the asset profile seeds (no Odoo, no network)."""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_asset_profiles.py")
_spec = importlib.util.spec_from_file_location("seed_asset_profiles", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

ACCOUNTS = {"1500": 11, "1590": 12, "6140": 13}


class TestProfileSpecs(unittest.TestCase):
    def test_two_profiles_with_expected_names_and_years(self):
        got = {(s["name"], s["years"]) for s in mod.PROFILE_SPECS}
        self.assertEqual(
            got,
            {("Machinery and equipment, 7 years", 7), ("Land improvements, 15 years", 15)},
        )

    def test_companies_are_farm_and_nursery(self):
        self.assertEqual(set(mod.COMPANY_NAMES), {"farm", "nursery"})


class TestProfileVals(unittest.TestCase):
    def test_vals_use_three_codes_and_misc_journal(self):
        for spec in mod.PROFILE_SPECS:
            vals = mod.profile_vals(spec, ACCOUNTS, 99, 7)
            self.assertEqual(vals["account_asset_id"], 11)
            self.assertEqual(vals["account_depreciation_id"], 12)
            self.assertEqual(vals["account_expense_depreciation_id"], 13)
            self.assertEqual(vals["journal_id"], 99)
            self.assertEqual(vals["company_id"], 7)

    def test_method_time_number_per_spec(self):
        for spec in mod.PROFILE_SPECS:
            vals = mod.profile_vals(spec, ACCOUNTS, 99, 7)
            self.assertEqual(vals["method"], "linear")
            self.assertEqual(vals["method_time"], "year")
            self.assertEqual(vals["method_period"], "year")
            self.assertEqual(vals["method_number"], spec["years"])
            self.assertEqual(vals["name"], spec["name"])

    def test_emitted_keys_are_known_fields(self):
        for spec in mod.PROFILE_SPECS:
            self.assertLessEqual(set(mod.profile_vals(spec, ACCOUNTS, 99, 7)), mod.ASSET_PROFILE_FIELDS)

    def test_journal_code_is_misc(self):
        self.assertEqual(mod.JOURNAL_CODE, "MISC")


if __name__ == "__main__":
    unittest.main()
