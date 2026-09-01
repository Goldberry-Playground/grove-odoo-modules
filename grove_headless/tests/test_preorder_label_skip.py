"""GOL-1982 Phase-5 inventory gate: a preorder line never buys a shipping label.

The three fulfilment modes consume different pools and must not be conflated.
Preorder is the sharp one: it consumes a per-variant ``preorder_cap`` (GOL-1671),
never on-hand stock, and owes **no** label at order time (GOL-1933 guard). The
oversell webhook already excludes ``grove_preorder_variant_ids`` from its on-hand
check; this guards the *other* on-hand touchpoint — ``action_buy_shipping_labels``
— so a bareroot (shippable-tier) preorder variant is never packed and labelled.

``post_install`` + ``GroveTaxFixtureMixin`` so ``product.*`` creates resolve a
live default tax in the minimal chartless CI database (see tests/common.py).
"""

import os
from unittest.mock import patch

from odoo.addons.grove_headless import shippo_client
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
