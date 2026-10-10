"""Offline consistency checks for the GOL-3257 §8 seed saved filters.

Pure-Python (no DB, no ``odoo`` import): parse the shipped XML directly so the
check runs under plain pytest. Mirrors ``test_usda_zone_filters.py`` for the
zone filters. Asserts both seed filters are global, group by harvest year, and
that "Seed balance not charged" carries the same balance-due status set the zone
filters use (so a seed order that owes a balance is never silently dropped).
"""

import ast
import os
import unittest
from xml.etree import ElementTree as ET

_HERE = os.path.dirname(__file__)
_FILTERS_XML = os.path.join(_HERE, "..", "data", "grove_seed_filters.xml")

# The retryable balance-due set, kept in lockstep with SaleOrder.GROVE_BALANCE_DUE_STATES
# and the zone filters (test_usda_zone_filters).
_BALANCE_DUE = {"deposit_paid", "settlement_failed", "settlement_error"}


def _leaf(domain, field):
    for leaf in domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == field:
            return leaf
    return None


class TestSeedFilterXml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ET.parse(_FILTERS_XML)
        cls.filters = {}
        for rec in tree.getroot().iter("record"):
            if rec.get("model") != "ir.filters":
                continue
            fields = {f.get("name"): f for f in rec.findall("field")}
            cls.filters[rec.get("id")] = {
                "domain": ast.literal_eval(fields["domain"].text),
                "context": ast.literal_eval(fields["context"].text),
                "user_ids_eval": fields["user_ids"].get("eval"),
            }

    def test_both_filters_present(self):
        self.assertEqual(
            set(self.filters),
            {"ir_filter_grove_seed_by_harvest_year", "ir_filter_grove_seed_balance_not_charged"},
        )

    def test_all_global_and_group_by_harvest_year(self):
        for name, f in self.filters.items():
            self.assertEqual(f["context"].get("group_by"), ["grove_seed_harvest_year"], name)
            # Global: user_ids is the empty x2many command [(6, 0, [])].
            self.assertEqual(ast.literal_eval(f["user_ids_eval"]), [(6, 0, [])], name)

    def test_both_scope_to_seed_orders(self):
        for name, f in self.filters.items():
            self.assertEqual(_leaf(f["domain"], "grove_seed_harvest_year"), ("grove_seed_harvest_year", "!=", 0), name)

    def test_balance_filter_uses_full_balance_due_set(self):
        dom = self.filters["ir_filter_grove_seed_balance_not_charged"]["domain"]
        status = _leaf(dom, "grove_checkout_status")
        self.assertIsNotNone(status)
        self.assertEqual(set(status[2]), _BALANCE_DUE)
        self.assertIn(("state", "in", ["sale", "done"]), [tuple(leaf) for leaf in dom])

    def test_by_harvest_year_filter_has_no_status_clause(self):
        # The plain "by harvest year" view spans every state, not just balance-due.
        dom = self.filters["ir_filter_grove_seed_by_harvest_year"]["domain"]
        self.assertIsNone(_leaf(dom, "grove_checkout_status"))


if __name__ == "__main__":
    unittest.main()
