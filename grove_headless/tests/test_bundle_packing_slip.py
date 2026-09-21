"""The compliance substitution must reach the picking Josh packs from (GOL-2237).

The per-state substitution ENGINE (which components swap, and to what) is unit-
tested in test_bundle_substitution.py. This case guards the last mile the
scoped GOL-2237 ask is about: the note the checkout stamps on the sale order has
to travel onto the delivery transfer (stock.picking) so it prints on the
delivery slip and shows on the transfer the warehouse works — otherwise the
legal composition and the contents of the box diverge in the barn, where no CI
or e2e check catches it.

We drive the real plumbing: set grove_substitution_note on a sale order for a
storable product, confirm it (which spawns the outgoing picking), and assert the
stored-related mirror carried the note through. The engine's own note wording is
covered elsewhere, so a plain sentinel string is enough here.

Runs under Odoo's --test-enable runner (needs a DB for sale.order + stock), so
it is listed in tests/__init__.py AND excluded from pytest in conftest.py — the
double-skip pattern every TransactionCase here follows (GOL-1936).
"""

from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestBundlePackingSlip(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "FL Bundle Customer", "email": "fl-bundle@example.com", "company_id": self.company.id}
        )
        # Storable so confirming the order spawns an outgoing delivery picking
        # (the surface the note must reach), mirroring test_mark_shipped.
        self.product = self.env["product.product"].create(
            {"name": "Remembrance Grove Bundle", "type": "consu", "is_storable": True, "list_price": 99.0}
        )

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def test_substitution_note_mirrors_onto_the_delivery_picking(self):
        note = (
            "COMPLIANCE SUBSTITUTION (Florida) — pack these swaps for this bundle:\n"
            "  • Shagbark Hickory (Carya ovata) IN PLACE OF American Chestnut (Castanea dentata)"
        )
        order = self._order(grove_substitution_note=note)
        order.action_confirm()

        self.assertTrue(order.picking_ids, "Confirming a storable order must spawn a delivery picking")
        for picking in order.picking_ids:
            self.assertEqual(
                picking.grove_substitution_note,
                note,
                "The substitution note must mirror onto the transfer so it prints on the delivery slip",
            )

    def test_no_substitution_leaves_the_picking_note_empty(self):
        # The common case: an order that needed no swap carries no note, so the
        # delivery slip prints unchanged (the report block is t-if'd on it).
        order = self._order()
        order.action_confirm()

        self.assertTrue(order.picking_ids)
        for picking in order.picking_ids:
            self.assertFalse(picking.grove_substitution_note)
