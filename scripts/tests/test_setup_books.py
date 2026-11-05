"""Pure tests for the books setup script (no Odoo, no network)."""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "setup_books.py")
_spec = importlib.util.spec_from_file_location("setup_books", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

READ_METHODS = {"search", "read", "search_read", "search_count", "fields_get"}


class TestJournalSpecs(unittest.TestCase):
    def test_farm_journals_include_shared_bank_flag_and_owner_paid(self):
        by_code = {j["code"]: j for j in mod.journal_specs("farm")}
        self.assertEqual(set(by_code), {"BNK1", "STRP", "OWNR"})
        self.assertEqual(by_code["BNK1"]["name"], "Shared bank (2026)")
        self.assertTrue(by_code["BNK1"]["grove_enterprise_required"])
        self.assertEqual(by_code["STRP"]["name"], "Stripe (farm)")
        self.assertEqual(by_code["STRP"]["type"], "bank")
        self.assertEqual(by_code["OWNR"]["type"], "bank")
        self.assertEqual(by_code["OWNR"]["default_account_code"], "2900")

    def test_nursery_journals_cover_every_channel(self):
        specs = {j["code"]: j for j in mod.journal_specs("nursery")}
        self.assertEqual(set(specs), {"INV", "BILL", "MISC", "STRP", "SQR", "VENM", "CSH"})
        self.assertEqual(specs["INV"]["type"], "sale")
        self.assertEqual(specs["BILL"]["type"], "purchase")
        self.assertEqual(specs["MISC"]["type"], "general")
        self.assertEqual(specs["SQR"]["type"], "bank")
        self.assertEqual(specs["VENM"]["type"], "cash")
        self.assertEqual(specs["CSH"]["type"], "cash")
        self.assertEqual(specs["STRP"]["name"], "Stripe (nursery)")

    def test_unknown_company_key_raises(self):
        with self.assertRaises(KeyError):
            mod.journal_specs("woodworking")


class TestCategoryMapping(unittest.TestCase):
    def test_category_mapping(self):
        self.assertEqual(mod.category_income_account("Chestnut Trees"), "4020")
        self.assertEqual(mod.category_income_account("T-shirts"), "4010")
        self.assertEqual(mod.category_income_account("Seeds"), "4020")
        self.assertEqual(mod.category_income_account("STICKERS"), "4010")
        self.assertEqual(mod.category_income_account("Merch"), "4010")
        self.assertEqual(mod.category_income_account("Anything else"), "4020")

    def test_plant_words_win_over_pot(self):
        self.assertEqual(mod.category_income_account("Potted Trees"), "4020")


class TestProviderRepoint(unittest.TestCase):
    JOURNALS = {"STRP": 77, "SQR": 78}

    def _p(self, pid, company, state="enabled", code="stripe", journal=10):
        return {"id": pid, "company_id": [company, "x"], "state": state, "code": code, "journal_id": [journal, "j"]}

    def test_provider_repoint_only_touches_enabled_nursery_stripe(self):
        providers = [
            self._p(24, 3),
            self._p(18, 1, state="disabled"),
            self._p(30, 1),  # farm provider, enabled
            self._p(31, 3, code="paypal"),
            self._p(32, 3, state="test"),
        ]
        plan = mod.provider_repoint_plan(providers, self.JOURNALS, nursery_company_id=3)
        self.assertEqual(plan, [{"provider_id": 24, "journal_id": 77}])

    def test_already_pointed_is_noop(self):
        self.assertEqual(mod.provider_repoint_plan([self._p(24, 3, journal=77)], self.JOURNALS, 3), [])

    def test_missing_strp_journal_plans_nothing(self):
        self.assertEqual(mod.provider_repoint_plan([self._p(24, 3)], {}, 3), [])


class TestDryRunWritesNothing(unittest.TestCase):
    def test_dry_run_issues_no_write_calls(self):
        seen = []

        def fake_call(models, uid, model, method, args, kwargs=None):
            seen.append((model, method))
            if model == "res.company" and method == "search":
                name = args[0][0][2]
                return [{"Goldberry Grove Farm": 1, "At The Grove Nursery": 3}[name]]
            if model == "account.move":
                return [{"id": 5, "name": "INV/2026/00001", "ref": "test", "company_id": [1, "f"]}]
            if model == "payment.provider":
                return [
                    {"id": 24, "company_id": [3, "n"], "state": "enabled", "code": "stripe", "journal_id": [10, "b"]}
                ]
            if model == "ir.model.data":
                return [{"res_id": 99}]
            if model == "res.company" and method == "read":
                return [{"grove_owns_shared_bank": False}]
            if method == "search":
                return []
            if method in ("search_read", "read"):
                return []
            return None

        orig = mod.call
        mod.call = fake_call
        try:
            args = mod.parse_args(["--dry-run", "--reset-test-entries", "--reset-ref-pattern", "test"])
            mod.run(object(), 1, args)
        finally:
            mod.call = orig
        writes = [(m, meth) for m, meth in seen if meth not in READ_METHODS]
        self.assertEqual(writes, [])
        self.assertTrue(seen)

    def _harness(self, moves, journals=None):
        seen = []

        def fake_call(models, uid, model, method, args, kwargs=None):
            seen.append((model, method, kwargs))
            if model == "res.company" and method == "search":
                return [{"Goldberry Grove Farm": 1, "At The Grove Nursery": 3}[args[0][0][2]]]
            if model == "account.move" and method == "search_read":
                return moves
            if model == "res.company" and method == "read":
                return [{"grove_owns_shared_bank": True}]
            if model == "account.journal" and method == "search":
                return journals if journals is not None else [1]
            if model == "ir.model.data":
                return [{"res_id": 99}]
            return [] if method in ("search", "search_read", "read") else None

        return seen, fake_call

    def _run(self, fake_call, argv):
        orig = mod.call
        mod.call = fake_call
        try:
            mod.run(object(), 1, mod.parse_args(argv))
        finally:
            mod.call = orig

    def test_reset_unlink_carries_force_delete(self):
        moves = [{"id": 5, "name": "INV/1", "ref": "zz-test", "company_id": [1, "f"]}]
        seen, fake = self._harness(moves)
        self._run(fake, ["--reset-test-entries", "--reset-ref-pattern", "zz-test"])
        unlinks = [k for m, meth, k in seen if meth == "unlink"]
        self.assertEqual(len(unlinks), 1)
        self.assertTrue(unlinks[0]["context"]["force_delete"])
        self.assertIn("button_draft", [meth for _, meth, _ in seen])

    def test_missing_bnk1_aborts_real_run_without_create(self):
        seen, fake = self._harness([], journals=[])
        with self.assertRaises(SystemExit):
            self._run(fake, [])
        self.assertNotIn("create", [meth for _, meth, _ in seen])

    def test_broad_reset_pattern_refused(self):
        moves = [{"id": i, "name": f"M{i}", "ref": "", "company_id": [1, "f"]} for i in range(11)]
        seen, fake = self._harness(moves)
        with self.assertRaises(SystemExit):
            self._run(fake, ["--reset-test-entries", "--reset-ref-pattern", "M"])
        with self.assertRaises(SystemExit):
            self._run(fake, ["--reset-test-entries", "--reset-ref-pattern", ".*"])
        self.assertNotIn("button_draft", [meth for _, meth, _ in seen])

    def test_reset_requires_pattern(self):
        args = mod.parse_args(["--dry-run", "--reset-test-entries"])
        with self.assertRaises(SystemExit):
            mod.run(object(), 1, args)


if __name__ == "__main__":
    unittest.main()
