"""Odoo-runtime draft-order / tax tests for Printify carts (spec §5-6)."""

import json
from datetime import datetime, timedelta
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client, stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase, get_db_name

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

    def test_fractional_qty_refused_400(self):
        # Printify prints whole shirts: a 1.5 qty would bill 1.5 in Odoo while
        # int(1.5)=1 ships (GOL-3314 review 2026-10-14).
        order, error, quoted = self._draft(self._payload("CA", items=[{"variant_id": self.shirt.id, "quantity": 1.5}]))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("whole number", error.data.decode())
        quoted.assert_not_called()
        self.assertFalse(self._orphans())

    def test_ships_to_checkout_address_not_stored_one(self):
        # A returning customer is reused by email and never mutated from a public
        # POST, so a shirt must ship to a dedicated delivery contact built from
        # THIS checkout, not the customer's OLD stored address (GOL-3314 review).
        stored = self.env["res.partner"].create(
            {
                "name": "Shirt Buyer",
                "email": "shirt@example.com",
                "company_id": self.company.id,
                "street": "99 Old St",
                "city": "Oldtown",
                "zip": "10001",
            }
        )
        order, error, _quoted = self._draft(self._payload("CA"))
        self.assertIsNone(error)
        self.assertEqual(order.partner_id, stored)
        self.assertEqual(stored.street, "99 Old St")  # main record untouched
        self.assertNotEqual(order.partner_shipping_id, stored)
        self.assertEqual(order.partner_shipping_id.type, "delivery")
        self.assertEqual(order.partner_shipping_id.parent_id, stored)
        self.assertEqual(order.partner_shipping_id.zip, "90001")
        self.assertEqual(order.partner_shipping_id.state_id.code, "CA")


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyQuoteEndpoint(_PrintifyFixture, GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._build()
        # The quote endpoint memoizes live Printify quotes per (region, ZIP,
        # lines) for a few minutes; clear it so each test sees its own mock.
        grove_main._printify_quote_cache.clear()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-printify-quote", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"API keys unavailable: {exc}")

    def _post(self, body):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        return self.url_open("/grove/api/v1/checkout/quote", data=json.dumps(body).encode(), headers=headers)

    def _body(self, **extra):
        body = {
            "items": [{"variant_id": self.shirt.id, "quantity": 1}],
            "fulfillment": "ship",
            "shipping": {"state": "CA", "zip": "90001"},
        }
        body.update(extra)
        return body

    def test_quote_returns_printify_shipping(self):
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            data = json.loads(self._post(self._body()).content)
        self.assertEqual(data["fulfillment_route"], "printify")
        self.assertEqual(data["shipping_amount"], 4.75)
        self.assertFalse(data["deposit_now"])

    def test_quote_without_address_has_null_shipping(self):
        with mock.patch.object(grove_main, "_printify_quote") as quoted:
            data = json.loads(self._post(self._body(shipping=None)).content)
        self.assertIsNone(data["shipping_amount"])
        quoted.assert_not_called()

    def test_quote_503_when_printify_down(self):
        with mock.patch.object(grove_main, "_printify_quote", side_effect=printify_client.PrintifyError("x", 503)):
            resp = self._post(self._body())
        self.assertEqual(resp.status_code, 503)

    def test_quote_409_mixed(self):
        resp = self._post(
            self._body(
                items=[{"variant_id": self.shirt.id, "quantity": 1}, {"variant_id": self.tree.id, "quantity": 1}]
            )
        )
        self.assertEqual(resp.status_code, 409)

    def test_tree_quote_unchanged(self):
        data = json.loads(
            self._post(self._body(items=[{"variant_id": self.tree.id, "quantity": 1}], shipping=None)).content
        )
        self.assertEqual(data["fulfillment_route"], "in_house")
        self.assertIsNone(data["shipping_amount"])

    def test_identical_quote_is_cached_within_ttl(self):
        # Repeated polls for the same cart + destination hit Printify once, not on
        # every keystroke (GOL-3314 review 2026-10-14).
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75) as quoted:
            first = json.loads(self._post(self._body()).content)
            second = json.loads(self._post(self._body()).content)
        self.assertEqual(first["shipping_amount"], 4.75)
        self.assertEqual(second["shipping_amount"], 4.75)
        quoted.assert_called_once()
