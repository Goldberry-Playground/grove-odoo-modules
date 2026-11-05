"""Pure tests for the reconcile model seeds (no Odoo, no network)."""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_reconcile_models.py")
_spec = importlib.util.spec_from_file_location("seed_reconcile_models", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

KNOWN_CODES = {"1350", "3100"}
ACCOUNTS = {"company_id": 7, "1350": 11, "3100": 22, "journal:STRP": 31, "journal:SQR": 32}
ENTERPRISES = {"nursery": 101, "personal": 102}


class TestModelSpecs(unittest.TestCase):
    def test_specs_name_real_accounts_and_stay_manual(self):
        for spec in mod.MODEL_SPECS:
            self.assertEqual(spec["trigger"], "manual")
            self.assertEqual(spec["match_label"], "contains")
            self.assertIn(spec["account_code"], KNOWN_CODES)

    def test_expected_models_per_company(self):
        names = {(s["company"], s["name"]) for s in mod.MODEL_SPECS}
        self.assertEqual(
            names,
            {
                ("farm", "Stripe payout (nursery)"),
                ("farm", "Square deposit"),
                ("farm", "Venmo cashout"),
                ("farm", "Owner draw"),
                ("nursery", "Stripe payout to shared bank"),
                ("nursery", "Square transfer to bank"),
            },
        )

    def test_model_vals_stripe_has_suffix_label_and_enterprise(self):
        spec = next(s for s in mod.MODEL_SPECS if s["name"] == "Stripe payout (nursery)")
        vals = mod.model_vals(spec, ACCOUNTS, ENTERPRISES)
        self.assertEqual(vals["name"], "Stripe payout (nursery), money in (check amount against both Stripe journals)")
        self.assertEqual(vals["trigger"], "manual")
        self.assertEqual(vals["match_label"], "contains")
        self.assertEqual(vals["match_label_param"], "STRIPE")
        self.assertEqual(vals["company_id"], 7)
        line = vals["line_ids"][0][2]
        self.assertEqual(vals["line_ids"][0][:2], (0, 0))
        self.assertEqual(line["account_id"], 11)
        self.assertEqual(line["amount_type"], "percentage")
        self.assertEqual(line["amount_string"], "100")
        self.assertEqual(line["analytic_distribution"], {"101": 100})
        self.assertIn("two Stripe accounts", line["label"])

    def test_owner_draw_is_amount_paid_to_personal(self):
        spec = next(s for s in mod.MODEL_SPECS if s["name"] == "Owner draw")
        vals = mod.model_vals(spec, ACCOUNTS, ENTERPRISES)
        self.assertEqual(vals["name"], "Owner draw, money out")
        self.assertTrue(vals["line_ids"][0][2]["label"].startswith("money out"))
        self.assertEqual(vals["line_ids"][0][2]["account_id"], 22)
        self.assertEqual(vals["line_ids"][0][2]["analytic_distribution"], {"102": 100})

    def test_no_enterprise_means_no_distribution_and_both_nature(self):
        spec = next(s for s in mod.MODEL_SPECS if s["name"] == "Square transfer to bank")
        vals = mod.model_vals(spec, ACCOUNTS, ENTERPRISES)
        self.assertEqual(vals["name"], "Square transfer to bank")
        self.assertNotIn("analytic_distribution", vals["line_ids"][0][2])

    def test_nursery_models_are_journal_scoped_and_farm_models_are_not(self):
        by_name = {s["name"]: mod.model_vals(s, ACCOUNTS, ENTERPRISES) for s in mod.MODEL_SPECS}
        self.assertEqual(by_name["Stripe payout to shared bank"]["match_journal_ids"], [(6, 0, [31])])
        self.assertEqual(by_name["Square transfer to bank"]["match_journal_ids"], [(6, 0, [32])])
        for spec in mod.MODEL_SPECS:
            if spec["company"] == "farm":
                self.assertNotIn("match_journal_ids", mod.model_vals(spec, ACCOUNTS, ENTERPRISES))

    def test_emitted_keys_are_odoo19_fields(self):
        for spec in mod.MODEL_SPECS:
            vals = mod.model_vals(spec, ACCOUNTS, ENTERPRISES)
            self.assertLessEqual(set(vals), mod.RECONCILE_MODEL_FIELDS)
            self.assertNotIn("rule_type", vals)
            self.assertNotIn("match_nature", vals)
            for _, _, line in vals["line_ids"]:
                self.assertLessEqual(set(line), mod.RECONCILE_MODEL_LINE_FIELDS)


if __name__ == "__main__":
    unittest.main()
