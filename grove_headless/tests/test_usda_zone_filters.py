"""Offline consistency checks for the GOL-3056 balance-not-charged filters.

Pure-Python (no DB, no ``odoo`` import): parse the shipped XML directly so the
check runs under plain pytest as well as Odoo's runner. The DB-backed guard that
ties these domains to ``SaleOrder.GROVE_BALANCE_DUE_STATES`` lives in
``test_usda_zone.py`` (TransactionCase); this one proves the three saved filters
and the search-view filter are mutually consistent and all carry the retryable
``settlement_error`` state, so none silently drops shipped-but-unsettled orders.
"""

import ast
import os
import unittest
from xml.etree import ElementTree as ET

_HERE = os.path.dirname(__file__)
_FILTERS_XML = os.path.join(_HERE, "..", "data", "grove_zone_filters.xml")
_SEARCH_XML = os.path.join(_HERE, "..", "views", "grove_zone_views.xml")


def _status_clause(domain):
    """The value list of the ``grove_checkout_status in [...]`` leaf, or None."""
    for leaf in domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == "grove_checkout_status":
            return list(leaf[2])
    return None


def _fulfillment_clause(domain):
    for leaf in domain:
        if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and leaf[0] == "grove_fulfillment":
            return (leaf[1], leaf[2])
    return None


class TestZoneFilterXml(unittest.TestCase):
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
                "user_id_global": fields["user_id"].get("eval") == "False",
            }

    def test_three_filters_present(self):
        self.assertEqual(
            set(self.filters),
            {
                "ir_filter_grove_balance_not_charged",
                "ir_filter_grove_preorders_pickup",
                "ir_filter_grove_preorders_shipping",
            },
        )

    def test_all_status_sets_identical_and_include_retry_state(self):
        sets = {frozenset(_status_clause(f["domain"])) for f in self.filters.values()}
        self.assertEqual(len(sets), 1, "filters disagree on the balance-due status set")
        self.assertEqual(
            sets.pop(),
            frozenset({"deposit_paid", "settlement_failed", "settlement_error"}),
        )

    def test_base_domain_has_state_and_no_fulfillment(self):
        base = self.filters["ir_filter_grove_balance_not_charged"]["domain"]
        self.assertIn(("state", "in", ["sale", "done"]), [tuple(leaf) for leaf in base])
        self.assertIsNone(_fulfillment_clause(base))

    def test_pickup_and_shipping_partition(self):
        self.assertEqual(
            _fulfillment_clause(self.filters["ir_filter_grove_preorders_pickup"]["domain"]),
            ("=", "pickup"),
        )
        self.assertEqual(
            _fulfillment_clause(self.filters["ir_filter_grove_preorders_shipping"]["domain"]),
            ("=", "ship"),
        )

    def test_all_group_by_zone_and_global(self):
        for name, f in self.filters.items():
            self.assertEqual(f["context"].get("group_by"), ["grove_usda_zone"], name)
            self.assertTrue(f["user_id_global"], f"{name} is not a global filter")

    def test_search_view_filter_matches_saved_filters(self):
        """The search-view 'Balance not charged' filter uses the same status set
        and offers a group-by-zone option."""
        root = ET.parse(_SEARCH_XML).getroot()
        domains = [
            ast.literal_eval(f.get("domain"))
            for f in root.iter("filter")
            if f.get("domain") and "grove_checkout_status" in f.get("domain")
        ]
        self.assertTrue(domains, "no balance filter in the search view")
        for dom in domains:
            self.assertEqual(
                set(_status_clause(dom)),
                {"deposit_paid", "settlement_failed", "settlement_error"},
            )
        group_bys = [f.get("context") for f in root.iter("filter") if "group_by" in (f.get("context") or "")]
        self.assertTrue(any("grove_usda_zone" in c for c in group_bys), "no group-by-zone option")


if __name__ == "__main__":
    unittest.main()
