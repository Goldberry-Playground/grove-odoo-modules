"""Mark shipped requires a tracking number and a label cost (order lanes).

Wesley's Mark shipped button sends ``{actor, tracking, cost}`` to
``POST /grove/api/v1/orders/<id>/mark-shipped``. What must hold on this
money path:

  * no valid number or cost, nothing happens (422, no write);
  * a held order, or a spring order with no batch label, is refused (409) in
    words Wesley can read;
  * the label is on the order BEFORE settlement runs, so the balance uses the
    real cost and the shipment email carries the number;
  * a retry never charges or emails twice; a different number replaces the
    label and charges nothing more;
  * a label failure writes nothing, and a failure after the label leaves a
    recorded label that can be retried.

Drives the real route through bearer auth. DB-backed, so listed in
tests/__init__.py AND conftest.py's collect_ignore_glob.
"""

import json
from datetime import datetime, timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, get_db_name, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin

UPS = "1Z999AA10123456784"
UPS_2 = "1Z999AA10123456791"
USPS = "9400100000000000000000"


@tagged("post_install", "-at_install")
class TestMarkShippedTracking(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-mark-shipped-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")
        websites = self.env["website"]
        website = websites._resolve_tenant_slug("goldberry") or websites.search([], limit=1)
        self.company = website.company_id
        self.partner = self.env["res.partner"].create(
            {"name": "Ship Customer", "email": "ship@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "American Plum", "type": "consu", "list_price": 22.0}
        )
        settle = mock.patch.object(grove_main, "settle_order_at_ship", return_value="settled")
        notify = mock.patch.object(grove_main, "_notify_shipping_status")
        self.settle = settle.start()
        self.notify = notify.start()
        self.addCleanup(settle.stop)
        self.addCleanup(notify.stop)

    # ── helpers ──────────────────────────────────────────────────────────

    def _order(self, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "grove_fulfillment": "ship",
            "grove_checkout_status": "paid",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
        }
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _deposit_order(self, **vals):
        return self._order(grove_checkout_status="deposit_paid", grove_ship_wave="fall", **vals)

    def _labelled_order(self, tracking=UPS, cost=9.0, **vals):
        order = self._order(**vals)
        order.action_grove_record_hand_label(tracking, actual_cost=cost)
        return order

    def _post(self, order, body):
        self.env.flush_all()
        response = self.url_open(
            f"/grove/api/v1/orders/{order.id}/mark-shipped",
            data=json.dumps(body).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=60,
        )
        self.env.invalidate_all()
        return response

    def _ship(self, order, tracking=UPS, cost="12.40", actor="wesley"):
        return self._post(order, {"actor": actor, "tracking": tracking, "cost": cost})

    def _chatter(self, order):
        return " ".join(order.message_ids.mapped("body"))

    def assertNothingHappened(self, order, stage):
        self.assertEqual(order.grove_fulfillment_stage, stage)
        self.assertFalse(order.grove_tracking_numbers)
        self.settle.assert_not_called()
        self.notify.assert_not_called()

    # ── bad input (422) ──────────────────────────────────────────────────

    def test_endpoint_refuses_missing_tracking(self):
        order = self._order()
        for body in ({"actor": "wesley", "cost": "12.40"}, {"actor": "wesley", "tracking": "  ", "cost": "12.40"}):
            response = self._post(order, body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("tracking", response.json()["error"].lower())
        self.assertNothingHappened(order, "awaiting_label")

    def test_endpoint_refuses_missing_or_zero_cost(self):
        order = self._order()
        for body in (
            {"actor": "wesley", "tracking": UPS},
            {"actor": "wesley", "tracking": UPS, "cost": "0"},
            {"actor": "wesley", "tracking": UPS, "cost": 0},
            {"actor": "wesley", "tracking": UPS, "cost": "free"},
        ):
            response = self._post(order, body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("cost", response.json()["error"].lower())
        self.assertNothingHappened(order, "awaiting_label")

    def test_endpoint_strips_spaces_and_refuses_invalid_number(self):
        order = self._order()
        response = self._ship(order, tracking="1Z-not*a*number")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("1Z-not*a*number", response.json()["error"])
        self.assertNothingHappened(order, "awaiting_label")

        spaced = " ".join([UPS[:6], UPS[6:12], UPS[12:]]) + "\n"
        response = self._ship(order, tracking=spaced)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS)

    def test_endpoint_refuses_wrong_shaped_numbers(self):
        order = self._order()
        for typo in (UPS[:-1], "9400100000", "ABC1234567890"):
            response = self._ship(order, tracking=typo)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(typo, response.json()["error"])
        self.assertNothingHappened(order, "awaiting_label")

    def test_endpoint_uppercases_the_number(self):
        order = self._order()
        response = self._ship(order, tracking=UPS.lower())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS)

    def test_endpoint_refuses_cost_over_the_cap(self):
        order = self._order()
        response = self._ship(order, cost="1240")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("decimal", response.json()["error"].lower())
        self.assertNothingHappened(order, "awaiting_label")

    def test_endpoint_accepts_cost_at_the_cap(self):
        order = self._order()
        response = self._ship(order, cost="250.00")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_actual_shipping_cost, 250.0)

    # ── the normal ship ──────────────────────────────────────────────────

    def test_records_label_then_ships_and_settles_on_real_cost(self):
        order = self._order()
        seen = {}

        def fake_settle(env, settling):
            seen.update(
                cost=settling.grove_actual_shipping_cost,
                tracking=settling.grove_tracking_numbers,
                stage=settling.grove_fulfillment_stage,
            )
            return "settled"

        self.settle.side_effect = fake_settle
        response = self._ship(order, cost="$12.40")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["already_shipped"])
        self.assertEqual(body["settlement"], "settled")
        self.assertEqual(body["tracking_numbers"], [UPS])
        self.assertEqual(body["carriers"], ["UPS"])
        self.assertEqual(seen, {"cost": 12.40, "tracking": UPS, "stage": "shipped"})
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertEqual(order.grove_actual_shipping_cost, 12.40)
        self.notify.assert_called_once()
        self.assertIn("wesley", self._chatter(order))

    def test_usps_number_records_usps(self):
        """The modal has no carrier field, so a number that is not a UPS 1Z must
        still ship: Pirate Ship sells only UPS and USPS."""
        order = self._order()
        response = self._ship(order, tracking=USPS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_shipping_carriers, "USPS")
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    def test_fall_deposit_order_is_assigned_to_wave_then_ships(self):
        order = self._deposit_order()
        self.assertEqual(order.grove_fulfillment_stage, "deposit_paid")
        response = self._ship(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.assertEqual(order.grove_tracking_numbers, UPS)
        self.settle.assert_called_once()
        body = self._chatter(order)
        self.assertIn("Preorder assigned to wave", body)
        self.assertIn("wesley", body)

    def test_deposit_order_with_no_wave_is_refused(self):
        order = self._order(grove_checkout_status="deposit_paid")
        response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(order.name, response.json()["error"])
        self.assertNothingHappened(order, "deposit_paid")

    def test_spring_order_with_batch_label_ships(self):
        order = self._labelled_order(grove_ship_wave="spring")
        response = self._ship(order, cost="9.00")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    # ── retries and corrections ──────────────────────────────────────────

    def test_second_submit_is_one_charge_one_email(self):
        order = self._order()
        first = self._ship(order)
        second = self._ship(order)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(first.json()["already_shipped"])
        self.assertTrue(second.json()["already_shipped"])
        self.settle.assert_called_once()
        self.notify.assert_called_once()
        self.assertEqual(order.grove_tracking_numbers, UPS)

    def test_same_number_on_delivered_order_is_a_no_op(self):
        order = self._labelled_order()
        self.assertTrue(self._ship(order).ok)
        order.action_grove_mark_delivered()
        response = self._ship(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["already_shipped"])
        self.settle.assert_called_once()

    def test_different_number_on_labelled_order_replaces_label(self):
        order = self._labelled_order()
        response = self._ship(order, tracking=UPS_2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS_2)
        self.assertEqual(order.grove_actual_shipping_cost, 12.40)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.settle.assert_called_once()
        body = self._chatter(order)
        self.assertIn(UPS, body, "chatter keeps the old number")
        self.assertIn("wesley", body)

    def test_same_number_with_new_cost_on_unshipped_order_uses_typed_cost(self):
        """The modal is prefilled from the label; a corrected cost is the one the
        customer's balance must settle on."""
        order = self._labelled_order(cost=9.0)
        seen = {}

        def fake_settle(env, settling):
            seen["cost"] = settling.grove_actual_shipping_cost
            return "settled"

        self.settle.side_effect = fake_settle
        response = self._ship(order, cost="12.40")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen["cost"], 12.40)
        self.assertIn("wesley", self._chatter(order))

    def test_cost_correction_on_settled_order_says_balance_was_charged(self):
        order = self._labelled_order(cost=9.0, grove_checkout_status="settled")
        self.assertTrue(self._ship(order, cost="12.40").ok)
        body = self._chatter(order)
        self.assertIn("already charged", body)
        self.assertIn("$9.00", body)

    def test_label_replaced_on_settled_order_says_balance_was_charged(self):
        order = self._labelled_order(cost=9.0, grove_checkout_status="settled")
        self.assertTrue(self._ship(order, tracking=UPS_2, cost="12.40").ok)
        body = self._chatter(order)
        self.assertIn("already charged", body)
        self.assertIn("$9.00", body)

    def test_cost_correction_on_unsettled_order_makes_no_charged_claim(self):
        order = self._labelled_order(cost=9.0)
        self.assertTrue(self._ship(order, cost="12.40").ok)
        self.assertNotIn("already charged", self._chatter(order))

    def test_different_number_on_shipped_order_replaces_and_charges_nothing_more(self):
        order = self._order()
        self.assertTrue(self._ship(order).ok)
        response = self._ship(order, tracking=UPS_2, cost="14.00")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["already_shipped"])
        self.assertEqual(order.grove_tracking_numbers, UPS_2)
        self.assertEqual(order.grove_actual_shipping_cost, 14.0)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.settle.assert_called_once()
        self.notify.assert_called_once()

    # ── state refusals (409) ─────────────────────────────────────────────

    def test_held_order_refused(self):
        order = self._order()
        until = fields.Date.context_today(order) + timedelta(days=30)
        order.action_grove_set_hold(until, "Waiting for dormancy", "josh")
        response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(f"{until:%b} {until.day}, {until.year}", response.json()["error"])
        self.assertNothingHappened(order, "awaiting_label")

    def test_spring_order_without_label_refused(self):
        order = self._order(grove_ship_wave="spring", grove_checkout_status="deposit_paid")
        order.grove_usda_zone = "6"
        response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Zone 6", response.json()["error"])
        self.assertNothingHappened(order, "deposit_paid")

    def test_number_already_on_another_order_refused(self):
        other = self._labelled_order()
        order = self._deposit_order()
        response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(other.name, response.json()["error"])
        self.assertIn(UPS, response.json()["error"])
        # The wave assignment that preceded the refusal is rolled back with it.
        self.assertNothingHappened(order, "deposit_paid")
        self.assertFalse(order.grove_actual_shipping_cost)
        self.assertNotIn("Preorder assigned to wave", self._chatter(order))
        self.assertEqual(other.grove_tracking_numbers, UPS)
        self.assertEqual(other.grove_fulfillment_stage, "label_purchased")

    def test_number_on_a_cancelled_order_is_not_a_duplicate(self):
        odoo_cancelled = self._labelled_order(tracking=UPS)
        odoo_cancelled.write({"state": "cancel"})
        grove_cancelled = self._labelled_order(tracking=UPS_2)
        grove_cancelled.write({"grove_fulfillment_state": "cancelled"})
        for number in (UPS, UPS_2):
            order = self._order()
            response = self._ship(order, tracking=number)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(order.grove_tracking_numbers, number)

    def test_number_on_another_company_is_not_a_duplicate(self):
        other_company = self.env["res.company"].create({"name": "Other Grove Co"})
        partner = self.env["res.partner"].create({"name": "Other Customer", "company_id": False})
        foreign = (
            self.env["sale.order"]
            .with_company(other_company)
            .create({"partner_id": partner.id, "company_id": other_company.id})
        )
        foreign.write({"grove_tracking_numbers": UPS})
        order = self._order()
        response = self._ship(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS)
        self.assertEqual(foreign.grove_tracking_numbers, UPS)

    def test_number_that_only_contains_another_is_not_a_duplicate(self):
        self._labelled_order(tracking=f"{UPS}99")
        order = self._order()
        response = self._ship(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS)

    def assertPlainRefusal(self, response, *expected):
        self.assertEqual(response.status_code, 409, response.text)
        error = response.json()["error"]
        for text in expected:
            self.assertIn(text, error)
        for jargon in ("stage", "ship wave", "awaiting_label", "label_purchased"):
            self.assertNotIn(jargon, error)

    def test_different_number_on_delivered_order_refused_in_plain_words(self):
        order = self._order()
        self.assertTrue(self._ship(order).ok)
        order.action_grove_mark_delivered()
        self.assertPlainRefusal(self._ship(order, tracking=UPS_2), order.name, UPS, "Delivered")
        self.assertEqual(order.grove_tracking_numbers, UPS)
        self.settle.assert_called_once()

    def test_shipped_order_with_no_tracking_refused_in_plain_words(self):
        order = self._order()
        order.action_grove_mark_shipped(source="shippo")
        self.assertPlainRefusal(self._ship(order), order.name, "no tracking number")
        self.assertFalse(order.grove_tracking_numbers)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.settle.assert_not_called()

    def test_delivered_order_with_no_tracking_refused_in_plain_words(self):
        order = self._order()
        order.action_grove_mark_shipped(source="shippo")
        order.action_grove_mark_delivered()
        self.assertPlainRefusal(self._ship(order), order.name, "no tracking number")
        self.assertFalse(order.grove_tracking_numbers)

    def test_no_transition_refusal_reads_plainly(self):
        order = self._order()
        with mock.patch.object(
            self.env.registry["sale.order"],
            "_grove_ship_with_label",
            return_value={"newly_shipped": False, "settlement": None},
        ):
            response = self._ship(order)
        self.assertPlainRefusal(response, order.name, "Paid — awaiting label")

    def test_pickup_order_refused(self):
        order = self._order(grove_fulfillment="pickup")
        response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.settle.assert_not_called()

    # ── failure handling ─────────────────────────────────────────────────

    def test_label_failure_writes_nothing(self):
        order = self._deposit_order()
        with mock.patch.object(
            self.env.registry["sale.order"], "action_grove_record_hand_label", side_effect=UserError("label refused")
        ):
            response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"], "label refused")
        # The wave assignment that preceded the failure is rolled back with it.
        self.assertNothingHappened(order, "deposit_paid")
        self.assertNotIn("Preorder assigned to wave", self._chatter(order))

    def test_failure_after_label_leaves_label_and_is_retryable(self):
        order = self._order()
        with mock.patch.object(grove_main, "_operator_mark_shipped", side_effect=UserError("not today")):
            response = self._ship(order)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(order.grove_tracking_numbers, UPS)
        self.assertEqual(order.grove_fulfillment_stage, "label_purchased")

        response = self._ship(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
        self.settle.assert_called_once()
        self.notify.assert_called_once()

    # ── Odoo's own Mark shipped ──────────────────────────────────────────

    def test_odoo_server_action_refuses_without_tracking(self):
        order = self._order()
        with self.assertRaises(UserError):
            order._grove_mark_shipped_and_settle(operator="josh")
        self.assertEqual(order.grove_fulfillment_stage, "awaiting_label")
        self.settle.assert_not_called()

    def test_odoo_server_action_still_ships_a_labelled_order(self):
        order = self._labelled_order()
        result = order._grove_mark_shipped_and_settle(operator="josh")
        self.assertTrue(result["newly_shipped"])
        self.assertEqual(order.grove_fulfillment_stage, "shipped")

    def test_odoo_server_action_still_skips_pickup_without_raising(self):
        order = self._order(grove_fulfillment="pickup")
        self.assertFalse(order._grove_mark_shipped_and_settle(operator="josh")["newly_shipped"])

    def _run_server_action(self, orders):
        action = self.env.ref("grove_headless.action_grove_mark_shipped")
        return action.with_context(active_model="sale.order", active_ids=orders.ids).run()

    def _waved_deposit_with_label(self):
        order = self._deposit_order()
        order.action_grove_assign_wave()
        order.action_grove_record_hand_label(UPS, actual_cost=9.0)
        return order

    def test_server_action_checks_the_whole_selection_before_shipping_any(self):
        """Raising mid-loop would roll back a charge that really happened."""
        labelled = self._waved_deposit_with_label()
        missing_one = self._order()
        missing_two = self._order()
        with self.assertRaises(UserError) as caught:
            self._run_server_action(labelled | missing_one | missing_two)
        message = str(caught.exception)
        self.assertIn(missing_one.name, message)
        self.assertIn(missing_two.name, message)
        self.assertNotIn(labelled.name, message)
        self.settle.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(labelled.grove_fulfillment_stage, "label_purchased")

    def test_server_action_refuses_up_front_when_compliance_is_missing(self):
        """The compliance gate also raises from the loop, so it is checked first too."""
        labelled = self._labelled_order()
        blocked = self._labelled_order(tracking=UPS_2)
        with mock.patch.object(
            self.env.registry["sale.order"],
            "_grove_consult_compliance_missing",
            lambda order: order.id == blocked.id,
        ):
            with self.assertRaises(UserError) as caught:
                self._run_server_action(labelled | blocked)
        self.assertIn(blocked.name, str(caught.exception))
        self.settle.assert_not_called()
        self.notify.assert_not_called()

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_server_action_no_op_stages_do_not_block_the_others(self):
        labelled = self._labelled_order()
        no_ops = (
            self._order(grove_checkout_status="deposit_paid")  # no wave, no label
            | self._order(grove_checkout_status="expired")  # cancelled
            | self._order(grove_checkout_status=False)  # awaiting payment
        )
        self.assertEqual(
            sorted(no_ops.mapped("grove_fulfillment_stage")),
            ["awaiting_payment", "cancelled", "deposit_paid"],
        )
        stages = no_ops.mapped("grove_fulfillment_stage")
        self._run_server_action(no_ops | labelled)
        self.assertEqual(labelled.grove_fulfillment_stage, "shipped")
        self.assertEqual(no_ops.mapped("grove_fulfillment_stage"), stages)
        self.settle.assert_called_once()
        self.notify.assert_called_once()

    def test_carrier_signal_is_not_gated(self):
        """The carrier poll and webhooks call action_grove_mark_shipped with no
        operator; only the operator paths need a number."""
        order = self._order()
        self.assertTrue(order.action_grove_mark_shipped(source="shippo"))
        self.assertEqual(order.grove_fulfillment_stage, "shipped")
