"""GOL-1982 Phase-5 acceptance: ship / pickup / preorder each decrement the
correct inventory pool — and ONLY that pool.

The three fulfilment modes draw on three different pools and must never be
conflated (the parent GOL-1975 gate). Each mode's own behaviour is proven in a
focused sibling suite — the label skip (``test_preorder_label_skip``), the cap
counter (``test_preorder_cap``), the shipment-email owed rule
(``test_shipment_email``), the state machine (``test_fulfillment_state``). What
was missing, and what this suite adds, is the *isolation* cross-check: a ship
order must not consume the preorder cap, a preorder must not consume on-hand,
and a pickup must neither buy a label nor send a shipment email. Those are the
leaks a future refactor could reintroduce, so they are pinned together here
against the module's own two pool gates:

  * on-hand pool  -> ``controllers.main._oversold_lines`` (the free_qty gate the
    paid webhook runs; a line it flags is one the order consumes on-hand).
  * preorder cap  -> ``product.template.grove_preorder_count`` (deposit-paid
    preorder units, the budget spent against ``grove_preorder_cap``).

``post_install`` + ``GroveTaxFixtureMixin`` so ``product.*`` creates resolve a
live default tax in the minimal chartless CI database (see tests/common.py).
"""

import os
from unittest.mock import patch

from odoo.addons.grove_headless.controllers.main import _oversold_lines
from odoo.addons.grove_headless.models import shippo_client
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFulfillmentPoolGate(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {
                "name": "Pool Gate Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "zip": "26651",
                "email": "poolgate@example.com",
                "company_id": self.company.id,
            }
        )
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id

    # ── fixtures ──────────────────────────────────────────────────────────
    def _product(self, name, tier="bareroot"):
        # is_storable so free_qty/qty_available track a real quant — the on-hand
        # pool the ship/pickup modes draw on. Bareroot is the shippable tier
        # (labels + oversell gate bite it); potted is pickup-only.
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "is_storable": True,
                "list_price": 40.0,
                "grove_shipping_tier": tier,
                "grove_tree_length": "20",
            }
        )

    def _set_stock(self, product, qty):
        # Mirror test_stripe_checkout._set_stock: a 0 delta raises in Odoo 19, so
        # a 0 baseline is a no-op; a non-stored compute cache must be dropped.
        if qty:
            self.env["stock.quant"]._update_available_quantity(product, self.location, qty)
        product.invalidate_recordset(["qty_available", "free_qty"])

    def _order(self, product, *, fulfillment, qty=2.0, status="paid", preorder=False):
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "grove_fulfillment": fulfillment,
                    "grove_checkout_status": status,
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": qty, "price_unit": 40.0})],
                }
            )
        )
        if preorder:
            order.grove_preorder_variant_ids = str(product.id)
        return order

    def _preorder_count(self, product):
        tmpl = product.product_tmpl_id
        tmpl.invalidate_recordset(["grove_preorder_count"])
        return tmpl.grove_preorder_count

    # ── ship: on-hand only ────────────────────────────────────────────────
    def test_ship_consumes_on_hand_not_cap(self):
        """A paid, in-stock ship order draws on the on-hand pool (the oversell
        gate consults its free_qty) and spends NOTHING against the preorder cap;
        a shipment email is owed. Short stock flips it oversold — proving the
        pool it consumes really is on-hand."""
        product = self._product("Ship Dogwood")
        self._set_stock(product, 5)
        order = self._order(product, fulfillment="ship", qty=2.0)

        # On-hand pool: 5 on hand, 2 wanted -> fillable, not oversold.
        self.assertFalse(_oversold_lines(order))
        # Preorder cap pool: a ship order is NOT a preorder — zero cap spent.
        self.assertEqual(self._preorder_count(product), 0)
        # Ship owes a shipment email.
        self.assertTrue(order.grove_should_send_shipment_email())

        # Drop the pool below the line and the same gate now flags it: the order
        # genuinely consumes on-hand, it is not merely ignored.
        self._set_stock(product, -5)  # back to 0 on hand
        self.assertEqual([line.product_id for line in _oversold_lines(order)], [product])

    # ── pickup: on-hand reserve, no label, no email ───────────────────────
    def test_pickup_reserves_on_hand_no_label_no_email(self):
        """A pickup order draws on on-hand (the oversell gate consults it) but
        never buys a label, never ships, never emails, and never touches the
        preorder cap — it collects at the farm."""
        product = self._product("Pickup Persimmon", tier="potted")
        self._set_stock(product, 1)
        order = self._order(product, fulfillment="pickup", qty=2.0)

        # On-hand pool: 1 on hand, 2 wanted -> short, so pickup DOES draw on-hand.
        self.assertEqual([line.product_id for line in _oversold_lines(order)], [product])
        # Preorder cap pool: untouched.
        self.assertEqual(self._preorder_count(product), 0)
        # No shipment email, and 'mark shipped' is refused outright (GOL-1981):
        # a pickup never enters the ship path, so no label/email can fire.
        self.assertFalse(order.grove_should_send_shipment_email())
        self.assertFalse(order.action_grove_mark_shipped())
        # Its own terminal transition is collection, not shipment.
        self.assertTrue(order.action_grove_mark_collected())
        self.assertEqual(order.grove_fulfillment_stage, "collected")

    # ── preorder: cap only, never on-hand, never a label ──────────────────
    def test_preorder_consumes_cap_not_on_hand(self):
        """A deposit-paid preorder spends against the preorder cap and is EXCLUDED
        from the on-hand oversell gate even at zero stock — it consumes the cap
        pool, not on-hand — and buys no label while its wave is closed."""
        product = self._product("Preorder Pawpaw")
        self._set_stock(product, 0)  # zero on hand: a preorder needs none
        order = self._order(product, fulfillment="ship", qty=3.0, status="deposit_paid", preorder=True)

        # Preorder cap pool: 3 units owed.
        self.assertEqual(self._preorder_count(product), 3)
        # On-hand pool: excluded from the oversell gate despite 0 free stock —
        # a preorder is legitimately short, not an oversell.
        self.assertFalse(_oversold_lines(order))
        # No label at order time while the wave is closed (GOL-1933 guard).
        with (
            patch.dict(os.environ, {"SHIPPO_API_KEY": "test-key"}),
            patch.object(shippo_client, "buy_cheapest_ground_label") as buy,
        ):
            with self.assertRaisesRegex(UserError, "no shippable lines"):
                order.action_buy_shipping_labels()
            buy.assert_not_called()

    # ── isolation: two modes on ONE product keep separate pools ───────────
    def test_ship_and_preorder_on_same_product_do_not_cross(self):
        """A ship order and a preorder for the SAME variant must not bleed into
        each other's pool: the cap counts only the preorder's units, and the
        oversell gate flags only the ship line — never the preorder line — when
        stock is short. This is the 'only the correct pool' half of acceptance."""
        product = self._product("Shared Elderberry")
        self._set_stock(product, 0)  # short for the ship line, irrelevant to preorder

        ship = self._order(product, fulfillment="ship", qty=2.0, status="paid")
        preorder = self._order(product, fulfillment="ship", qty=4.0, status="deposit_paid", preorder=True)

        # Cap pool holds ONLY the preorder's 4 units — the ship order's 2 are not
        # a preorder and never counted.
        self.assertEqual(self._preorder_count(product), 4)
        # On-hand pool: the ship line is oversold at 0 stock; the preorder line is
        # excluded from the gate entirely.
        self.assertEqual([line.product_id for line in _oversold_lines(ship)], [product])
        self.assertFalse(_oversold_lines(preorder))
