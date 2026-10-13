"""Odoo-runtime draft-order / tax tests for Printify carts (spec §5)."""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client, stripe_gateway
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


class _PrintifyFixture:
    def _build(self):
        self.company = self.env.ref("base.main_company")
        size = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        medium = self.env["product.attribute.value"].create({"attribute_id": size.id, "name": "M"})
        self.shirt_tmpl = self.env["product.template"].create(
            {
                "name": "Grove Tee",
                "type": "consu",
                "is_storable": False,
                "list_price": 28.0,
                "company_id": self.company.id,
                "website_published": True,
                "grove_fulfillment_route": "printify",
                "printify_product_id": "6650abc",
                "attribute_line_ids": [(0, 0, {"attribute_id": size.id, "value_ids": [(6, 0, medium.ids)]})],
            }
        )
        self.shirt = self.shirt_tmpl.product_variant_ids[0]
        self.shirt.write({"printify_variant_id": 11001})
        self.tree = (
            self.env["product.template"]
            .create(
                {
                    "name": "Pawpaw",
                    "type": "consu",
                    "is_storable": True,
                    "list_price": 12.0,
                    "company_id": self.company.id,
                    "grove_shipping_tier": "potted",
                }
            )
            .product_variant_ids[0]
        )

    def _website(self):
        return self.env["website"].search([("company_id", "=", self.company.id)], limit=1) or self.env[
            "website"
        ].search([], limit=1)

    def _payload(self, state="CA", items=None, **extra):
        payload = {
            "contact": {"name": "Shirt Buyer", "email": "shirt@example.com", "phone": "3045551212"},
            "items": items or [{"variant_id": self.shirt.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "90001"},
        }
        payload.update(extra)
        return payload


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyDraftOrder(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()

    def _draft(self, payload, quote=4.75):
        patch = mock.patch.object(grove_main, "_printify_quote", return_value=quote)
        if isinstance(quote, Exception):
            patch = mock.patch.object(grove_main, "_printify_quote", side_effect=quote)
        with patch as quoted:
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        return order, error, quoted

    def _orphans(self):
        return self.env["sale.order"].search([("partner_id.email", "=", "shirt@example.com")])

    def test_shirt_ships_to_non_green_state_with_printify_quote(self):
        order, error, quoted = self._draft(self._payload("CA"))
        self.assertIsNone(error)
        ship = order.order_line.filtered(lambda ln: ln.product_id.default_code == grove_main.SHIPPING_PRODUCT_CODE)
        self.assertEqual(ship.price_unit, 4.75)
        self.assertTrue(order.grove_is_printify)
        self.assertEqual(order.grove_fulfillment, "ship")
        line_items, address = quoted.call_args.args
        self.assertEqual(line_items, [{"product_id": "6650abc", "variant_id": 11001, "quantity": 1}])
        self.assertEqual((address["country"], address["region"], address["zip"]), ("US", "CA", "90001"))

    def test_mixed_cart_refused_409(self):
        items = [{"variant_id": self.shirt.id, "quantity": 1}, {"variant_id": self.tree.id, "quantity": 1}]
        order, error, quoted = self._draft(self._payload("WV", items=items))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 409)
        self.assertIn("Shirts check out separately from trees.", error.data.decode())
        quoted.assert_not_called()
        self.assertFalse(self._orphans())

    def test_territory_refused_400_without_quote(self):
        order, error, quoted = self._draft(self._payload("PR"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("50 US states and DC", error.data.decode())
        quoted.assert_not_called()
        self.assertFalse(self._orphans())

    def test_pickup_refused_400(self):
        order, error, _quoted = self._draft(self._payload("WV", fulfillment="pickup"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)

    def test_quote_failure_is_503_and_no_orphan(self):
        order, error, _quoted = self._draft(self._payload("CA"), quote=printify_client.PrintifyError("down", 503))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 503)
        self.assertIn("Shipping is temporarily unavailable. Try again in a few minutes.", error.data.decode())
        self.assertFalse(self._orphans())

    def test_tree_order_unchanged_still_blocks_non_green(self):
        order, error, quoted = self._draft(self._payload("CA", items=[{"variant_id": self.tree.id, "quantity": 1}]))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't ship", error.data.decode().lower())
        quoted.assert_not_called()

    def test_apparel_tax_code(self):
        self.assertEqual(stripe_gateway.TAX_CODE_APPAREL, "txcd_30011000")
        self.assertEqual(grove_main._stripe_tax_code_for(self.shirt), "txcd_30011000")
        self.assertEqual(grove_main._stripe_tax_code_for(self.tree), stripe_gateway.TAX_CODE_GOODS)
