"""Integration tests for grove.publish.event + the Draft-guide publish action.

Runs inside Odoo's TransactionCase harness (the install-smoke-test CI job).
Proves the GOL-985 acceptance: triggering the publish action delivers a
verified-HMAC webhook and records the event in grove.publish.event.

    odoo --addons-path=... --test-enable --test-tags='/grove_headless' \
         --init=grove_headless --stop-after-init

The webhook `requests.post` is patched so no real network call is made; the
captured request is asserted to carry a signature that verifies against the
configured per-tenant secret over the exact bytes sent.
"""

import os
from unittest import mock

from odoo.addons.grove_headless.models import grove_publish
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

WEBHOOK_ENV = {
    "GROVE_PUBLISH_WEBHOOK_URL_GOLDBERRY": "https://goldberry.test/api/webhooks/publish",
    "GROVE_PUBLISH_WEBHOOK_SECRET_GOLDBERRY": "unit-test-tenant-secret",
}


class _FakePost:
    """Callable stand-in for requests.post that records the last request."""

    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return mock.Mock(status_code=self.status_code, text=self.text)


@tagged("grove_headless", "publish_event", "post_install", "-at_install")
class TestPublishEvent(TransactionCase):
    def setUp(self):
        super().setUp()
        # Default company/website is Goldberry (data/grove_companies.xml), so the
        # product resolves to the 'goldberry' tenant.
        self.product = self.env["product.template"].create({"name": "Test Pawpaw Guide", "grove_guide_ready": True})

    def _patch_env(self, **overrides):
        env = dict(WEBHOOK_ENV)
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=False)

    def _patch_post(self, fake):
        return mock.patch.object(grove_publish.requests, "post", fake)

    def test_publish_records_verified_delivery(self):
        fake = _FakePost(status_code=200)
        with self._patch_env(), self._patch_post(fake):
            self.product.action_publish_guide()

        event = self.env["grove.publish.event"].search([("product_tmpl_id", "=", self.product.id)], limit=1)
        self.assertTrue(event, "a grove.publish.event row must be recorded")
        self.assertEqual(event.state, "delivered")
        self.assertEqual(event.tenant, "goldberry")
        self.assertEqual(event.event_type, "guide.publish")
        self.assertEqual(event.http_status, 200)
        self.assertTrue(event.delivery_id)

        # Exactly one delivery, to the configured tenant URL.
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["url"], WEBHOOK_ENV["GROVE_PUBLISH_WEBHOOK_URL_GOLDBERRY"])

        # The signature we sent verifies against the tenant secret over the raw
        # bytes on the wire — the whole point of the contract.
        sig = call["headers"][grove_publish.SIGNATURE_HEADER]
        self.assertEqual(sig, event.signature)
        self.assertTrue(
            grove_publish.verify_signature(WEBHOOK_ENV["GROVE_PUBLISH_WEBHOOK_SECRET_GOLDBERRY"], call["data"], sig)
        )
        # Delivery headers carry the routing + dedupe metadata.
        self.assertEqual(call["headers"][grove_publish.EVENT_HEADER], "guide.publish")
        self.assertEqual(call["headers"][grove_publish.TENANT_HEADER], "goldberry")
        self.assertEqual(call["headers"][grove_publish.DELIVERY_HEADER], event.delivery_id)

    def test_publish_requires_approval(self):
        self.product.grove_guide_ready = False
        with self._patch_env(), self._patch_post(_FakePost()):
            with self.assertRaises(UserError):
                self.product.action_publish_guide()
        self.assertFalse(self.env["grove.publish.event"].search([("product_tmpl_id", "=", self.product.id)]))

    def test_missing_config_raises_and_records_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True), self._patch_post(_FakePost()):
            with self.assertRaises(UserError):
                self.product.action_publish_guide()
        self.assertFalse(self.env["grove.publish.event"].search([("product_tmpl_id", "=", self.product.id)]))

    def test_non_2xx_marks_failed_without_raising(self):
        fake = _FakePost(status_code=401, text="unauthorized")
        with self._patch_env(), self._patch_post(fake):
            # Non-2xx surfaces as a notification, not an exception — the event
            # row must persist for retry.
            result = self.product.action_publish_guide()
        self.assertEqual(result["params"]["type"], "warning")
        event = self.env["grove.publish.event"].search([("product_tmpl_id", "=", self.product.id)], limit=1)
        self.assertEqual(event.state, "failed")
        self.assertEqual(event.http_status, 401)

    def test_retry_reuses_delivery_id(self):
        failing = _FakePost(status_code=500, text="boom")
        with self._patch_env(), self._patch_post(failing):
            self.product.action_publish_guide()
        event = self.env["grove.publish.event"].search([("product_tmpl_id", "=", self.product.id)], limit=1)
        self.assertEqual(event.state, "failed")
        original_delivery = event.delivery_id

        succeeding = _FakePost(status_code=200)
        with self._patch_env(), self._patch_post(succeeding):
            event.action_retry()
        self.assertEqual(event.state, "delivered")
        # Same logical delivery -> same dedupe key on the receiver.
        self.assertEqual(event.delivery_id, original_delivery)
        self.assertEqual(succeeding.calls[0]["headers"][grove_publish.DELIVERY_HEADER], original_delivery)
