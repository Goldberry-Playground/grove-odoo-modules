"""Odoo-runtime tests for the Printify paid-order push, cron backoff, retry and
the oversell skip (spec §6, GOL-3314)."""

import os
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture

ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42", "PRINTIFY_DRY_RUN": ""}


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyPush(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.write({"printify_state": "queued"})

    def _patches(self, create="po_1", existing=None, send=None, env=None):
        return (
            mock.patch.dict(os.environ, env or ENV, clear=False),
            mock.patch.object(printify_client, "find_order_by_external_id", return_value=existing),
            mock.patch.object(printify_client, "create_order", return_value=create)
            if not isinstance(create, Exception)
            else mock.patch.object(printify_client, "create_order", side_effect=create),
            mock.patch.object(printify_client, "send_to_production", side_effect=send),
            mock.patch.object(grove_main, "_notify_discord"),
        )

    def _run(self, **kw):
        p_env, p_find, p_create, p_send, p_discord = self._patches(**kw)
        with p_env, p_find, p_create as create, p_send as send, p_discord as discord:
            self.order._grove_printify_push()
        return create, send, discord

    def test_push_creates_and_sends_to_production(self):
        create, send, _discord = self._run()
        payload = create.call_args.args[2]
        self.assertEqual(payload["external_id"], self.order.name)
        self.assertEqual(payload["shipping_method"], 1)
        self.assertFalse(payload["send_shipping_notification"])
        self.assertEqual(payload["line_items"], [{"product_id": "6650abc", "variant_id": 11001, "quantity": 1}])
        send.assert_called_once()
        self.assertEqual(self.order.printify_order_id, "po_1")
        self.assertEqual(self.order.printify_state, "in_production")

    def test_retry_reuses_existing_printify_order(self):
        create, send, _discord = self._run(existing={"id": "po_9", "status": "pending"})
        create.assert_not_called()
        send.assert_called_once()
        self.assertEqual(self.order.printify_order_id, "po_9")

    def test_existing_order_already_in_production_not_resent(self):
        _create, send, _discord = self._run(existing={"id": "po_9", "status": "in-production"})
        send.assert_not_called()
        self.assertEqual(self.order.printify_state, "in_production")

    def test_dry_run_skips_send_to_production(self):
        _create, send, _discord = self._run(env=dict(ENV, PRINTIFY_DRY_RUN="1"))
        send.assert_not_called()
        self.assertEqual(self.order.printify_state, "created")

    def test_failure_backs_off_then_alerts_after_three(self):
        for attempt in range(1, 4):
            _c, _s, discord = self._run(create=printify_client.PrintifyError("boom", 500))
            self.assertEqual(self.order.printify_push_attempts, attempt)
        self.assertEqual(self.order.printify_state, "push_failed")
        discord.assert_called_once()

    def test_send_failure_resets_to_queued_for_retry(self):
        # create SUCCEEDS (po_1 persisted) but send_to_production raises: the order
        # must reset to "queued" so the cron retries it, NOT stay stuck at
        # "created" which the cron never picks up (GOL-3314 review 2026-10-14).
        _c, _s, _discord = self._run(send=printify_client.PrintifyError("send boom", 500))
        self.assertEqual(self.order.printify_order_id, "po_1")
        self.assertEqual(self.order.printify_state, "queued")
        self.assertEqual(self.order.printify_push_attempts, 1)

    def test_send_failure_parks_and_alerts_after_three(self):
        for attempt in range(1, 4):
            _c, _s, discord = self._run(send=printify_client.PrintifyError("send boom", 500))
            self.assertEqual(self.order.printify_push_attempts, attempt)
        self.assertEqual(self.order.printify_state, "push_failed")
        discord.assert_called_once()

    def test_cron_counts_unexpected_crash_and_parks(self):
        # A non-PrintifyError bug in the push path must not retry every 5 minutes
        # forever: the cron counts the crash and parks/alerts at the cap.
        for attempt in range(1, 4):
            self.order.write({"printify_state": "queued", "printify_push_last_attempt": False})
            p_env, p_find, _p_create, p_send, p_discord = self._patches()
            crash = mock.patch.object(printify_client, "create_order", side_effect=RuntimeError("bug"))
            with p_env, p_find, crash, p_send, p_discord as discord:
                self.env["sale.order"]._cron_printify_push()
            self.assertEqual(self.order.printify_push_attempts, attempt)
        self.assertEqual(self.order.printify_state, "push_failed")
        discord.assert_called_once()

    def test_cron_respects_backoff(self):
        self.order.write({"printify_push_attempts": 1, "printify_push_last_attempt": fields.Datetime.now()})
        p_env, p_find, p_create, p_send, p_discord = self._patches()
        with p_env, p_find, p_create as create, p_send, p_discord:
            self.env["sale.order"]._cron_printify_push()
        create.assert_not_called()
        self.order.write({"printify_push_last_attempt": fields.Datetime.now() - timedelta(minutes=6)})
        with p_env, p_find, p_create as create, p_send, p_discord:
            self.env["sale.order"]._cron_printify_push()
        create.assert_called_once()

    def test_retry_action_requeues(self):
        self.order.write({"printify_state": "push_failed", "printify_push_attempts": 3})
        self.order.action_grove_retry_printify_push()
        self.assertEqual((self.order.printify_state, self.order.printify_push_attempts), ("queued", 0))

    def test_retry_action_refuses_non_push_failed(self):
        # Only a push_failed Printify order may be re-queued — never one that
        # already printed/shipped (GOL-3314 review 2026-10-14).
        self.order.write({"printify_state": "in_production"})
        with self.assertRaises(UserError):
            self.order.action_grove_retry_printify_push()

    def test_paid_printify_order_not_oversold(self):
        self.assertEqual(grove_main._oversold_lines(self.order), [])

    def test_session_completed_queues_push(self):
        self.order.write({"printify_state": False, "grove_stripe_session_id": "cs_shirt"})
        with (
            mock.patch.object(grove_main, "_notify_discord"),
            mock.patch.object(grove_main, "_notify_new_order"),
            mock.patch.object(grove_main, "_notify_merchant_email"),
            mock.patch.object(grove_main, "_send_order_confirmation_email"),
        ):
            grove_main._handle_session_completed(self.env, {"id": "cs_shirt", "payment_intent": "pi_shirt"})
        self.assertEqual(self.order.printify_state, "queued")
