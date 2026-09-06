"""Shared bareroot/potted stock pool (GOL-2031 peat & bagged).

The trees that ship as "Bareroot" during the leafed season ARE the potted
stock — staff de-pot and bag the root ball at packing. So a Bareroot variant
with zero own stock but 30 potted siblings must expose 30 (PDP availability,
checkout full-charge gate, webhook oversell check), while a Potted variant
never borrows from Bareroot. Runs under Odoo's --test-enable runner.
"""

from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSharedPoolQty(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        self.fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        self.v_potted = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": self.fmt.id})
        self.v_bareroot = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": self.fmt.id})
        self.cultivar = self.env["product.attribute"].create({"name": "Cultivar", "create_variant": "always"})
        self.c_meader = self.env["product.attribute.value"].create({"name": "Meader", "attribute_id": self.cultivar.id})
        self.c_early = self.env["product.attribute.value"].create(
            {"name": "Early Golden", "attribute_id": self.cultivar.id}
        )

    def _template(self, cultivar_values):
        return self.env["product.template"].create(
            {
                "name": "Pool Persimmon",
                "type": "consu",
                "is_storable": True,
                "grove_shipping_tier": "potted",
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.v_potted.id, self.v_bareroot.id])]},
                    ),
                    (0, 0, {"attribute_id": self.cultivar.id, "value_ids": [(6, 0, cultivar_values)]}),
                ],
            }
        )

    def _variant(self, tmpl, fmt_name, cultivar_name=None):
        def match(v):
            names = v.product_template_variant_value_ids.mapped("name")
            return fmt_name in names and (cultivar_name is None or cultivar_name in names)

        return tmpl.product_variant_ids.filtered(match)[:1]

    def _stock(self, variant, qty):
        self.env["stock.quant"]._update_available_quantity(variant, self.location, qty)

    def test_bareroot_pool_includes_potted_sibling(self):
        tmpl = self._template([self.c_meader.id])
        potted = self._variant(tmpl, "Potted")
        bareroot = self._variant(tmpl, "Bareroot")
        self._stock(potted, 30)
        self.assertEqual(bareroot.grove_shared_pool_qty("qty_available"), 30)
        self.assertEqual(bareroot.grove_shared_pool_qty("free_qty"), 30)

    def test_pool_sums_own_and_sibling_stock(self):
        tmpl = self._template([self.c_meader.id])
        self._stock(self._variant(tmpl, "Potted"), 30)
        bareroot = self._variant(tmpl, "Bareroot")
        self._stock(bareroot, 5)
        self.assertEqual(bareroot.grove_shared_pool_qty("qty_available"), 35)

    def test_potted_variant_never_borrows(self):
        tmpl = self._template([self.c_meader.id])
        potted = self._variant(tmpl, "Potted")
        self._stock(self._variant(tmpl, "Bareroot"), 5)
        self.assertEqual(potted.grove_shared_pool_qty("qty_available"), 0)

    def test_pool_is_per_cultivar(self):
        # Meader's bareroot must not count Early Golden's potted stock.
        tmpl = self._template([self.c_meader.id, self.c_early.id])
        self._stock(self._variant(tmpl, "Potted", "Early Golden"), 12)
        meader_bareroot = self._variant(tmpl, "Bareroot", "Meader")
        self.assertEqual(meader_bareroot.grove_shared_pool_qty("qty_available"), 0)
        self._stock(self._variant(tmpl, "Potted", "Meader"), 30)
        self.assertEqual(meader_bareroot.grove_shared_pool_qty("qty_available"), 30)

    def test_no_format_axis_is_own_stock_only(self):
        tmpl = self.env["product.template"].create(
            {"name": "Plain Aronia", "type": "consu", "is_storable": True, "grove_shipping_tier": "bareroot"}
        )
        self._stock(tmpl.product_variant_id, 7)
        self.assertEqual(tmpl.product_variant_id.grove_shared_pool_qty("qty_available"), 7)
