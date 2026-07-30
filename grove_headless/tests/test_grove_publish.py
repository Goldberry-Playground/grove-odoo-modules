"""Pure-Python tests for grove_publish (HMAC signing + delivery contract).

Loaded by file path so it stays free of Odoo imports and `requests` is fully
mocked — same pattern as test_stripe_gateway.py / test_shippo_client.py. These
pin the wire contract the grove-sites receiver must match (GOL-985).
"""

import hashlib
import hmac
import importlib.util
import json
import os
import unittest
from unittest import mock

import requests

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "grove_publish.py")
_spec = importlib.util.spec_from_file_location("grove_publish", _MODULE_PATH)
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)

SECRET = "s3cr3t-per-tenant-key"
PAYLOAD = {
    "event": "guide.publish",
    "delivery_id": "abc123",
    "tenant": "goldberry",
    "kind": "product",
    "product": {"id": 7, "template_id": 7, "slug": "pawpaw", "name": "Pawpaw"},
    "guide_ready": True,
}


def _resp(status=200, text="ok"):
    return mock.Mock(status_code=status, text=text)


class TestSerialize(unittest.TestCase):
    def test_deterministic_sorted_compact(self):
        # Same logical payload -> identical bytes regardless of dict order, so a
        # replay signs identically and the receiver never guesses our encoding.
        a = gp.serialize({"b": 1, "a": 2})
        b = gp.serialize({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, b'{"a":2,"b":1}')  # sorted keys, no spaces


class TestSign(unittest.TestCase):
    def test_sign_body_matches_manual_hmac(self):
        body = gp.serialize(PAYLOAD)
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(gp.sign_body(SECRET, body), expected)

    def test_sign_empty_secret_raises(self):
        with self.assertRaises(ValueError):
            gp.sign_body("", b"{}")

    def test_verify_round_trip(self):
        body = gp.serialize(PAYLOAD)
        sig = gp.sign_body(SECRET, body)
        self.assertTrue(gp.verify_signature(SECRET, body, sig))

    def test_verify_rejects_wrong_secret(self):
        body = gp.serialize(PAYLOAD)
        sig = gp.sign_body(SECRET, body)
        self.assertFalse(gp.verify_signature("other-secret", body, sig))

    def test_verify_rejects_tampered_body(self):
        body = gp.serialize(PAYLOAD)
        sig = gp.sign_body(SECRET, body)
        self.assertFalse(gp.verify_signature(SECRET, body + b" ", sig))

    def test_verify_fails_closed_on_empty(self):
        self.assertFalse(gp.verify_signature("", b"{}", "sha256=x"))
        self.assertFalse(gp.verify_signature(SECRET, b"{}", ""))


class TestDeliver(unittest.TestCase):
    def test_posts_exact_signed_bytes_and_headers(self):
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _resp(200)

        body, signature, response = gp.deliver(
            "https://site/api/webhooks/publish",
            SECRET,
            PAYLOAD,
            event_type="guide.publish",
            delivery_id="abc123",
            tenant="goldberry",
            post=fake_post,
        )

        # The bytes on the wire are exactly the bytes we signed (data=, not json=).
        self.assertEqual(captured["data"], body)
        self.assertEqual(captured["data"], gp.serialize(PAYLOAD))
        self.assertEqual(captured["url"], "https://site/api/webhooks/publish")
        self.assertEqual(captured["timeout"], gp.DEFAULT_TIMEOUT)

        h = captured["headers"]
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h[gp.EVENT_HEADER], "guide.publish")
        self.assertEqual(h[gp.DELIVERY_HEADER], "abc123")
        self.assertEqual(h[gp.TENANT_HEADER], "goldberry")
        self.assertEqual(h[gp.SIGNATURE_HEADER], signature)

        # A receiver recomputing HMAC over the raw body accepts it.
        self.assertTrue(gp.verify_signature(SECRET, captured["data"], h[gp.SIGNATURE_HEADER]))
        self.assertEqual(response.status_code, 200)

    def test_non_2xx_is_not_an_exception(self):
        _, _, response = gp.deliver(
            "https://site/hook",
            SECRET,
            PAYLOAD,
            event_type="guide.publish",
            delivery_id="d",
            tenant="goldberry",
            post=lambda *a, **k: _resp(401, "unauthorized"),
        )
        self.assertEqual(response.status_code, 401)  # caller inspects, no raise

    def test_transport_error_raises_publish_delivery_error(self):
        def boom(*a, **k):
            raise requests.ConnectionError("refused")

        with self.assertRaises(gp.PublishDeliveryError):
            gp.deliver(
                "https://site/hook",
                SECRET,
                PAYLOAD,
                event_type="guide.publish",
                delivery_id="d",
                tenant="goldberry",
                post=boom,
            )

    def test_body_is_valid_json(self):
        body = gp.serialize(PAYLOAD)
        self.assertEqual(json.loads(body)["product"]["slug"], "pawpaw")


if __name__ == "__main__":
    unittest.main()
