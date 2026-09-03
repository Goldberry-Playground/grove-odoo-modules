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
        # free stock covers the quantity -> one full-price sub-charge, no preorder
        self.assertEqual(
            sg.line_charge(unit_price=25.0, quantity=2, free_available=5),
            [(2500, 2, False)],
        )

    def test_line_charge_short_stock_splits_per_unit(self):
        # GOL-1036 defect 3: want 5, only 2 free -> 2 billed full price PLUS
        # 3 units of deposit (per-unit, NOT a single flat qty-1 deposit).
        dep = sg.to_cents(sg.PREORDER_DEPOSIT)
        self.assertEqual(
            sg.line_charge(unit_price=25.0, quantity=5, free_available=2),
            [(2500, 2, False), (dep, 3, True)],
        )

    def test_line_charge_zero_stock_is_all_deposit_per_unit(self):
        # No free stock -> every unit is a preorder deposit (qty preserved, not
        # collapsed to 1).
        dep = sg.to_cents(sg.PREORDER_DEPOSIT)
        self.assertEqual(
            sg.line_charge(unit_price=25.0, quantity=4, free_available=0),
            [(dep, 4, True)],
        )

    def test_line_charge_unknown_stock_is_preorder(self):
        # free_available None (no stock module / unknown) is treated as zero free
        # stock -> preorder, never an in-stock full charge we can't back.
        charges = sg.line_charge(unit_price=25.0, quantity=3, free_available=None)
        self.assertEqual(charges, [(sg.to_cents(sg.PREORDER_DEPOSIT), 3, True)])

    def test_line_charge_negative_free_is_zero(self):
        # A reserved-oversold free_qty (< 0) must not flip units back to in-stock.
        charges = sg.line_charge(unit_price=25.0, quantity=2, free_available=-3)
        self.assertEqual(charges, [(sg.to_cents(sg.PREORDER_DEPOSIT), 2, True)])

    def test_line_charge_out_of_window_is_all_deposit_despite_stock(self):
        # GOL-1666 §1: a bareroot line inside a dormant preorder window can't
        # ship now, so every unit reserves with the flat deposit EVEN with free
        # stock on hand — matches the product page's deposit-now promise instead
        # of charging 100% at checkout.
        dep = sg.to_cents(sg.PREORDER_DEPOSIT)
        self.assertEqual(
            sg.line_charge(unit_price=25.0, quantity=3, free_available=10, ships_now=False),
            [(dep, 3, True)],
        )

    def test_line_charge_in_window_default_is_full_price(self):
        # ships_now defaults True: the existing in-stock/in-window path is
        # unchanged (in-stock and in-window still charges in full).
        self.assertEqual(
            sg.line_charge(unit_price=25.0, quantity=2, free_available=5, ships_now=True),
            [(2500, 2, False)],
        )


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


class TestPaymentIntent(unittest.TestCase):
    """GOL-2052: off-session ship-time settlement primitive."""

    def _charge(self, post, **over):
        kwargs = dict(
            amount_cents=3120,
            customer="cus_1",
            payment_method="pm_1",
            post=post,
        )
        kwargs.update(over)
        return sg.create_payment_intent("sk_test", **kwargs)

    def test_happy_path_confirms_off_session(self):
        post = mock.Mock(return_value=_ok(200, {"id": "pi_9", "status": "succeeded"}))
        out = self._charge(post, idempotency_key="order-42-settle")
        self.assertEqual(out["id"], "pi_9")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["amount"], 3120)
        self.assertEqual(data["currency"], "usd")
        self.assertEqual(data["customer"], "cus_1")
        self.assertEqual(data["payment_method"], "pm_1")
        # off_session + confirm are what make the saved card settle without the
        # shopper present.
        self.assertEqual(data["off_session"], "true")
        self.assertEqual(data["confirm"], "true")
        self.assertEqual(post.call_args.kwargs["auth"], ("sk_test", ""))
        # Idempotency-Key rides as a header so a retried settlement never
        # double-charges.
        self.assertEqual(post.call_args.kwargs["headers"], {"Idempotency-Key": "order-42-settle"})

    def test_missing_key_raises_before_network(self):
        post = mock.Mock()
        with self.assertRaises(sg.StripeError):
            sg.create_payment_intent("", amount_cents=100, customer="c", payment_method="pm", post=post)
        post.assert_not_called()

    def test_missing_customer_or_pm_raises_before_network(self):
        post = mock.Mock()
        with self.assertRaises(sg.StripeError):
            sg.create_payment_intent("sk", amount_cents=100, customer="", payment_method="pm", post=post)
        with self.assertRaises(sg.StripeError):
            sg.create_payment_intent("sk", amount_cents=100, customer="c", payment_method="", post=post)
        post.assert_not_called()

    def test_nonpositive_amount_raises_before_network(self):
        post = mock.Mock()
        with self.assertRaises(sg.StripeError):
            self._charge(post, amount_cents=0)
        post.assert_not_called()

    def test_card_decline_raises_card_error_with_detail(self):
        # Stripe's off-session decline shape: HTTP 402, error.type card_error,
        # with the failed payment_intent echoed back for the retry path.
        body = {
            "error": {
                "type": "card_error",
                "code": "card_declined",
                "decline_code": "insufficient_funds",
                "message": "Your card has insufficient funds.",
                "payment_intent": {"id": "pi_dead"},
            }
        }
        post = mock.Mock(return_value=_ok(402, body))
        with self.assertRaises(sg.StripeCardError) as ctx:
            self._charge(post)
        err = ctx.exception
        self.assertEqual(err.code, "card_declined")
        self.assertEqual(err.decline_code, "insufficient_funds")
        self.assertEqual(err.payment_intent, "pi_dead")
        # A card decline is still a StripeError subclass so blanket handlers catch it.
        self.assertIsInstance(err, sg.StripeError)

    def test_non_card_error_raises_plain_stripe_error(self):
        post = mock.Mock(return_value=_ok(400, {"error": {"type": "invalid_request_error", "message": "bad"}}))
        with self.assertRaises(sg.StripeError) as ctx:
            self._charge(post)
        self.assertNotIsInstance(ctx.exception, sg.StripeCardError)


class TestRetrievePaymentIntent(unittest.TestCase):
    """GOL-2053: read the deposit intent back to recover the saved card ids."""

    def test_returns_customer_and_payment_method(self):
        get = mock.Mock(return_value=_ok(200, {"id": "pi_1", "customer": "cus_9", "payment_method": "pm_9"}))
        out = sg.retrieve_payment_intent("sk_test", "pi_1", get=get)
        self.assertEqual(out["customer"], "cus_9")
        self.assertEqual(out["payment_method"], "pm_9")
        # GET by id, authed with the secret key.
        self.assertTrue(get.call_args.args[0].endswith("/v1/payment_intents/pi_1"))
        self.assertEqual(get.call_args.kwargs["auth"], ("sk_test", ""))

    def test_missing_key_or_id_raises_before_network(self):
        get = mock.Mock()
        with self.assertRaises(sg.StripeError):
            sg.retrieve_payment_intent("", "pi_1", get=get)
        with self.assertRaises(sg.StripeError):
            sg.retrieve_payment_intent("sk", "", get=get)
        get.assert_not_called()

    def test_non_2xx_raises_stripe_error(self):
        get = mock.Mock(return_value=_ok(404, {"error": {"message": "No such payment_intent"}}))
        with self.assertRaises(sg.StripeError):
            sg.retrieve_payment_intent("sk", "pi_missing", get=get)


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
