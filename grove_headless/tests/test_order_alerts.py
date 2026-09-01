"""Pure tests for the post-purchase alert formatters (GOL-1933).

``order_alerts`` has no imports at all, so it loads by file path like the other
pure tests and runs under plain pytest (the webhook wiring that calls it is a
TransactionCase, covered separately).
"""

import importlib.util
import os
import sys
import unittest

_MODELS = os.path.join(os.path.dirname(__file__), "..", "models")


def _load(modname):
    spec = importlib.util.spec_from_file_location(
        f"grove_headless.models.{modname}", os.path.join(_MODELS, f"{modname}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


oa = _load("order_alerts")


class MoneyTests(unittest.TestCase):
    def test_thousands_and_two_decimals(self):
        self.assertEqual(oa.format_money(1234.5), "$1,234.50 USD")
        self.assertEqual(oa.format_money(24.92), "$24.92 USD")

    def test_bad_amount_degrades_gracefully(self):
        self.assertEqual(oa.format_money(None), "None USD")


class FulfillmentLabelTests(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(oa.fulfillment_label("ship"), "Shipping")
        self.assertEqual(oa.fulfillment_label("pickup"), "Farm pickup")

    def test_unknown_defaults_to_shipping(self):
        self.assertEqual(oa.fulfillment_label(None), "Shipping")
        self.assertEqual(oa.fulfillment_label("bogus"), "Shipping")


class DiscordTests(unittest.TestCase):
    def _order(self, **over):
        base = dict(
            order_ref="NURS-0042",
            customer="Jane Doe",
            customer_email="jane@example.com",
            fulfillment="ship",
            lines=[("American Plum (Bareroot)", 2), ("Fig 'Chicago Hardy'", 1.0)],
            total=124.92,
        )
        base.update(over)
        return oa.format_new_order_discord(**base)

    def test_ship_order_shape(self):
        msg = self._order()
        self.assertIn("New order NURS-0042 — Shipping", msg)
        self.assertIn("Customer: Jane Doe <jane@example.com>", msg)
        self.assertIn("• 2× American Plum (Bareroot)", msg)
        # 1.0 renders as an int, not "1.0×"
        self.assertIn("• 1× Fig 'Chicago Hardy'", msg)
        self.assertIn("Total: $124.92 USD", msg)

    def test_pickup_still_notifies_and_labels(self):
        msg = self._order(fulfillment="pickup")
        self.assertIn("Farm pickup", msg)

    def test_deposit_wording(self):
        msg = self._order(is_deposit=True)
        self.assertIn("New preorder (deposit) NURS-0042", msg)

    def test_carrier_tracking_omitted_when_absent(self):
        msg = self._order()
        self.assertNotIn("Carrier", msg)
        self.assertNotIn("Tracking", msg)

    def test_carrier_tracking_included_when_present(self):
        msg = self._order(carrier="UPS Ground", tracking="1Z999")
        self.assertIn("Carrier: UPS Ground", msg)
        self.assertIn("Tracking: 1Z999", msg)


class MerchantEmailTests(unittest.TestCase):
    def _mail(self, **over):
        base = dict(
            order_ref="NURS-0042",
            customer="Jane Doe",
            customer_email="jane@example.com",
            fulfillment="ship",
            lines=[("American Plum (Bareroot)", 2)],
            total=124.92,
            shipping_address="Jane Doe<br/>123 Main St<br/>Athens OH 45701",
        )
        base.update(over)
        return oa.format_merchant_email(**base)

    def test_subject_and_body(self):
        subject, body = self._mail()
        self.assertEqual(subject, "New order NURS-0042 — Shipping")
        self.assertIn("<li>2× American Plum (Bareroot)</li>", body)
        self.assertIn("Total:</strong> $124.92 USD", body)
        self.assertIn("Ship to:", body)
        self.assertIn("123 Main St", body)

    def test_pickup_has_no_ship_to_block(self):
        subject, body = self._mail(fulfillment="pickup", shipping_address=None)
        self.assertEqual(subject, "New order NURS-0042 — Farm pickup")
        self.assertNotIn("Ship to:", body)

    def test_deposit_subject(self):
        subject, _ = self._mail(is_deposit=True)
        self.assertEqual(subject, "New preorder NURS-0042 — Shipping")


if __name__ == "__main__":
    unittest.main()
