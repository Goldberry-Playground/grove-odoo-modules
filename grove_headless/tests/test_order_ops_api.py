"""Card payload and action endpoints for the Discord bridge.

Each route under ``/grove/api/v1/orders/<id>/`` runs one order action as the
bearer-auth'd service user and returns ``{"card": <payload>}``, so the bridge
re-renders the card from what Odoo says and never from its own guess. DB-backed,
so listed in tests/__init__.py AND conftest.py's collect_ignore_glob.
"""

import json
from datetime import date, datetime, timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, get_db_name, tagged

from .common import GroveTaxFixtureMixin

UPS = "1Z999AA10123456784"


@tagged("post_install", "-at_install")
class TestOrderOpsApi(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-order-ops-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")
        websites = self.env["website"]
        website = websites._resolve_tenant_slug("goldberry") or websites.search([], limit=1)
        self.company = website.company_id
        self.product = self.env["product.product"].create({"name": "Pawpaw", "type": "consu", "list_price": 22.0})

    # helpers

    def _order(self, company=None, **vals):
        company = company or self.company
        partner = self.env["res.partner"].create(
            {
                "name": "Card Buyer",
                "email": "card@example.com",
                "zip": "26651",
                "country_id": self.env.ref("base.us").id,
                "company_id": company.id,
            }
        )
        order = (
            self.env["sale.order"]
            .with_company(company)
            .create(
                {
                    "partner_id": partner.id,
                    "partner_shipping_id": partner.id,
                    "company_id": company.id,
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
                }
            )
        )
        order.grove_fulfillment = "ship"
        order.sudo().write({"grove_checkout_status": "paid", **vals})
        return order

    def _post(self, order, path, body, auth=True):
        self.env.flush_all()
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Content-Type": "application/json",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.url_open(
            f"/grove/api/v1/orders/{order.id}/{path}",
            data=json.dumps(body).encode(),
            headers=headers,
            timeout=60,
        )
        self.env.invalidate_all()
        return response

    def _card(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["card"]

    def _held(self, until="2027-03-01"):
        order = self._order()
        order.action_grove_set_hold(fields.Date.from_string(until), "Wants spring", "Josh")
        return order

    # hold

    def test_hold_route_accepts_typed_date(self):
        order = self._order()
        year = fields.Date.context_today(order).year + 1
        card = self._card(self._post(order, "hold", {"actor": "wes", "until": f"{year}-03-01", "reason": "Spring"}))
        self.assertEqual(order.grove_hold_until, date(year, 3, 1))
        self.assertEqual(card["hold"], {"until": f"{year}-03-01", "reason": "Spring", "setBy": "wes"})

    def test_hold_route_rejects_bad_date_with_message(self):
        order = self._order()
        response = self._post(order, "hold", {"actor": "wes", "until": "someday", "reason": "Spring"})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json()["error"])
        self.assertFalse(order.grove_hold_until)

    def test_hold_route_rejects_missing_reason_as_422(self):
        order = self._order()
        response = self._post(order, "hold", {"actor": "wes", "until": "2099-01-01", "reason": " "})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(order.grove_hold_until)

    def test_hold_route_rejects_non_string_input_as_422(self):
        order = self._order()
        response = self._post(order, "hold", {"actor": "wes", "until": 12, "reason": ["x"]})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(order.grove_hold_until)

    def test_release_route_clears_hold(self):
        order = self._held()
        card = self._card(self._post(order, "hold/release", {"actor": "wes"}))
        self.assertIsNone(card["hold"])
        self.assertFalse(order.grove_hold_until)

    def test_snooze_route_uses_seven_days_and_ignores_body_days(self):
        order = self._held()
        # The snooze runs server-side as the bearer-auth service user whose
        # context timezone can differ from this test env's, so recomputing
        # ``context_today`` here would drift a day near a UTC/local boundary.
        # Freeze the reference date for both the write and the expectation.
        frozen = date(2026, 6, 15)
        with mock.patch.object(fields.Date, "context_today", return_value=frozen):
            card = self._card(self._post(order, "hold/snooze", {"actor": "wes", "days": 90}))
        expected = frozen + timedelta(days=7)
        self.assertEqual(card["hold"]["until"], expected.isoformat())
        self.assertEqual(order.grove_hold_until, expected)

    def test_snooze_without_hold_is_422(self):
        order = self._order()
        self.assertEqual(self._post(order, "hold/snooze", {"actor": "wes"}).status_code, 422)

    # move, label refunded

    def test_move_route_changes_wave(self):
        order = self._order(grove_ship_wave="fall")
        card = self._card(self._post(order, "move", {"actor": "wes", "wave": "spring"}))
        self.assertEqual(card["shipWave"], "spring")
        self.assertEqual(order.grove_ship_wave, "spring")

    def test_move_route_bad_wave_is_422(self):
        order = self._order(grove_ship_wave="fall")
        self.assertEqual(self._post(order, "move", {"actor": "wes", "wave": "winter"}).status_code, 422)
        self.assertEqual(order.grove_ship_wave, "fall")

    def test_label_refunded_route_clears_label_cost(self):
        order = self._order()
        order.action_grove_record_hand_label(UPS, actual_cost=9.0)
        card = self._post(order, "label-refunded", {"actor": "wes"})
        card = self._card(card)
        self.assertIsNone(card["labelCost"])
        self.assertFalse(order.grove_tracking_numbers)

    def test_label_refunded_route_says_how_many_boxes_were_cleared(self):
        order = self._order()
        order.action_grove_record_hand_label(UPS, actual_cost=9.0)
        order.sudo().write({"grove_tracking_numbers": f"{UPS}\n1Z999AA10123456791"})
        response = self._post(order, "label-refunded", {"actor": "wes"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["boxes_cleared"], 2)

    def test_label_refunded_without_label_is_422(self):
        order = self._order()
        self.assertEqual(self._post(order, "label-refunded", {"actor": "wes"}).status_code, 422)

    # mark-shipped keeps its keys and adds the card

    def test_mark_shipped_returns_card_and_keeps_existing_keys(self):
        order = self._order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled"),
            mock.patch.object(grove_main, "_notify_shipping_status"),
        ):
            first = self._post(order, "mark-shipped", {"actor": "wes", "tracking": UPS, "cost": "9.00"})
            retry = self._post(order, "mark-shipped", {"actor": "wes", "tracking": UPS, "cost": "9.00"})
        for response, already in ((first, False), (retry, True)):
            body = response.json()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(body["already_shipped"], already)
            self.assertIn("tracking_numbers", body)
            self.assertEqual(body["card"]["orderId"], order.id)
            self.assertEqual(body["card"]["labelCost"], 9.0)

    # auth, scope, actor

    def test_routes_need_bearer_auth(self):
        order = self._order()
        response = self._post(order, "hold/snooze", {"actor": "wes"}, auth=False)
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(order.grove_hold_until)

    def test_routes_are_scoped_to_the_website_company(self):
        other = self.env["res.company"].create({"name": "Other Co"})
        order = self._order(company=other, grove_ship_wave="fall")
        order.sudo().write({"grove_hold_until": date(2099, 1, 1), "grove_hold_reason": "x", "grove_hold_set_by": "z"})
        bodies = {
            "hold": {"actor": "wes", "until": "2099-06-01", "reason": "r"},
            "hold/release": {"actor": "wes"},
            "hold/snooze": {"actor": "wes"},
            "move": {"actor": "wes", "wave": "spring"},
            "label-refunded": {"actor": "wes"},
        }
        for path, body in bodies.items():
            self.assertEqual(self._post(order, path, body).status_code, 404, path)
        self.assertEqual(order.grove_hold_until, date(2099, 1, 1))
        self.assertEqual(order.grove_hold_reason, "x")
        self.assertEqual(order.grove_ship_wave, "fall")

    def test_actor_is_required_on_every_route(self):
        order = self._held()
        bodies = {
            "hold": {"until": "2099-06-01", "reason": "r"},
            "hold/release": {"actor": "  "},
            "hold/snooze": {},
            "move": {"wave": "spring"},
            "label-refunded": {"actor": 7},
        }
        for path, body in bodies.items():
            self.assertEqual(self._post(order, path, body).status_code, 422, path)
        self.assertEqual(order.grove_hold_until, date(2027, 3, 1))

    # card payload

    def test_card_payload_carries_lane_zone_and_hold(self):
        order = self._order(grove_ship_wave="fall")
        card = grove_main._order_card_payload(order)
        self.assertEqual(card["lane"], order.grove_lane)
        self.assertEqual(card["zone"], order.grove_usda_zone)
        self.assertEqual(card["shipWave"], "fall")
        self.assertIsNone(card["hold"])
        self.assertIsNone(card["labelCost"])
        self.assertEqual(card["suggestedHold"], order._grove_suggested_hold().isoformat())
        order.action_grove_set_hold(date(2099, 1, 2), "Reason", "Josh")
        order.action_grove_record_hand_label(UPS, actual_cost=9.5)
        card = grove_main._order_card_payload(order)
        self.assertEqual(card["hold"], {"until": "2099-01-02", "reason": "Reason", "setBy": "Josh"})
        self.assertEqual(card["labelCost"], 9.5)

    def test_card_still_builds_when_suggested_hold_raises(self):
        order = self._order(grove_ship_wave="fall")
        with mock.patch.object(type(order), "_grove_suggested_hold", side_effect=RuntimeError("boom")):
            card = grove_main._order_card_payload(order)
        self.assertIsNone(card["suggestedHold"])
        self.assertEqual(card["orderId"], order.id)

    # failure handling

    def test_access_error_is_403_with_generic_body(self):
        order = self._held()
        with mock.patch.object(
            type(order), "action_grove_release_hold", side_effect=AccessError("secret internal detail")
        ):
            response = self._post(order, "hold/release", {"actor": "wes"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("secret internal detail", response.text)
        self.assertTrue(response.json()["error"])

    def test_unexpected_error_is_500_generic_and_writes_nothing(self):
        order = self._held()

        def half_write(rec, actor):
            rec.sudo().write({"grove_hold_reason": "half written"})
            raise RuntimeError("secret boom")

        with mock.patch.object(type(order), "action_grove_release_hold", half_write):
            response = self._post(order, "hold/release", {"actor": "wes"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret boom", response.text)
        self.assertEqual(order.grove_hold_reason, "Wants spring")

    def test_card_failure_after_action_keeps_result_and_returns_null_card(self):
        order = self._held()
        with mock.patch.object(grove_main, "_order_card_payload", side_effect=RuntimeError("card boom")):
            response = self._post(order, "hold/release", {"actor": "wes"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"card": None})
        self.assertFalse(order.grove_hold_until)

    def test_mark_shipped_card_failure_keeps_shipment_and_returns_null_card(self):
        order = self._order()
        with (
            mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled"),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.object(grove_main, "_order_card_payload", side_effect=RuntimeError("card boom")),
        ):
            response = self._post(order, "mark-shipped", {"actor": "wes", "tracking": UPS, "cost": "9.00"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["card"])
        self.assertIn("tracking_numbers", body)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
