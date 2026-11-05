"""Pure tests for the bank CSV backfill import script (no Odoo, no network)."""

import argparse
import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "import_bank_csv.py")
_spec = importlib.util.spec_from_file_location("import_bank_csv", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _args(**kw):
    base = dict(
        date_col="Date",
        amount_col=None,
        debit_col=None,
        credit_col=None,
        desc_col="Description",
        id_col=None,
        partner_col=None,
        date_fmt="%m/%d/%Y",
    )
    base.update(kw)
    return argparse.Namespace(**base)


ROW = {"Date": "03/05/2026", "Amount": "-12.50", "Description": "COFFEE", "Id": "TX123"}


class TestSplitByMonth(unittest.TestCase):
    def test_split_by_month_keeps_order_and_groups(self):
        rows = [
            {"Date": "03/05/2026", "n": "1"},
            {"Date": "02/27/2026", "n": "2"},
            {"Date": "03/01/2026", "n": "3"},
            {"Date": "02/01/2026", "n": "4"},
        ]
        out = mod.split_by_month(rows, "Date", "%m/%d/%Y")
        self.assertEqual(list(out), ["2026-03", "2026-02"])
        self.assertEqual([r["n"] for r in out["2026-03"]], ["1", "3"])
        self.assertEqual([r["n"] for r in out["2026-02"]], ["2", "4"])


class TestUniqueImportId(unittest.TestCase):
    def test_prefers_bank_id_and_is_stable(self):
        self.assertEqual(mod.unique_import_id(ROW, 3, "Id"), "TX123")
        self.assertEqual(mod.unique_import_id(ROW, 3, None), mod.unique_import_id(ROW, 3, None))
        self.assertNotEqual(mod.unique_import_id(ROW, 3, None), mod.unique_import_id(ROW, 4, None))

    def test_blank_bank_id_falls_back_to_hash(self):
        row = dict(ROW, Id="")
        got = mod.unique_import_id(row, 0, "Id")
        self.assertEqual(len(got), 40)
        self.assertNotEqual(got, "")

    def test_hash_uses_date_amount_description_index(self):
        a = mod.unique_import_id(ROW, 1, None, date_col="Date", amount_col="Amount", desc_col="Description")
        b = mod.unique_import_id(
            dict(ROW, Description="TEA"), 1, None, date_col="Date", amount_col="Amount", desc_col="Description"
        )
        self.assertNotEqual(a, b)


class TestMappingVals(unittest.TestCase):
    def test_single_amount(self):
        vals = mod.mapping_vals(_args(amount_col="Amount"))
        self.assertEqual(vals["name"], "Shared bank CSV")
        self.assertEqual(vals["timestamp_column"], "Date")
        self.assertEqual(vals["timestamp_format"], "%m/%d/%Y")
        self.assertEqual(vals["amount_type"], "simple_value")
        self.assertEqual(vals["amount_column"], "Amount")
        self.assertEqual(vals["description_column"], "Description")
        self.assertEqual(vals["transaction_id_column"], "unique_import_id")
        self.assertEqual(vals["delimiter"], "comma")
        self.assertEqual(vals["float_decimal_sep"], "dot")
        self.assertEqual(vals["float_thousands_sep"], "comma")
        self.assertEqual(vals["file_encoding"], "utf-8")
        self.assertNotIn("amount_debit_column", vals)

    def test_debit_credit(self):
        vals = mod.mapping_vals(_args(debit_col="Debit", credit_col="Credit", partner_col="Payee", id_col="Id"))
        self.assertEqual(vals["amount_type"], "distinct_credit_debit")
        self.assertEqual(vals["amount_debit_column"], "Debit")
        self.assertEqual(vals["amount_credit_column"], "Credit")
        self.assertEqual(vals["partner_name_column"], "Payee")
        self.assertNotIn("amount_column", vals)
        # the synthetic column carries the bank id too, so it stays the mapped id column
        self.assertEqual(vals["transaction_id_column"], "unique_import_id")

    def test_requires_amount_or_debit_credit(self):
        with self.assertRaises(ValueError):
            mod.mapping_vals(_args())
        with self.assertRaises(ValueError):
            mod.mapping_vals(_args(amount_col="A", debit_col="D", credit_col="C"))
        with self.assertRaises(ValueError):
            mod.mapping_vals(_args(debit_col="D"))


class TestMonthSelection(unittest.TestCase):
    def test_inclusive_bounds(self):
        months = ["2025-12", "2026-01", "2026-05", "2026-10", "2026-11"]
        self.assertEqual(mod.select_months(months, "2026-01", "2026-10"), ["2026-01", "2026-05", "2026-10"])
        self.assertEqual(mod.select_months(months, None, None), months)


if __name__ == "__main__":
    unittest.main()
