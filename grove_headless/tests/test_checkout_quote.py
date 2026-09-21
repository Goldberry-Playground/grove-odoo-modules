"""Pre-checkout deposit quote (GOL-2233 follow-up).

The storefront's cart and checkout-form summaries only knew unit prices, so a
sold-out bareroot reservation read as "charged in full" until the Stripe
review step. ``POST /grove/api/v1/checkout/quote`` answers "does this cart take
the flat $10 deposit today?" READ-ONLY, from ``_deposit_reason_for_lines`` —
the same predicate ``_order_takes_deposit`` charges by — so the preview and
the charge can never disagree. Runs under Odoo's --test-enable runner.
"""

import json
from datetime import date, datetime, timedelta

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase, get_db_name

BEFORE_CUTOVER = date(2026, 9, 20)
AFTER_CUTOVER = date(2026, 10, 16)


class _PoolFixture:
    """A Format-axis template (Potted + Bareroot variants) in the main company,
    mirroring test_shared_pool_qty so bareroot draws on the potted pool."""

    def _build_pool(self):
        self.company = self.env.ref("base.main_company")
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        v_potted = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": fmt.id})
        v_bareroot = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": fmt.id})
        self.tmpl = self.env["product.template"].create(
            {
                "name": "Quote Serviceberry",
                "type": "consu",
                "is_storable": True,
                "sale_ok": True,
                "list_price": 12.0,
                "grove_shipping_tier": "bareroot",
                "company_id": self.company.id,
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": fmt.id, "value_ids": [(6, 0, [v_potted.id, v_bareroot.id])]}),
                ],
            }
        )

        def variant(name):
            return self.tmpl.product_variant_ids.filtered(
                lambda v: name in v.product_template_variant_value_ids.mapped("name")
            )[:1]

        self.potted = variant("Potted")
        self.bareroot = variant("Bareroot")

    def _stock(self, variant, qty):
        # Same idiom as test_stripe_checkout._set_stock: a 0 delta is rejected
        # by Odoo 19's quant update (GOL-2014), so only write non-zero and
        # always invalidate the non-stored quantity computes.
        if qty:
            self.env["stock.quant"]._update_available_quantity(variant, self.location, qty)
        variant.invalidate_recordset(["qty_available", "free_qty"])


@tagged("grove_headless", "post_install", "-at_install")
class TestDepositReasonForLines(_PoolFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build_pool()

    def _reason(self, lines, fulfillment="ship", today=BEFORE_CUTOVER):
        return grove_main._deposit_reason_for_lines(self.env, lines, fulfillment, today)

    def test_sold_out_bareroot_is_a_deposit_any_fulfillment(self):
        # No stock anywhere in the pool → the bareroot line is sold out.
        self.assertEqual(self._reason([(self.bareroot, 1)], "ship"), "sold-out")
        self.assertEqual(self._reason([(self.bareroot, 1)], "pickup"), "sold-out")
        self.assertEqual(self._reason([(self.bareroot, 1)], None), "sold-out")

    def test_shortfall_not_just_zero(self):
        self._stock(self.bareroot, 2)
        self.assertIsNone(self._reason([(self.bareroot, 2)]))
        self.assertEqual(self._reason([(self.bareroot, 3)]), "sold-out")

    def test_bareroot_draws_on_the_potted_pool(self):
        # GOL-2031: potted stock backs the bareroot option in leafed season.
        self._stock(self.potted, 5)
        self.assertIsNone(self._reason([(self.bareroot, 4)]))

    def test_in_stock_bareroot_before_cutover_charges_in_full(self):
        self._stock(self.bareroot, 3)
        self.assertIsNone(self._reason([(self.bareroot, 1)], "ship", BEFORE_CUTOVER))

    def test_sold_out_potted_never_triggers(self):
        # Potted is pickup-only and not reservable; no stock ≠ deposit.
        self.assertIsNone(self._reason([(self.potted, 1)], "pickup"))
        self.assertIsNone(self._reason([(self.potted, 1)], "ship", AFTER_CUTOVER))

    def test_after_cutover_shipped_bareroot_deposits_even_when_stocked(self):
        self._stock(self.bareroot, 3)
        self.assertEqual(self._reason([(self.bareroot, 1)], "ship", AFTER_CUTOVER), "off-season")
        # Unset fulfillment counts as ship (GOL-1906 label-gate idiom).
        self.assertEqual(self._reason([(self.bareroot, 1)], None, AFTER_CUTOVER), "off-season")

    def test_after_cutover_pickup_charges_in_full(self):
        self._stock(self.bareroot, 3)
        self.assertIsNone(self._reason([(self.bareroot, 1)], "pickup", AFTER_CUTOVER))

    def test_sold_out_outranks_off_season_as_the_reason(self):
        self.assertEqual(self._reason([(self.bareroot, 1)], "ship", AFTER_CUTOVER), "sold-out")

    def test_one_sold_out_line_takes_the_whole_cart(self):
        self._stock(self.potted, 5)
        # A stocked potted line plus a sold-out bareroot sibling of another
        # cultivar: the whole cart deposits.
        other = self.env["product.product"].create(
            {
                "name": "Other Bareroot",
                "type": "consu",
                "is_storable": True,
                "product_tmpl_id": self.env["product.template"]
                .create(
                    {
                        "name": "Other",
                        "type": "consu",
                        "is_storable": True,
                        "grove_shipping_tier": "bareroot",
                        "company_id": self.company.id,
                    }
                )
                .id,
            }
        )
        self.assertEqual(self._reason([(self.potted, 1), (other, 1)], "pickup"), "sold-out")

    def test_order_predicate_delegates_to_the_line_predicate(self):
        # _order_takes_deposit must agree with the line rule for the same cart.
        partner = self.env["res.partner"].create({"name": "Q", "email": "q@example.com"})
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": self.bareroot.id, "product_uom_qty": 1})],
                }
            )
        )
        order.grove_fulfillment = "ship"
        self.assertTrue(grove_main._order_takes_deposit(order, BEFORE_CUTOVER))
        self._stock(self.bareroot, 3)
        self.assertFalse(grove_main._order_takes_deposit(order, BEFORE_CUTOVER))
        self.assertTrue(grove_main._order_takes_deposit(order, AFTER_CUTOVER))
        order.grove_fulfillment = "pickup"
        self.assertFalse(grove_main._order_takes_deposit(order, AFTER_CUTOVER))


@tagged("grove_headless", "post_install", "-at_install")
class TestCheckoutQuoteEndpoint(_PoolFixture, HttpCase):
    def setUp(self):
        super().setUp()
        self._build_pool()
        try:
            admin = self.env.ref("base.user_admin")
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-quote-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _post(self, body, authed=True):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Content-Type": "application/json",
        }
        if authed:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return self.url_open(
            "/grove/api/v1/checkout/quote",
            data=json.dumps(body).encode(),
            headers=headers,
        )

    def test_sold_out_bareroot_quotes_the_flat_deposit(self):
        orders_before = self.env["sale.order"].search_count([])
        resp = self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 2}], "fulfillment": "ship"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["deposit_now"])
        self.assertEqual(body["deposit_reason"], "sold-out")
        self.assertEqual(body["amount_due_today"], stripe_gateway.PREORDER_DEPOSIT)
        self.assertEqual(body["deposit_amount"], stripe_gateway.PREORDER_DEPOSIT)
        self.assertEqual(
            body["lines"],
            [{"variant_id": self.bareroot.id, "quantity": 2.0, "bareroot": True, "sold_out": True, "free_qty": 0.0}],
        )
        # Read-only: no draft order, partner, or session was created.
        self.assertEqual(self.env["sale.order"].search_count([]), orders_before)

    def test_stocked_bareroot_quotes_full_charge(self):
        self._stock(self.bareroot, 5)
        body = self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 1}]}).json()
        # Before Oct 15 an in-stock bareroot ships now, charged in full; after
        # the cutover the route reports off-season. Both are consistent with
        # the charge the session would make on the same day.
        if body["after_cutover"]:
            self.assertEqual(body["deposit_reason"], "off-season")
        else:
            self.assertFalse(body["deposit_now"])
            self.assertIsNone(body["amount_due_today"])
        self.assertEqual(body["lines"][0]["free_qty"], 5.0)
        self.assertFalse(body["lines"][0]["sold_out"])

    def test_validation_and_unknown_variant(self):
        self.assertEqual(self._post({"items": []}).status_code, 400)
        self.assertEqual(self._post({"items": [{"variant_id": "x", "quantity": 1}]}).status_code, 400)
        self.assertEqual(self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 0}]}).status_code, 400)
        self.assertEqual(
            self._post(
                {"items": [{"variant_id": self.bareroot.id, "quantity": 1}], "fulfillment": "teleport"}
            ).status_code,
            400,
        )
        self.assertEqual(self._post({"items": [{"variant_id": 99999999, "quantity": 1}]}).status_code, 404)

    def test_requires_bearer_auth(self):
        resp = self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 1}]}, authed=False)
        self.assertIn(resp.status_code, (401, 403))
