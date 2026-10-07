"""Ship-time settlement failure handling — the three-bucket outcome matrix (GOL-3011).

CEO ruling 2026-10-05 (Josh): the deposit-balance settlement must classify every
Stripe outcome and heal itself on the next run. The first live settlement (prod
S00357, $63.14) failed because the nursery rk_live key lacked Payment Intents:
Write — a *known-not-charged* permission error that the old code left silently
stuck in deposit_paid (the retry cron only selected settlement_failed), so it
never retried despite the chatter promising "will retry".

Every settlement outcome now falls into one of three buckets, each handled
explicitly:
  1. the card could not be charged (decline / SCA / no card) → settlement_failed,
     dun the customer, auto-retry daily (SCA skips auto-retry — needs the customer);
  2. an error where we KNOW no charge was made (4xx invalid_request / permission /
     auth) → settlement_error, NO dunning (not the customer's fault), cron retries
     with a fresh key;
  3. the outcome is UNKNOWN (timeout / 5xx / connection reset) → settlement_error +
     reconcile flag, and the retry reconciles with Stripe before charging so a
     first attempt that actually succeeded is never double-charged.

Stripe stays mocked at the gateway boundary (create_payment_intent /
search_payment_intents), never deeper — the tests exercise the real classification,
state transitions and reconcile logic.

Runs under Odoo's --test-enable runner (needs a DB for sale.order), so it is listed
in tests/__init__.py AND excluded from pytest in conftest.py (GOL-1936).
"""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestSettlementFailureHandling(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Settle Customer", "email": "settle@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Persimmon 'Prok'", "type": "consu", "is_storable": True, "list_price": 25.0}
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _ship_order(self, **vals):
        """A deposit-paid SHIP order primed for ship-time settlement: a GROVE-SHIP
        line (so a shipping/handling breakdown is expected), the deposit taken, a
        saved card on file, and the actual label cost known."""
        order = (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2.0})],
                }
            )
        )
        ship_product = grove_main._get_shipping_product(self.env, self.company)
        order.write({"order_line": [(0, 0, {"product_id": ship_product.id, "product_uom_qty": 1, "price_unit": 12.5})]})
        base = {
            "grove_fulfillment": "ship",
            "grove_checkout_status": "deposit_paid",
            "grove_amount_charged_today": 10.0,
            "grove_actual_shipping_cost": 15.0,
            "grove_stripe_customer": "cus_test",
            "grove_stripe_payment_method": "pm_test",
        }
        base.update(vals)
        order.write(base)
        return order

    def _env(self):
        return mock.patch.dict("os.environ", {"stripe_test_secret_key": "sk_test"}, clear=False)

    def _no_stripe_tax(self):
        # Flag OFF: balance is Odoo's amount_total − deposit, no Stripe Tax calls.
        return mock.patch.object(grove_main, "_stripe_tax_enabled", return_value=False)

    # ── bucket 1: card could not be charged ──────────────────────────────

    def test_card_decline_marks_failed_and_duns(self):
        """A plain decline → settlement_failed, dunning email sent, auto-retry kept
        (needs_customer stays False), reconcile NOT set."""
        order = self._ship_order()

        def decline(secret_key, **kwargs):
            raise stripe_gateway.StripeCardError(
                "declined", code="card_declined", decline_code="do_not_honor", payment_intent="pi_bad"
            )

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=decline),
            mock.patch.object(grove_main, "_send_dunning_email") as dun,
            mock.patch.object(stripe_gateway, "create_checkout_session", return_value={"url": "https://pay/x"}),
            self._env(),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settlement_failed")
        self.assertEqual(order.grove_checkout_status, "settlement_failed")
        self.assertFalse(order.grove_settlement_needs_customer)
        self.assertFalse(order.grove_settlement_reconcile)
        self.assertEqual(order.grove_settlement_attempts, 1)
        dun.assert_called_once()  # a decline duns the customer

    def test_authentication_required_needs_customer_no_autoretry(self):
        """An SCA (authentication_required) decline → settlement_failed BUT flagged
        needs_customer so the cron never auto-retries (an off-session retry can't
        clear SCA); the customer is dunned to pay via the link."""
        order = self._ship_order()

        def sca(secret_key, **kwargs):
            raise stripe_gateway.StripeCardError(
                "auth required", code="authentication_required", payment_intent="pi_sca"
            )

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=sca),
            mock.patch.object(stripe_gateway, "create_checkout_session", return_value={"url": "https://pay/x"}),
            self._env(),
            mute_logger("odoo.addons.mail.models.mail_mail"),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settlement_failed")
        self.assertTrue(order.grove_settlement_needs_customer, "SCA needs the customer")
        # The cron must skip an SCA order even with retries remaining.
        order.write({"grove_settlement_attempts": 1})
        self.env["ir.config_parameter"].sudo().set_param("grove_headless.settlement_max_retries", "3")
        pi = mock.Mock()
        with mock.patch.object(stripe_gateway, "create_payment_intent", pi):
            self.env["sale.order"]._cron_retry_settlements()
        pi.assert_not_called()

    def test_no_saved_card_marks_failed(self):
        """No saved card and none recoverable from the deposit intent → the card
        could not be charged → settlement_failed, no charge attempted."""
        order = self._ship_order(grove_stripe_customer=False, grove_stripe_payment_method=False)
        pi = mock.Mock()
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", pi),
            mock.patch.object(stripe_gateway, "create_checkout_session", return_value={"url": "https://pay/x"}),
            self._env(),
            mute_logger("odoo.addons.mail.models.mail_mail"),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)
        self.assertEqual(result, "settlement_failed")
        pi.assert_not_called()

    # ── bucket 2: known not charged (do NOT dun) ─────────────────────────

    def test_permission_error_is_settlement_error_no_dunning(self):
        """The S00357 case: a restricted key refused with HTTP 403 invalid_request.
        Stripe rejected BEFORE charging → settlement_error, reconcile=False, and the
        customer is NOT dunned (not their fault). The order becomes retryable."""
        order = self._ship_order()

        def perm(secret_key, **kwargs):
            raise stripe_gateway.StripeError(
                "key lacks Payment Intents: Write", http_status=403, error_type="invalid_request_error"
            )

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=perm),
            mock.patch.object(grove_main, "_send_dunning_email") as dun,
            self._env(),
            mute_logger("odoo.addons.grove_headless.controllers.main"),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settlement_error")
        self.assertEqual(order.grove_checkout_status, "settlement_error")
        self.assertFalse(order.grove_settlement_reconcile, "a known-not-charged error does not need reconciliation")
        self.assertEqual(order.grove_settlement_attempts, 1)
        dun.assert_not_called()  # a known-not-charged error must NOT dun the customer

    def test_known_not_charged_retry_uses_a_fresh_key(self):
        """A known-not-charged error rotates the idempotency key per attempt (safe —
        no charge exists), so a retry is a genuinely new charge, not a replay."""
        order = self._ship_order()

        def perm(secret_key, **kwargs):
            raise stripe_gateway.StripeError("nope", http_status=400, error_type="invalid_request_error")

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=perm),
            self._env(),
            mute_logger("odoo.addons.grove_headless.controllers.main"),
        ):
            grove_main.settle_order_at_ship(self.env, order)
        first_key = order.grove_settlement_idem_key

        captured = {}

        def ok(secret_key, **kwargs):
            captured.update(kwargs)
            return {"id": "pi_ok", "status": "succeeded"}

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=ok),
            self._env(),
        ):
            grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertNotEqual(captured["idempotency_key"], first_key, "a new attempt gets a new key")

    # ── bucket 3: outcome unknown (reconcile before retry) ───────────────

    def test_timeout_parks_reconciling_without_dunning(self):
        """A transport timeout (no response) → outcome UNKNOWN: settlement_error +
        reconcile flag + the idempotency key preserved, customer NOT dunned."""
        order = self._ship_order()

        def timeout(secret_key, **kwargs):
            raise stripe_gateway.StripeError("no response (timeout)", http_status=None, error_type="connection_error")

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=timeout),
            mock.patch.object(grove_main, "_send_dunning_email") as dun,
            self._env(),
            mute_logger("odoo.addons.grove_headless.controllers.main"),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settlement_error")
        self.assertTrue(order.grove_settlement_reconcile, "an unknown outcome must flag reconcile")
        self.assertTrue(order.grove_settlement_idem_key, "the in-flight key is preserved for a 24h replay")
        dun.assert_not_called()  # an unknown error must NOT dun the customer

    def test_5xx_is_unknown_and_reconciles(self):
        """A 5xx mid-request also leaves the outcome unknown → reconcile flagged."""
        order = self._ship_order()

        def boom(secret_key, **kwargs):
            raise stripe_gateway.StripeError("gateway 503", http_status=503, error_type=None)

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=boom),
            self._env(),
            mute_logger("odoo.addons.grove_headless.controllers.main"),
        ):
            grove_main.settle_order_at_ship(self.env, order)
        self.assertEqual(order.grove_checkout_status, "settlement_error")
        self.assertTrue(order.grove_settlement_reconcile)

    def test_reconcile_finds_prior_success_never_double_charges(self):
        """THE double-charge guard: the first attempt actually SUCCEEDED at Stripe
        but we lost the answer (timeout). On retry, reconcile finds the succeeded
        intent and marks the order settled WITHOUT creating a second charge."""
        order = self._ship_order(
            grove_checkout_status="settlement_error",
            grove_settlement_reconcile=True,
            grove_settlement_idem_key="grove-settle-x-1",
            grove_settlement_attempts=1,
        )
        pi = mock.Mock()
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", pi),
            mock.patch.object(
                stripe_gateway,
                "search_payment_intents",
                return_value=[{"id": "pi_prior", "status": "succeeded"}],
            ),
            self._env(),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settled")
        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(order.grove_settlement_payment_intent, "pi_prior")
        self.assertFalse(order.grove_settlement_reconcile)
        pi.assert_not_called()  # the headline: NEVER a second charge

    def test_reconcile_no_prior_charge_recharges_with_same_key(self):
        """When Stripe confirms no charge exists, the retry DOES charge — reusing the
        SAME idempotency key so a search index-lag can't let it double-charge."""
        order = self._ship_order(
            grove_checkout_status="settlement_error",
            grove_settlement_reconcile=True,
            grove_settlement_idem_key="grove-settle-x-1",
            grove_settlement_attempts=1,
        )
        captured = {}

        def ok(secret_key, **kwargs):
            captured.update(kwargs)
            return {"id": "pi_new", "status": "succeeded"}

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=ok),
            mock.patch.object(stripe_gateway, "search_payment_intents", return_value=[]),
            self._env(),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)

        self.assertEqual(result, "settled")
        self.assertEqual(captured["idempotency_key"], "grove-settle-x-1", "reuse the stored key while reconciling")

    def test_reconcile_unreachable_stays_parked(self):
        """If the reconcile search itself fails (network), we cannot confirm no
        charge exists, so we HOLD — never charge blind."""
        order = self._ship_order(
            grove_checkout_status="settlement_error",
            grove_settlement_reconcile=True,
            grove_settlement_idem_key="grove-settle-x-1",
            grove_settlement_attempts=1,
        )
        pi = mock.Mock()
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", pi),
            mock.patch.object(
                stripe_gateway, "search_payment_intents", side_effect=stripe_gateway.StripeError("search down")
            ),
            self._env(),
            mute_logger("odoo.addons.grove_headless.controllers.main"),
        ):
            result = grove_main.settle_order_at_ship(self.env, order)
        self.assertEqual(result, "settlement_error")
        pi.assert_not_called()

    # ── success + pickup wording ─────────────────────────────────────────

    def test_success_chatter_names_shipping_for_ship_order(self):
        """A ship order's success chatter describes the actual shipping + handling."""
        order = self._ship_order()
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", return_value={"id": "pi_ok"}),
            self._env(),
        ):
            self.assertEqual(grove_main.settle_order_at_ship(self.env, order), "settled")
        note = order.message_ids.filtered(lambda m: "settlement captured" in (m.body or "").lower())
        self.assertTrue(note)
        self.assertIn("handling", (note[0].body or "").lower())

    def test_success_chatter_omits_shipping_for_pickup_order(self):
        """A PICKUP order has no GROVE-SHIP line: the chatter must NOT claim a
        shipping/handling line that does not exist (GOL-3011)."""
        order = self._ship_order(grove_fulfillment="pickup")
        # Strip the GROVE-SHIP line so the order looks like a real pickup order.
        ship_line = grove_main._settlement_shipping_line(order)
        ship_line.unlink()
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", return_value={"id": "pi_ok"}),
            self._env(),
        ):
            self.assertEqual(grove_main.settle_order_at_ship(self.env, order), "settled")
        note = order.message_ids.filtered(lambda m: "settlement captured" in (m.body or "").lower())
        self.assertTrue(note)
        body = (note[0].body or "").lower()
        self.assertNotIn("handling", body, "pickup carries no handling line")
        self.assertIn("pickup", body)

    # ── cron: selects both states, double-fire safety ────────────────────

    def test_cron_selects_settlement_error_too(self):
        """The retry cron now picks up settlement_error, not just settlement_failed,
        so a known-not-charged / unknown order actually retries (the core bug)."""
        order = self._ship_order(
            grove_checkout_status="settlement_error",
            grove_settlement_attempts=1,
            grove_settlement_reconcile=False,
        )
        self.env["ir.config_parameter"].sudo().set_param("grove_headless.settlement_max_retries", "3")
        charges = []

        def ok(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_retry", "status": "succeeded"}

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=ok),
            self._env(),
        ):
            self.env["sale.order"]._cron_retry_settlements()

        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(len(charges), 1)

    def test_manual_retry_racing_cron_charges_once(self):
        """Double-fire: a manual Retry settlement and the cron both act on the same
        order. The first settles it; the second sees status==settled under the row
        lock and no-ops — exactly one charge."""
        order = self._ship_order(grove_checkout_status="settlement_error", grove_settlement_attempts=1)
        self.env["ir.config_parameter"].sudo().set_param("grove_headless.settlement_max_retries", "3")
        charges = []

        def ok(secret_key, **kwargs):
            charges.append(kwargs)
            return {"id": "pi_once", "status": "succeeded"}

        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", side_effect=ok),
            self._env(),
        ):
            # First: the operator's manual Retry settlement.
            order.action_grove_retry_settlement()
            # Then: the cron fires on the (now settled) order.
            self.env["sale.order"]._cron_retry_settlements()

        self.assertEqual(order.grove_checkout_status, "settled")
        self.assertEqual(len(charges), 1, "the balance is captured exactly once")

    def test_manual_retry_posts_result_to_chatter(self):
        """The Retry settlement server action records its outcome on the order."""
        order = self._ship_order(grove_checkout_status="settlement_error", grove_settlement_attempts=1)
        with (
            self._no_stripe_tax(),
            mock.patch.object(stripe_gateway, "create_payment_intent", return_value={"id": "pi_m"}),
            self._env(),
        ):
            order.action_grove_retry_settlement()
        self.assertEqual(order.grove_checkout_status, "settled")
        note = order.message_ids.filtered(lambda m: "manual retry settlement" in (m.body or "").lower())
        self.assertTrue(note, "the manual retry posts its outcome to the chatter")
