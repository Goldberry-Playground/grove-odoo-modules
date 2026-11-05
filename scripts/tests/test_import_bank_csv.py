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

    def _ids(self, rows):
        return mod.assign_import_ids(rows, None, "Date", ["Amount"], "Description")

    def test_overlapping_exports_share_ids(self):
        def day(d, desc="SHOP", amount="-1.00"):
            return {"Date": f"03/{d:02d}/2026", "Amount": amount, "Description": desc}

        file_a = [day(d) for d in range(1, 11)] + [day(7, "TWIN")]
        file_b = [day(d) for d in range(5, 16)] + [day(7, "TWIN")]
        ids_a = {(r["Date"], r["Description"]): i for r, i in zip(file_a, self._ids(file_a))}
        ids_b = {(r["Date"], r["Description"]): i for r, i in zip(file_b, self._ids(file_b))}
        shared = set(ids_a) & set(ids_b)
        self.assertEqual(len(shared), 7)  # days 5-10 plus the TWIN row
        for key in shared:
            self.assertEqual(ids_a[key], ids_b[key], key)

    def test_identical_rows_on_one_day_get_distinct_ids_in_both_files(self):
        twin = {"Date": "03/07/2026", "Amount": "-4.00", "Description": "COFFEE"}
        other = {"Date": "03/06/2026", "Amount": "-9.00", "Description": "FEED"}
        file_a = [dict(twin), dict(twin)]
        file_b = [dict(other), dict(twin), {"Date": "03/08/2026", "Amount": "-1", "Description": "X"}, dict(twin)]
        ids_a = self._ids(file_a)
        ids_b = self._ids(file_b)
        self.assertNotEqual(ids_a[0], ids_a[1])
        self.assertEqual(ids_a, [ids_b[1], ids_b[3]])

    def test_bank_id_wins_over_occurrence_hash(self):
        rows = [dict(ROW), dict(ROW, Id="")]
        ids = mod.assign_import_ids(rows, "Id", "Date", ["Amount"], "Description")
        self.assertEqual(ids[0], "TX123")
        self.assertEqual(len(ids[1]), 40)

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


class TestDomainAndDates(unittest.TestCase):
    def test_ids_domain_is_or_chain_of_like(self):
        dom = mod.ids_domain(["a", "b", "c"])
        self.assertEqual(dom[:2], ["|", "|"])
        self.assertEqual(dom[2:], [("unique_import_id", "like", i) for i in "abc"])
        self.assertEqual(mod.ids_domain(["a"]), [("unique_import_id", "like", "a")])

    def test_blank_or_bad_date_names_the_row(self):
        with self.assertRaisesRegex(ValueError, "row 3"):
            mod.split_by_month([{"Date": "03/05/2026"}, {"Date": " "}], "Date", "%m/%d/%Y")
        with self.assertRaisesRegex(ValueError, "does not match"):
            mod.split_by_month([{"Date": "2026-03-05"}], "Date", "%m/%d/%Y")

    def test_month_bounds(self):
        self.assertEqual(mod.month_bounds("2028-02"), ("2028-02-01", "2028-02-29"))


if __name__ == "__main__":
    unittest.main()
