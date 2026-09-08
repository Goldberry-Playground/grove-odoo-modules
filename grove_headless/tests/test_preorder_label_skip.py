"""GOL-1982 Phase-5 inventory gate: a preorder line never buys a shipping label.

The three fulfilment modes consume different pools and must not be conflated.
Preorder is the sharp one: it consumes a per-variant ``preorder_cap`` (GOL-1671),
never on-hand stock, and owes **no** label at order time (GOL-1933 guard). The
oversell webhook already excludes ``grove_preorder_variant_ids`` from its on-hand
check; this guards the *other* on-hand touchpoint — ``action_buy_shipping_labels``
— so a bareroot (shippable-tier) preorder variant is never packed and labelled
*while its ship wave is still closed*.

The gate is wave-aware, NOT unconditional: ``grove_preorder_variant_ids`` is the
permanent order-time deposit record and is never cleared, so once the wave opens
(``action_grove_assign_wave`` -> ``wave_assigned``) that same method IS the
preorder ship + settle path and MUST pack the line. Both directions are proven
below so a preorder can never be stranded (never ships) nor shipped early.

``post_install`` + ``GroveTaxFixtureMixin`` so ``product.*`` creates resolve a
live default tax in the minimal chartless CI database (see tests/common.py).
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.models import shippo_client
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPreorderLabelSkip(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {
                "name": "Preorder Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "zip": "26651",
                "email": "preorder@example.com",
            }
        )

    def _bareroot_product(self, name):
        # Bareroot = shippable tier: without the preorder guard this variant WOULD
        # pack and buy a label, so it is the exact conflation the gate prevents.
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )

    def _order(self, product, mark_preorder):
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 2.0, "price_unit": 40.0})],
                }
            )
        )
        if mark_preorder:
            order.grove_preorder_variant_ids = str(product.id)
        return order

    def test_preorder_id_set_parses_field(self):
        """The shared parser tolerates blanks/whitespace and yields int ids."""
        order = self._order(self._bareroot_product("Parse Tree"), mark_preorder=False)
        order.grove_preorder_variant_ids = " 12, ,34 ,, 56 "
        self.assertEqual(order._preorder_variant_id_set(), {12, 34, 56})
        order.grove_preorder_variant_ids = False
        self.assertEqual(order._preorder_variant_id_set(), set())

    def test_all_preorder_order_buys_no_label(self):
        """A pure-preorder order hits the no-shippable guard before any Shippo
        call — the preorder line is excluded, so no label is ever bought.

        The message is pinned so this asserts the *preorder exclusion* branch
        specifically, not some other pre-purchase refusal (bad state, etc.)."""
        product = self._bareroot_product("Preorder Dogwood")
        order = self._order(product, mark_preorder=True)
        with (
            patch.dict(os.environ, {"SHIPPO_API_KEY": "test-key"}),
            patch.object(shippo_client, "buy_cheapest_ground_label") as buy,
        ):
            with self.assertRaisesRegex(UserError, "no shippable lines"):
                order.action_buy_shipping_labels()
            buy.assert_not_called()
        # No label side effects were written.
        self.assertFalse(order.grove_tracking_numbers)
        self.assertFalse(order.grove_delivery_status)

    def test_preorder_at_open_wave_buys_label(self):
        """Once the wave opens (grove_fulfillment_state = wave_assigned), the SAME
        method is the preorder ship path and MUST pack the (formerly-preorder)
        line and buy a label — the deposit record is permanent, so a wave-blind
        skip would strand the preorder forever. Packing/Shippo are stubbed so this
        isolates the gate: at wave_assigned the line is included, a label is
        bought, and the fulfilment watermark advances to label_purchased."""
        product = self._bareroot_product("Wave Dogwood")
        order = self._order(product, mark_preorder=True)
        # Walk the legal preorder path to an open wave: deposit_paid -> wave_assigned.
        order.grove_fulfillment_state = "deposit_paid"
        self.assertTrue(order.action_grove_assign_wave("2026-fall"))
        self.assertEqual(order.grove_fulfillment_stage, "wave_assigned")

        fake_label = {
            "tracking_number": "1Z-WAVE",
            "label_url": "https://labels.example/wave.pdf",
            "carrier": "usps",
            "servicelevel": "usps_ground_advantage",
            "amount": "8.97",
        }

        # The real _persist_label_result writes through an independent cursor so a
        # bought label survives a later rollback; that durability is owned/tested
        # elsewhere. Here it would (a) hide the write from this test's cursor and
        # (b) leak committed rows past the test's rollback — so persist in-cursor.
        def _persist_in_cursor(order, vals):
            order.write(vals)

        with (
            patch.dict(os.environ, {"SHIPPO_API_KEY": "test-key"}),
            # Stub the packer so the test is not coupled to the live rate table /
            # ship-window calendar; the gate under test is line inclusion, not packing.
            patch.object(sale_order_module, "pack_for_state", return_value=[SimpleNamespace(box_id="BR_S", count=1)]),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
            # An OPEN wave implies dormant season by definition — stub the GOL-1906
            # dormancy fail-closed gate (landed after this test, #190) the same way
            # the packer is stubbed, so the test isn't coupled to today's date.
            patch.object(sale_order_module, "can_ship_bareroot", return_value=True),
            patch.object(shippo_client, "build_shipment_payload", return_value={"box": "BR_S"}),
            patch.object(shippo_client, "buy_cheapest_ground_label", return_value=fake_label) as buy,
            # Ship-time settlement (GOL-2053) runs after labels and reaches Stripe;
            # it is proven elsewhere and swallows its own errors — stub it so this
            # test asserts only the label gate.
            patch.object(sale_order_module.SaleOrder, "_grove_settle_at_ship", return_value="settled"),
            patch.object(sale_order_module.SaleOrder, "_persist_label_result", _persist_in_cursor),
        ):
            self.assertTrue(order.action_buy_shipping_labels())
            # The preorder line was INCLUDED: a label was actually bought.
            buy.assert_called_once()
        self.assertEqual(order.grove_tracking_numbers, "1Z-WAVE")
        self.assertEqual(order.grove_delivery_status, "label_purchased")
        # Watermark advanced onto the ship path (legal from wave_assigned).
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")
