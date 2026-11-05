"""Pure tests for the Square monthly summary importer (no Odoo, no network)."""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "import_square_summary.py")
_spec = importlib.util.spec_from_file_location("import_square_summary", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestSummaryEntry(unittest.TestCase):
    def test_balances_and_dates_month_end(self):
        vals = mod.summary_entry("2026-04", 1234.50, 36.20)
        lines = vals["lines"]
        debits = sum(line["debit"] for line in lines)
        credits = sum(line["credit"] for line in lines)
        self.assertAlmostEqual(debits, credits)
        self.assertEqual(vals["date"], "2026-04-30")
        self.assertEqual(vals["ref"], "SQUARE 2026-04")
        self.assertEqual(vals["journal"], "SQR")
        self.assertEqual(vals["move_type"], "entry")
        self.assertEqual(vals["company"], "nursery")

    def test_lines_use_expected_codes_and_amounts(self):
        by_code = {line["account_code"]: line for line in mod.summary_entry("2026-02", 1000.0, 29.5)["lines"]}
        self.assertEqual(set(by_code), {"SQR", "6321", "4020"})
        self.assertAlmostEqual(by_code["SQR"]["debit"], 970.5)
        self.assertAlmostEqual(by_code["6321"]["debit"], 29.5)
        self.assertAlmostEqual(by_code["4020"]["credit"], 1000.0)

    def test_leap_february_and_december_end(self):
        self.assertEqual(mod.summary_entry("2028-02", 10, 1)["date"], "2028-02-29")
        self.assertEqual(mod.summary_entry("2026-12", 10, 1)["date"], "2026-12-31")

    def test_lookup_replaces_journal_placeholder(self):
        vals = mod.summary_entry("2026-04", 100, 3, journal_default_account_code_lookup=lambda code: "1010")
        self.assertIn("1010", {line["account_code"] for line in vals["lines"]})

    def test_fees_above_gross_rejected(self):
        with self.assertRaises(ValueError):
            mod.summary_entry("2026-04", 10, 11)

    def test_parse_rows_reads_dollars(self):
        rows = mod.parse_rows(["month,gross,fees", "2026-01,1234.50,36.20", '2026-02,"$1,000.00",29.50'])
        self.assertEqual(rows, [("2026-01", 1234.50, 36.20), ("2026-02", 1000.0, 29.5)])

    def test_parse_rows_validates_every_row(self):
        for bad in ("2026-13,10,1", "2026-01,abc,1", "2026-01,10,11", "April,10,1", "2026-01,-5,1"):
            with self.assertRaises(ValueError, msg=bad):
                mod.parse_rows(["month,gross,fees", "2026-01,10,1", bad])


    def test_parse_rows_rejects_non_finite_amounts(self):
        for bad in ("2026-01,nan,1", "2026-01,inf,1", "2026-01,10,nan", "2026-01,-inf,0", "2026-01,Infinity,1"):
            with self.assertRaises(ValueError, msg=bad):
                mod.parse_rows(["month,gross,fees", bad])

    def test_parse_rows_canonicalizes_the_month(self):
        rows = mod.parse_rows(["month,gross,fees", "2026-1,10,1", " 2026-02 ,10,1"])
        self.assertEqual([r[0] for r in rows], ["2026-01", "2026-02"])
        self.assertEqual(mod.summary_entry(*rows[0])["ref"], "SQUARE 2026-01")


if __name__ == "__main__":
    unittest.main()
