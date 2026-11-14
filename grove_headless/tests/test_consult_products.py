"""Consult service menu products (GOL-3575, spec C.6).

Every seeded product is a $-reference service on the nursery company, sale_ok,
unpublished, gate-exempt and tax-free — and, being a service, never counts as a
tree for the deposit rule, the bundle tree count or the box plan (the same
reasoning and assertion as test_consult_line for product_planting_consult).

DB test (TransactionCase): listed in tests/__init__.py AND the root conftest
collect_ignore_glob so the pure-Python pytest collector skips it (GOL-1936).
"""

from odoo.addons.grove_headless.models import promotions
from odoo.tests import TransactionCase, tagged

# External id -> reference list price from spec C.6 (product_planting_consult is
# covered by test_consult_line; the thirteen new menu products are checked here).
CONSULT_PRODUCTS = {
    "consult_strategy_hour": 100.0,
    "consult_basemaps": 150.0,
    "consult_plan": 800.0,
    "consult_workbook": 2000.0,
    "install_labor_day": 0.0,
    "steward_visit": 0.0,
    "steward_year": 0.0,
    "consult_hour": 100.0,
    "graft_topwork": 0.0,
    "willow_structure": 0.0,
    "travel_mile": 0.76,
    "travel_hour": 100.0,
    "travel_night": 0.0,
}


@tagged("post_install", "-at_install")
class TestConsultProducts(TransactionCase):
    def setUp(self):
        super().setUp()
        self.nursery = self.env.ref("grove_headless.company_nursery")

    def test_each_product_matches_the_spec_table(self):
        for ext_id, price in CONSULT_PRODUCTS.items():
            with self.subTest(product=ext_id):
                tmpl = self.env.ref(f"grove_headless.{ext_id}")
                self.assertEqual(tmpl.type, "service")
                self.assertEqual(tmpl.list_price, price)
                self.assertTrue(tmpl.sale_ok)
                self.assertFalse(tmpl.website_published)
                self.assertTrue(tmpl.grove_gate_exempt)
                self.assertFalse(tmpl.taxes_id)
                self.assertEqual(tmpl.company_id, self.nursery)

    def test_no_consult_product_counts_as_a_tree(self):
        for ext_id in CONSULT_PRODUCTS:
            with self.subTest(product=ext_id):
                variant = self.env.ref(f"grove_headless.{ext_id}").product_variant_id
                # A service is not storable, so the box plan skips it...
                self.assertFalse(variant.is_storable)
                # ...and it carries a zero tree count.
                self.assertEqual(promotions.variant_tree_count(variant), 0)
