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
from unittest import mock

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

    def _reason(self, lines, fulfillment="ship", today=BEFORE_CUTOVER, ship_wave=None):
        return grove_main._deposit_reason_for_lines(self.env, lines, fulfillment, today, ship_wave)

    def test_in_stock_bareroot_with_wave_is_a_preorder_ship_or_pickup(self):
        self._stock(self.bareroot, 5)
        for fulfillment in ("ship", "pickup"):
            for today in (BEFORE_CUTOVER, AFTER_CUTOVER):
                for wave in ("fall", "spring"):
                    self.assertEqual(self._reason([(self.bareroot, 1)], fulfillment, today, wave), "preorder")

    def test_sold_out_bareroot_with_wave_is_a_preorder(self):
        self.assertEqual(self._reason([(self.bareroot, 1)], "ship", BEFORE_CUTOVER, "fall"), "preorder")

    def test_potted_only_with_wave_is_never_a_deposit(self):
        self._stock(self.potted, 5)
        self.assertIsNone(self._reason([(self.potted, 1)], "pickup", BEFORE_CUTOVER, "fall"))
        self.assertIsNone(self._reason([(self.potted, 1)], "ship", AFTER_CUTOVER, "fall"))

    def test_no_wave_legacy_rule_unchanged(self):
        self._stock(self.bareroot, 5)
        self.assertIsNone(self._reason([(self.bareroot, 1)], "ship", BEFORE_CUTOVER))
        self.assertEqual(self._reason([(self.bareroot, 1)], "ship", AFTER_CUTOVER), "off-season")
        self.assertIsNone(self._reason([(self.bareroot, 1)], "pickup", AFTER_CUTOVER))

    def test_order_predicate_reads_the_stored_wave(self):
        partner = self.env["res.partner"].create({"name": "W", "email": "w@example.com"})
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
        order.grove_fulfillment = "pickup"
        self._stock(self.bareroot, 3)
        self.assertFalse(grove_main._order_takes_deposit(order, BEFORE_CUTOVER))
        order.grove_ship_wave = "fall"
        self.assertTrue(grove_main._order_takes_deposit(order, BEFORE_CUTOVER))
        self.assertTrue(grove_main._order_takes_deposit(order, AFTER_CUTOVER))

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
class TestCheckoutQuoteEndpoint(_PoolFixture, GroveTaxFixtureMixin, HttpCase):
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

    def _quote_waved(self, items, fulfillment, today=date(2026, 10, 7), **extra):
        # The wave rules are nursery-only (M1); this fixture posts as goldberry
        # (main-company pool), so treat it as the nursery for the rule tests.
        # The real tenant scope is covered by the *_tenant tests below.
        body = {"items": [{"variant_id": v.id, "quantity": 1} for v in items], "fulfillment": fulfillment}
        body.update(extra)
        with (
            mock.patch.object(grove_main, "_today_utc", return_value=today),
            mock.patch.object(grove_main, "_is_nursery_website", return_value=True),
        ):
            return self._post(body)

    def _post_tenant(self, body, tenant):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": tenant,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        return self.url_open("/grove/api/v1/checkout/quote", data=json.dumps(body).encode(), headers=headers)

    def test_goldberry_tenant_potted_after_oct_15_quotes(self):
        self._stock(self.potted, 5)
        body = {"items": [{"variant_id": self.potted.id, "quantity": 1}], "fulfillment": "pickup"}
        with mock.patch.object(grove_main, "_today_utc", return_value=date(2026, 10, 16)):
            resp = self._post_tenant(dict(body, ship_wave="fall"), "goldberry")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["ship_wave"])

    def test_nursery_tenant_potted_after_oct_15_rejected(self):
        # A company-less product so the nursery tenant's variant lookup finds it;
        # grove_shipping_tier left at its "potted" default.
        product = self.env["product.product"].create({"name": "Nursery Quote Potted", "type": "consu"})
        body = {"items": [{"variant_id": product.id, "quantity": 1}], "fulfillment": "pickup"}
        with mock.patch.object(grove_main, "_today_utc", return_value=date(2026, 10, 16)):
            resp = self._post_tenant(body, "nursery")
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("sold through oct 15", resp.text.lower())

    def test_wave_quote_is_a_preorder_deposit_for_pickup_and_ship(self):
        self._stock(self.bareroot, 5)
        resp = self._quote_waved([self.bareroot], "pickup", ship_wave="fall")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["deposit_now"])
        self.assertEqual(body["deposit_reason"], "preorder")
        self.assertEqual(body["amount_due_today"], 10.0)
        self.assertEqual(body["ship_wave"], "fall")
        ship = self._quote_waved([self.bareroot], "ship", ship_wave="spring", shipping={"zip": "08014"}).json()
        self.assertEqual(
            (ship["deposit_reason"], ship["amount_due_today"], ship["ship_wave"]), ("preorder", 10.0, "spring")
        )

    def test_wave_quote_ship_without_zip_skips_only_the_zone_check(self):
        self._stock(self.bareroot, 5)
        body = self._quote_waved([self.bareroot], "ship", ship_wave="fall").json()
        self.assertEqual(body["deposit_reason"], "preorder")

    def test_no_wave_quote_reports_null_wave(self):
        self._stock(self.bareroot, 5)
        body = self._quote_waved([self.bareroot], "pickup").json()
        self.assertIsNone(body["ship_wave"])

    def test_wave_quote_rejects_what_the_order_path_rejects(self):
        self._stock(self.bareroot, 5)
        self._stock(self.potted, 5)
        cases = [
            (self._quote_waved([self.bareroot], "pickup", ship_wave="winter"), "fall or spring"),
            (self._quote_waved([self.potted], "pickup", ship_wave="fall"), "only applies to bareroot"),
            (self._quote_waved([self.potted], "pickup", date(2026, 10, 16)), "sold through Oct 15"),
            (self._quote_waved([self.potted, self.bareroot], "pickup"), "Pre-orders check out on their own"),
            (self._quote_waved([self.bareroot], "pickup", date(2026, 11, 22), ship_wave="fall"), "closed on"),
            (self._quote_waved([self.bareroot], "ship", ship_wave="fall", shipping={"zip": "00000"}), "planting zone"),
        ]
        for resp, fragment in cases:
            self.assertEqual(resp.status_code, 400, (fragment, resp.text))
            self.assertIn(fragment.lower(), resp.text.lower())

    def test_validation_and_unknown_variant(self):
        self.assertEqual(self._post({"items": []}).status_code, 400)
        self.assertEqual(self._post({"items": [{"variant_id": "x", "quantity": 1}]}).status_code, 400)
        self.assertEqual(self._post({"items": [{"variant_id": self.bareroot.id, "quantity": -1}]}).status_code, 400)
        self.assertEqual(
            self._post(
                {"items": [{"variant_id": self.bareroot.id, "quantity": 1}], "fulfillment": "teleport"}
            ).status_code,
            400,
        )
        self.assertEqual(self._post({"items": [{"variant_id": 99999999, "quantity": 1}]}).status_code, 404)

    def test_explicit_zero_quantity_is_rejected_but_omitted_defaults_to_one(self):
        """``quantity: 0`` is a client error, an ABSENT quantity means one.

        The parser defaults only on a missing key (``.get("quantity", 1)``).
        Using ``or 1`` instead made an explicit 0 falsy, coercing it to one unit
        past the positivity guard — on the order path that silently CHARGED a
        tree nobody ordered (Ada, review of #244).
        """
        self.assertEqual(self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 0}]}).status_code, 400)
        self.assertEqual(self._post({"items": [{"variant_id": self.bareroot.id, "quantity": None}]}).status_code, 400)
        omitted = self._post({"items": [{"variant_id": self.bareroot.id}]})
        self.assertEqual(omitted.status_code, 200)
        self.assertEqual(omitted.json()["lines"][0]["quantity"], 1.0)

    def test_order_path_rejects_zero_quantity_identically(self):
        """The CHARGE surface must reject exactly what the preview rejects.

        ``/orders`` (and therefore ``/checkout/session``, which shares
        ``_create_draft_order``) parses quantity with the same helper. If only
        the quote were fixed, a qty-0 cart would 400 in the preview and become a
        charged qty-1 line at checkout — the preview-vs-charge divergence this
        endpoint exists to prevent. Asserted here rather than in
        test_stripe_checkout.py so the money-path test file stays untouched.
        """
        body = {
            "contact": {"name": "Quote Zero", "email": "quote-zero@example.com", "phone": "3045551212"},
            "items": [{"variant_id": self.bareroot.id, "quantity": 0}],
        }
        resp = self.url_open(
            "/grove/api/v1/orders",
            data=json.dumps(body).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        # And no partial order was left behind by the rejected request.
        self.assertFalse(self.env["sale.order"].search_count([("partner_id.email", "=", "quote-zero@example.com")]))

    def test_order_path_requires_a_phone(self):
        """Phone is required on every checkout (2026-09-30): a missing, empty
        or whitespace-only contact.phone is a 400 and leaves no draft order."""
        for phone in (None, "", "   "):
            contact = {"name": "No Phone", "email": "no-phone@example.com"}
            if phone is not None:
                contact["phone"] = phone
            resp = self.url_open(
                "/grove/api/v1/orders",
                data=json.dumps(
                    {"contact": contact, "items": [{"variant_id": self.bareroot.id, "quantity": 1}]}
                ).encode(),
                headers={
                    "X-Odoo-Database": get_db_name(),
                    "X-Grove-Tenant": "goldberry",
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertIn("contact.phone", resp.text)
        self.assertFalse(self.env["sale.order"].search_count([("partner_id.email", "=", "no-phone@example.com")]))

    def test_requires_bearer_auth(self):
        resp = self._post({"items": [{"variant_id": self.bareroot.id, "quantity": 1}]}, authed=False)
        self.assertIn(resp.status_code, (401, 403))
