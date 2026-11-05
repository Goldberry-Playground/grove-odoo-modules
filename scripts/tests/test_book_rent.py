"""Pure tests for the annual rent booking script (no Odoo, no network)."""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "book_rent.py")
_spec = importlib.util.spec_from_file_location("book_rent", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestRentDocuments(unittest.TestCase):
    def setUp(self):
        self.d = mod.rent_documents("2026", 1200.0)

    def test_invoice_and_bill_accounts_and_shape(self):
        inv, bill = self.d["farm_invoice"], self.d["nursery_bill"]
        self.assertEqual(inv["move_type"], "out_invoice")
        self.assertEqual(bill["move_type"], "in_invoice")
        self.assertEqual(inv["invoice_line_ids"][0][2]["account_code"], "4080")
        self.assertEqual(bill["invoice_line_ids"][0][2]["account_code"], "6245")
        self.assertEqual(inv["invoice_line_ids"][0][2]["price_unit"], 1200.0)
        self.assertEqual(bill["invoice_line_ids"][0][2]["price_unit"], 1200.0)
        self.assertEqual(inv["partner_company"], "nursery")
        self.assertEqual(bill["partner_company"], "farm")
        for doc in (inv, bill):
            self.assertEqual(doc["invoice_date"], "2026-12-31")
            self.assertEqual(doc["ref"], "RENT 2026")
            self.assertEqual(doc["invoice_line_ids"][0][2]["tax_ids"], [])

    def test_settlements_balance_and_go_through_1350(self):
        for key, side in (("farm_settlement", "RECEIVABLE"), ("nursery_settlement", "PAYABLE")):
            move = self.d[key]
            lines = [cmd[2] for cmd in move["line_ids"]]
            self.assertAlmostEqual(sum(x["debit"] for x in lines), sum(x["credit"] for x in lines))
            codes = {x["account_code"] for x in lines}
            self.assertEqual(codes, {"1350", side})
            self.assertEqual(move["journal"], "MISC")
            self.assertEqual(move["date"], "2026-12-31")
            self.assertEqual(move["ref"], "RENT 2026 settlement")
            self.assertEqual(move["move_type"], "entry")

    def test_settlement_directions(self):
        farm = {c[2]["account_code"]: c[2] for c in self.d["farm_settlement"]["line_ids"]}
        self.assertEqual(farm["1350"]["debit"], 1200.0)
        self.assertEqual(farm["RECEIVABLE"]["credit"], 1200.0)
        nur = {c[2]["account_code"]: c[2] for c in self.d["nursery_settlement"]["line_ids"]}
        self.assertEqual(nur["PAYABLE"]["debit"], 1200.0)
        self.assertEqual(nur["1350"]["credit"], 1200.0)

    def test_refuses_non_positive_amounts(self):
        for bad in (0, 0.0, -5):
            with self.assertRaises(ValueError):
                mod.rent_documents("2026", bad)


if __name__ == "__main__":
    unittest.main()
