"""Consult-built mix compliance backstop (GOL-3007).

Templates 134/135 are *consult-built*: the customer pays a $10 deposit, then
Wesley and the customer choose the species list together, then the balance is
charged and it ships. The checkout taxon gate has nothing to evaluate at
checkout (the carton does not exist yet) and correctly fail-safe-blocks the nine
regulated states for these SKUs (GOL-2971). The control that makes a real mix
safe is a HUMAN check at mix-build time, recorded on the order. This is the
system backstop for that SOP: an order carrying a ``grove_consult_built`` line
cannot settle its balance / ship until the check is recorded in
``grove_substitution_note``.

Deposit vs balance seam (the mechanism, named for the handback): the $10 deposit
is a Stripe *Checkout Session* taken at checkout and recorded as
``grove_amount_charged_today`` with status ``deposit_paid`` — it never runs
through the gate. The deferred BALANCE is the off-session charge in
``settle_order_at_ship`` (reached only from a ship-commit, and only for status
``deposit_paid``/``settlement_failed``). The gate attaches to the ship-commit
actions (hard ``UserError``) and to settlement (non-raising hold for the retry
cron / bulk reconcile), so the deposit is structurally untouched.

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is
listed in tests/__init__.py AND excluded from pytest in conftest.py — the
pattern every TransactionCase here follows so it is not double-skipped (GOL-1936).
"""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestConsultComplianceGate(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Consult Customer", "email": "consult@example.com", "company_id": self.company.id}
        )
        # Stand-in for template 134/135: a consult-built mix. On prod the flag is
        # set on 134/135 as master data; here we tick it on a fresh product so the
        # test asserts the FLAG's behaviour, not two database ids (GOL-3007 §1).
        self.consult = self.env["product.product"].create(
            {"name": "Centennial Food Forest (100)", "type": "consu", "is_storable": True, "list_price": 400.0}
        )
        self.consult.product_tmpl_id.grove_consult_built = True
        # An ordinary SKU — the regression control.
        self.plain = self.env["product.product"].create(
            {"name": "American Plum (bareroot)", "type": "consu", "is_storable": True, "list_price": 22.0}
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _order(self, product, note=False, **vals):
        base = {
            "partner_id": self.partner.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1.0})],
        }
        if note:
            base["grove_substitution_note"] = note
        base.update(vals)
        return self.env["sale.order"].with_company(self.company).create(base)

    def _seed_wv_tax(self, product):
        wv_state = self.env["account.tax"].search(
            [("name", "=", "WV State Sales Tax 6%"), ("amount_type", "=", "percent")], limit=1
        )
        self.assertTrue(wv_state, "WV state tax must exist (GroveTaxFixtureMixin)")
        product.product_tmpl_id.taxes_id = [(6, 0, wv_state.ids)]
        return wv_state

    def _deposit_ship_order_at_label(self, product, note=False):
        """A consult deposit-only order whose watermark reached label_purchased
        but whose payment is still ``deposit_paid`` — the state a mark-shipped
        lands on, where the balance would settle."""
        return self._order(
            product,
            note=note,
            grove_fulfillment="ship",
            grove_checkout_status="deposit_paid",
            grove_fulfillment_state="label_purchased",
            grove_amount_charged_today=10.0,
            grove_actual_shipping_cost=9.0,
            grove_stripe_customer="cus_test",
            grove_stripe_payment_method="pm_test",
        )

    # ── the predicate (one source of truth) ──────────────────────────────

    def test_predicate_true_only_for_consult_line_without_note(self):
        self.assertTrue(self._order(self.consult)._grove_consult_compliance_missing())
        self.assertFalse(self._order(self.consult, note="OH; checked; Wesley")._grove_consult_compliance_missing())
        self.assertFalse(self._order(self.plain)._grove_consult_compliance_missing())
        # Whitespace is not a recorded check.
        self.assertTrue(self._order(self.consult, note="   \n\t ")._grove_consult_compliance_missing())

    # ── AC2: the balance cannot be invoiced without the check ─────────────

    @mute_logger("odoo.addons.grove_headless.models.sale_order")
    def test_mark_shipped_blocked_without_note(self):
        """A 134/135 order with an empty note CANNOT be marked shipped — the move,
        the ship-time settlement and the shipment email are all refused as a unit
        (AC2). The message is the instruction Wesley reads."""
        order = self._deposit_ship_order_at_label(self.consult)
        with self.assertRaises(UserError) as ctx:
            order.action_grove_mark_shipped(operator="josh")
        self.assertIn("Compliance check required", str(ctx.exception))
        self.assertIn("Compliance check / substitutions", str(ctx.exception))
        # Nothing moved.
        self.assertNotEqual(order.grove_fulfillment_stage, "shipped")
        self.assertEqual(order.grove_checkout_status, "deposit_paid")

    def test_hand_label_and_label_buy_blocked_without_note(self):
        """The other ship-commit entries refuse a consult order with no note too,
        so no label is ever bought before the check exists."""
        order = self._order(self.consult, grove_fulfillment="ship")
        with self.assertRaises(UserError):
            order.action_grove_record_hand_label("1Z999AA10123456784", carrier="UPS", actual_cost=9.0)
        with mock.patch.dict("os.environ", {"SHIPPO_API_KEY": "shippo_test"}, clear=False):
            with self.assertRaises(UserError):
                order.action_buy_shipping_labels()
        self.assertFalse(order.grove_tracking_numbers)

    # ── AC3: a recorded check lets the balance settle normally ────────────

    def test_mark_shipped_settles_when_note_present(self):
        """The same order with a non-empty note ships and settles normally (AC3):
        the gate is a no-op and the deferred balance is captured."""
        self._seed_wv_tax(self.consult)
        order = self._deposit_ship_order_at_label(
            self.consult, note="FL->substituted hickory; checked; Wesley 2026-10-05"
        )
        charges = []

        def fake_pi(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_consult_settle", "status": "succeeded"}

        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=fake_pi),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="josh", source="operator")

        self.assertTrue(result["newly_shipped"])
        self.assertEqual(result["settlement"], "settled")
        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(len(charges), 1, "the balance is captured exactly once")

    # ── AC4: an order with no consult line is unaffected ──────────────────

    def test_plain_order_unaffected(self):
        """A regression control: a normal (non-consult) order with an empty note
        is never gated — mark-shipped proceeds."""
        order = self._deposit_ship_order_at_label(self.plain)
        order._grove_assert_consult_compliance()  # does not raise
        ok_pi = {"id": "pi_x", "status": "succeeded"}
        self._seed_wv_tax(self.plain)
        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", return_value=ok_pi),
            mock.patch.object(grove_main, "_notify_shipping_status"),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            result = grove_main._operator_mark_shipped(self.env, order, actor="josh", source="operator")
        self.assertTrue(result["newly_shipped"])

    # ── AC5: the deposit is intact; only the BALANCE is held ──────────────

    def test_settlement_holds_balance_but_leaves_deposit_intact(self):
        """The automated settlement caller (retry cron / bulk reconcile) must not
        raise. For a consult mix with no note it HOLDS — returns ``compliance_hold``,
        charges nothing, keeps status ``deposit_paid`` so it re-settles once the
        check is recorded — and the $10 deposit already taken is untouched (AC5)."""
        self._seed_wv_tax(self.consult)
        order = self._deposit_ship_order_at_label(self.consult)  # no note
        with (
            mock.patch.object(stripe_gateway, "create_payment_intent") as pi,
            mock.patch.object(grove_main, "_notify_discord") as notify,
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            status = grove_main.settle_order_at_ship(self.env, order)
        self.assertEqual(status, "compliance_hold")
        pi.assert_not_called()  # no balance charge
        self.assertEqual(order.grove_checkout_status, "deposit_paid")  # re-settles after the check
        self.assertEqual(order.grove_amount_charged_today, 10.0)  # the deposit is intact
        notify.assert_called_once()  # ops is told, not silent

    def test_settlement_proceeds_after_note_recorded(self):
        """Once Wesley records the check, the held balance settles (the forcing
        function completes): same order, note now present → ``settled``."""
        self._seed_wv_tax(self.consult)
        order = self._deposit_ship_order_at_label(self.consult, note="WV; all clear; Wesley")
        ok_pi = {"id": "pi_ok", "status": "succeeded"}
        with (
            mock.patch.object(stripe_gateway, "create_payment_intent", return_value=ok_pi),
            mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False),
        ):
            status = grove_main.settle_order_at_ship(self.env, order)
        self.assertEqual(status, "settled")
        self.assertEqual(order.grove_checkout_status, "settled")

    # ── AC7: the checkout gate is unchanged for a consult-built SKU ────────

    def _cart_payload(self, state, **extra):
        payload = {
            "contact": {"name": "Consult Test", "email": "consult-cart@example.com", "phone": "3045551212"},
            "items": [{"variant_id": self.consult.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "10001"},
        }
        payload.update(extra)
        return payload

    def _website(self):
        return self.env["website"].search([("company_id", "=", self.company.id)], limit=1) or self.env[
            "website"
        ].search([], limit=1)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_checkout_still_failsafe_blocks_regulated_state(self):
        """AC7: ticking ``grove_consult_built`` does NOT relax the checkout taxon
        gate. A consult SKU (unparseable bundle botanical, no kit BoM) into a
        regulated green state still fail-safe-blocks at 400 — the nine-state block
        is additive to, and untouched by, this change."""
        tmpl = self.consult.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_botanical_name = "Bundle: Castanea spp., Cornus florida"
        payload = self._cart_payload("OH", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't confirm", error.data.decode().lower())

    def test_checkout_deposit_path_unaffected_in_green_state(self):
        """The deposit checkout for a consult SKU into a non-regulated green state
        still creates the order — the gate never touches the checkout/deposit path
        (AC5, checkout side)."""
        tmpl = self.consult.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_botanical_name = "Bundle: Castanea spp., Cornus florida"
        payload = self._cart_payload("WV", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)

    # ── GOL-3019: deposit-time deferral for consult-built SKUs ────────────
    # Templates 134/135 carry an EMPTY botanical (correct — the mix does not exist
    # at deposit time, GOL-2972), so the fail-safe above blocks the four
    # regulated∩green states FL/IN/OH/WI. Now that GOL-3007 enforces the mix-build
    # check at ship-commit, those four are recoverable: take the $10 deposit and
    # record a deferral instead of blocking. The whole feature is behind one flag
    # (AC5), default-off, self-guarded on the GOL-3007 assert (§3).

    def _arm_deferral(self):
        self.env["ir.config_parameter"].sudo().set_param("grove_headless.consult_deferral_enabled", "True")

    def _empty_consult(self):
        """The real 134/135 prod shape: consult-built, EMPTY botanical, bareroot
        so it clears the potted gate and reaches the carve-out loop."""
        tmpl = self.consult.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_botanical_name = False
        return tmpl

    def test_ac1_deposit_accepted_into_regulated_state_when_armed(self):
        """AC1: an armed consult-built mix into FL returns an order (deposit path)
        rather than the fail-safe 400, and the deferral is RECORDED on the order —
        destination state captured, chatter names the taxa constrained there."""
        self._empty_consult()
        self._arm_deferral()
        payload = self._cart_payload("FL", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertTrue(order.grove_consult_compliance_deferred)
        self.assertEqual(order.grove_consult_deferred_state, "FL")
        body = "".join((order.message_ids.mapped("body") or []))
        self.assertIn("castanea", body)
        self.assertIn("cornus", body)

    def test_ac2_deferred_order_still_cannot_ship(self):
        """AC2: the deferral does NOT satisfy the ship-commit gate — the recorded
        deferral lives on separate fields, grove_substitution_note stays empty, so
        the order still owes a human check and mark-shipped still raises."""
        self._empty_consult()
        self._arm_deferral()
        payload = self._cart_payload("IN", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertFalse((order.grove_substitution_note or "").strip())
        self.assertTrue(order._grove_consult_compliance_missing())
        with self.assertRaises(UserError):
            order._grove_assert_consult_compliance()

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_ac3_non_consult_empty_botanical_still_blocks_when_armed(self):
        """AC3: the deferral keys on grove_consult_built ONLY. With the flag ON, a
        NON-consult template with an empty botanical into FL still hard-blocks —
        emptiness alone never defers."""
        tmpl = self.plain.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_botanical_name = False
        self._arm_deferral()
        payload = {
            "contact": {"name": "Plain", "email": "plain-cart@example.com", "phone": "3045551212"},
            "items": [{"variant_id": self.plain.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": "FL", "zip": "33101"},
            "fulfillment": "ship",
        }
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't confirm", error.data.decode().lower())

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_self_guard_blocks_when_flag_off(self):
        """§3 self-guard (flag side): with the deferral flag OFF (default), a
        consult-built empty-botanical mix into FL still fail-safe-blocks — merging
        the code changes nothing until the flag is explicitly set (AC5 reversible)."""
        self._empty_consult()  # flag NOT armed
        payload = self._cart_payload("FL", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)

    def test_deferral_in_unregulated_state_records_no_exclusions(self):
        """An armed consult mix into a non-regulated green state (WV) also records
        the deferral, naming no constrained taxa — the mix is fully deliverable
        there and the record says so honestly."""
        self._empty_consult()
        self._arm_deferral()
        payload = self._cart_payload("WV", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order.grove_consult_compliance_deferred)
        self.assertEqual(order.grove_consult_deferred_state, "WV")
        body = "".join((order.message_ids.mapped("body") or []))
        self.assertIn("none for this state", body)
