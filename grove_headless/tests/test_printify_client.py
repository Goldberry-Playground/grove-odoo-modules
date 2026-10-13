"""Pure tests for the Printify client (no Odoo). Spec: 2026-10-13-printify-merch-design.md."""

import hashlib
import hmac
import importlib.util
import os
import unittest
from unittest import mock

import requests

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "printify_client.py")
_spec = importlib.util.spec_from_file_location("grove_printify_client", _MODULE_PATH)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _resp(status, body):
    return mock.Mock(status_code=status, json=lambda: body, text=str(body))


class TestPrintifyClient(unittest.TestCase):
    def test_list_products_follows_pages(self):
        get = mock.Mock(
            side_effect=[
                _resp(200, {"current_page": 1, "last_page": 2, "data": [{"id": "a"}]}),
                _resp(200, {"current_page": 2, "last_page": 2, "data": [{"id": "b"}]}),
            ]
        )
        self.assertEqual([p["id"] for p in pc.list_products("tok", "42", get=get)], ["a", "b"])
        url = get.call_args_list[0].args[0]
        self.assertEqual(url, "https://api.printify.com/v1/shops/42/products.json")
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer tok")

    def test_calculate_shipping_returns_standard_in_dollars(self):
        post = mock.Mock(return_value=_resp(200, {"standard": 475, "express": 1200}))
        amount = pc.calculate_shipping(
            "tok", "42", [{"product_id": "p", "variant_id": 1, "quantity": 1}], {"zip": "25526"}, post=post
        )
        self.assertEqual(amount, 4.75)
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.assertEqual(post.call_args.args[0], "https://api.printify.com/v1/shops/42/orders/shipping.json")

    def test_calculate_shipping_timeout_raises_printify_error(self):
        post = mock.Mock(side_effect=requests.Timeout("slow"))
        with self.assertRaises(pc.PrintifyError):
            pc.calculate_shipping("tok", "42", [], {}, post=post)

    def test_http_error_raises_with_status(self):
        post = mock.Mock(return_value=_resp(503, {"error": "down"}))
        with self.assertRaises(pc.PrintifyError) as ctx:
            pc.calculate_shipping("tok", "42", [], {}, post=post)
        self.assertEqual(ctx.exception.status, 503)

    def test_missing_standard_rate_raises(self):
        post = mock.Mock(return_value=_resp(200, {"express": 900}))
        with self.assertRaises(pc.PrintifyError):
            pc.calculate_shipping("tok", "42", [], {}, post=post)

    def test_create_order_returns_id(self):
        post = mock.Mock(return_value=_resp(200, {"id": "po_1"}))
        self.assertEqual(pc.create_order("tok", "42", {"external_id": "S1"}, post=post), "po_1")

    def test_find_order_by_external_id(self):
        get = mock.Mock(
            side_effect=[
                _resp(200, {"last_page": 2, "data": [{"id": "x", "external_id": "S9"}]}),
                _resp(200, {"last_page": 2, "data": [{"id": "po_1", "external_id": "S1"}]}),
            ]
        )
        self.assertEqual(pc.find_order_by_external_id("tok", "42", "S1", get=get)["id"], "po_1")

    def test_find_order_by_external_id_none(self):
        get = mock.Mock(return_value=_resp(200, {"last_page": 1, "data": []}))
        self.assertIsNone(pc.find_order_by_external_id("tok", "42", "S1", get=get))

    def test_send_to_production_posts(self):
        post = mock.Mock(return_value=_resp(200, {"id": "po_1"}))
        pc.send_to_production("tok", "42", "po_1", post=post)
        self.assertEqual(
            post.call_args.args[0], "https://api.printify.com/v1/shops/42/orders/po_1/send_to_production.json"
        )

    def test_verify_signature(self):
        body = b'{"type":"order:updated"}'
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(pc.verify_signature("s3cret", body, f"sha256={digest}"))
        self.assertTrue(pc.verify_signature("s3cret", body, digest))
        self.assertFalse(pc.verify_signature("s3cret", body, "sha256=bad"))
        self.assertFalse(pc.verify_signature("", body, f"sha256={digest}"))
        self.assertFalse(pc.verify_signature("s3cret", body, ""))

    def test_destinations_are_50_states_and_dc(self):
        self.assertEqual(len(pc.US_50_AND_DC), 51)
        self.assertIn("DC", pc.US_50_AND_DC)
        for territory in ("PR", "GU", "VI", "AS", "MP"):
            self.assertNotIn(territory, pc.US_50_AND_DC)
