"""TDD tests for stripe_gateway (pure Python, no Odoo DB required).

Module loaded by file path so its lack of Odoo imports is honoured and the
`requests` calls stay fully mocked — same pattern as test_shippo_client.py.
"""

import hashlib
import hmac
import importlib.util
import os
import time
import unittest
from unittest import mock

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "stripe_gateway.py")
_spec = importlib.util.spec_from_file_location("grove_stripe_gateway", _MODULE_PATH)
sg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sg)


def _ok(status, body):
    return mock.Mock(status_code=status, json=lambda: body)


class TestAmounts(unittest.TestCase):
    def test_to_cents_rounds_half_up(self):
        self.assertEqual(sg.to_cents(19.99), 1999)
        self.assertEqual(sg.to_cents(10), 1000)
        # 19.999 * 100 == 1999.8999… — must round to 2000, not truncate to 1999
        self.assertEqual(sg.to_cents(19.999), 2000)

    def test_line_charge_in_stock_full_price(self):
        # qty_available covers the quantity -> full price, full qty, not preorder
        amount, qty, is_pre = sg.line_charge(unit_price=25.0, quantity=2, qty_available=5)
        self.assertEqual((amount, qty, is_pre), (2500, 2, False))

    def test_line_charge_short_stock_is_deposit(self):
        amount, qty, is_pre = sg.line_charge(unit_price=25.0, quantity=2, qty_available=1)
        self.assertEqual((amount, qty, is_pre), (sg.to_cents(sg.PREORDER_DEPOSIT), 1, True))

    def test_line_charge_unknown_stock_is_preorder(self):
        # qty_available None (no stock module / unknown) is treated as preorder,
        # never as an in-stock full charge we can't back.
        _, _, is_pre = sg.line_charge(unit_price=25.0, quantity=1, qty_available=None)
        self.assertTrue(is_pre)


class TestSessionParams(unittest.TestCase):
    LINES = [
        {"name": "Pawpaw", "amount_cents": 2500, "quantity": 2},
        {"name": "Sales tax (WV)", "amount_cents": 300, "quantity": 1},
    ]

    def test_flatten_line_items_bracket_encoding(self):
        params = sg.build_session_params(line_items=self.LINES, success_url="https://s/ok", cancel_url="https://s/no")
        self.assertEqual(params["line_items[0][price_data][currency]"], "usd")
        self.assertEqual(params["line_items[0][price_data][unit_amount]"], 2500)
        self.assertEqual(params["line_items[0][price_data][product_data][name]"], "Pawpaw")
        self.assertEqual(params["line_items[0][quantity]"], 2)
        self.assertEqual(params["line_items[1][price_data][unit_amount]"], 300)
        self.assertEqual(params["mode"], "payment")

    def test_setup_future_usage_only_when_preorder(self):
        without = sg.build_session_params(line_items=self.LINES, success_url="a", cancel_url="b")
        self.assertNotIn("payment_intent_data[setup_future_usage]", without)
        with_pre = sg.build_session_params(
            line_items=self.LINES, success_url="a", cancel_url="b", setup_future_usage=True
        )
        self.assertEqual(with_pre["payment_intent_data[setup_future_usage]"], "off_session")

    def test_metadata_and_email_flattened(self):
        params = sg.build_session_params(
            line_items=self.LINES,
            success_url="a",
            cancel_url="b",
            metadata={"order_id": 42, "access_token": "tok"},
            customer_email="j@x.com",
        )
        self.assertEqual(params["metadata[order_id]"], 42)
        self.assertEqual(params["metadata[access_token]"], "tok")
        self.assertEqual(params["customer_email"], "j@x.com")


class TestCreateSession(unittest.TestCase):
    LINES = [{"name": "Pawpaw", "amount_cents": 2500, "quantity": 1}]

    def test_happy_path_returns_session(self):
        post = mock.Mock(return_value=_ok(200, {"id": "cs_1", "url": "https://pay/x", "payment_intent": "pi_1"}))
        out = sg.create_checkout_session("sk_test", line_items=self.LINES, success_url="a", cancel_url="b", post=post)
        self.assertEqual(out["id"], "cs_1")
        # secret key rides as HTTP basic-auth username
        self.assertEqual(post.call_args.kwargs["auth"], ("sk_test", ""))

    def test_missing_key_raises_before_network(self):
        post = mock.Mock()
        with self.assertRaises(sg.StripeError):
            sg.create_checkout_session("", line_items=self.LINES, success_url="a", cancel_url="b", post=post)
        post.assert_not_called()

    def test_empty_line_items_raises(self):
        with self.assertRaises(sg.StripeError):
            sg.create_checkout_session("sk", line_items=[], success_url="a", cancel_url="b", post=mock.Mock())

    def test_stripe_error_response_raises_with_message(self):
        post = mock.Mock(return_value=_ok(400, {"error": {"message": "Amount too small"}}))
        with self.assertRaises(sg.StripeError) as ctx:
            sg.create_checkout_session("sk", line_items=self.LINES, success_url="a", cancel_url="b", post=post)
        self.assertIn("Amount too small", str(ctx.exception))


class TestRefund(unittest.TestCase):
    def test_refund_posts_payment_intent(self):
        post = mock.Mock(return_value=_ok(200, {"id": "re_1", "status": "succeeded"}))
        out = sg.create_refund("sk", "pi_1", reason="requested_by_customer", post=post)
        self.assertEqual(out["id"], "re_1")
        self.assertEqual(post.call_args.kwargs["data"]["payment_intent"], "pi_1")
        self.assertEqual(post.call_args.kwargs["data"]["reason"], "requested_by_customer")

    def test_refund_requires_payment_intent(self):
        with self.assertRaises(sg.StripeError):
            sg.create_refund("sk", "", post=mock.Mock())


class TestWebhookSignature(unittest.TestCase):
    SECRET = "whsec_test"

    def _sign(self, payload, secret=None, ts=None):
        secret = secret or self.SECRET
        ts = ts if ts is not None else int(time.time())
        signed = f"{ts}.".encode() + payload
        v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={v1}"

    def test_valid_signature_passes(self):
        body = b'{"id":"evt_1","type":"checkout.session.completed"}'
        self.assertTrue(sg.verify_webhook_signature(body, self._sign(body), self.SECRET))

    def test_str_payload_accepted(self):
        body = '{"id":"evt_1"}'
        header = self._sign(body.encode())
        self.assertTrue(sg.verify_webhook_signature(body, header, self.SECRET))

    def test_tampered_body_fails(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body)
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(b'{"id":"evt_HACKED"}', header, self.SECRET)

    def test_wrong_secret_fails(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body, secret="whsec_other")
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(body, header, self.SECRET)

    def test_stale_timestamp_fails(self):
        body = b'{"id":"evt_1"}'
        old = int(time.time()) - 10_000
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(body, self._sign(body, ts=old), self.SECRET)

    def test_missing_secret_or_header_fails(self):
        body = b"{}"
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(body, self._sign(body), "")
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(body, "", self.SECRET)

    def test_malformed_header_fails(self):
        with self.assertRaises(sg.StripeError):
            sg.verify_webhook_signature(b"{}", "garbage-no-equals", self.SECRET)


if __name__ == "__main__":
    unittest.main()
