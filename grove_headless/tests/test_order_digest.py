"""Tests for order_digest.py (GOL-1978).

Pure stdlib — loaded by file path so these run both under Odoo's test runner
and standalone (``python3 -m pytest grove_headless/tests/test_order_digest.py``
or ``python3 -m unittest grove_headless.tests.test_order_digest``).
"""

import importlib.util
import os
import unittest
from datetime import date

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "order_digest.py")
_spec = importlib.util.spec_from_file_location("grove_order_digest", _MODULE_PATH)
od = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(od)

TODAY = date(2026, 9, 1)  # Monday
PERIOD_START = date(2026, 8, 25)


def _order(
    name="S00001",
    date_order=TODAY,
    amount_total=100.00,
    checkout_status="paid",
    delivery_status=None,
    preorder_ids="",
    partner_name="Alice",
    zip_code="26651",
    is_pickup=False,
    units=1,
):
    return {
        "name": name,
        "date_order": date_order,
        "amount_total": amount_total,
        "grove_checkout_status": checkout_status,
        "grove_delivery_status": delivery_status,
        "grove_preorder_variant_ids": preorder_ids,
        "partner_name": partner_name,
        "partner_shipping_zip": zip_code,
        "is_pickup": is_pickup,
        "units": units,
    }


class TestBuildDigestEmpty(unittest.TestCase):
    def test_empty_orders(self):
        d = od.build_digest([], today=TODAY)
        self.assertEqual(d["orders_placed"], 0)
        self.assertEqual(d["revenue_total"], 0.0)
        self.assertEqual(d["units_shipped"], 0)
        self.assertEqual(d["preorder_count"], 0)
        self.assertEqual(d["pickup_count"], 0)


class TestBuildDigestPeriodWindow(unittest.TestCase):
    def test_order_in_window_counted(self):
        orders = [_order(date_order=TODAY, amount_total=200.00)]
        d = od.build_digest(orders, today=TODAY, period_days=7)
        self.assertEqual(d["orders_placed"], 1)
        self.assertAlmostEqual(d["revenue_total"], 200.00)

    def test_order_outside_window_not_in_period_counts(self):
        old = _order(date_order=date(2026, 1, 1), amount_total=500.00)
        recent = _order(name="S00002", date_order=TODAY, amount_total=50.00)
        d = od.build_digest([old, recent], today=TODAY, period_days=7)
        self.assertEqual(d["orders_placed"], 1)
        self.assertAlmostEqual(d["revenue_total"], 50.00)

    def test_old_preorder_still_appears_as_outstanding(self):
        old_preorder = _order(
            name="S00099",
            date_order=date(2026, 3, 1),
            checkout_status="deposit_paid",
            preorder_ids="42",
        )
        d = od.build_digest([old_preorder], today=TODAY, period_days=7)
        self.assertEqual(d["orders_placed"], 0)  # outside window
        self.assertEqual(d["preorder_count"], 1)  # still outstanding


class TestShippedFiltering(unittest.TestCase):
    def test_transit_counts_as_shipped(self):
        orders = [
            _order(name="S00001", delivery_status="transit"),
            _order(name="S00002", delivery_status=None),
        ]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_shipped"], 1)

    def test_delivered_counts_as_shipped(self):
        orders = [_order(delivery_status="delivered")]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_shipped"], 1)

    def test_label_purchased_counts_as_shipped(self):
        orders = [_order(delivery_status="label_purchased")]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_shipped"], 1)

    def test_no_delivery_status_not_shipped(self):
        orders = [_order(delivery_status=None)]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_shipped"], 0)

    def test_units_shipped_sums_line_units_not_orders(self):
        # Two shipped orders carrying 3 and 2 tree units -> 5 units, not 2 orders.
        orders = [
            _order(name="S1", delivery_status="transit", units=3),
            _order(name="S2", delivery_status="delivered", units=2),
            _order(name="S3", delivery_status=None, units=9),  # not shipped -> excluded
        ]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_shipped"], 5)

    def test_units_shipped_falls_back_to_one_when_units_absent(self):
        # A record without a units field counts as 1 (order-level fallback).
        order = _order(delivery_status="transit")
        del order["units"]
        d = od.build_digest([order], today=TODAY)
        self.assertEqual(d["units_shipped"], 1)


class TestPreorderSection(unittest.TestCase):
    def _preorder(self, name="S00010", zip_code="26651", delivery_status=None):
        return _order(
            name=name,
            date_order=date(2026, 6, 1),
            checkout_status="deposit_paid",
            preorder_ids="42",
            zip_code=zip_code,
            delivery_status=delivery_status,
        )

    def test_deposit_paid_no_delivery_is_outstanding(self):
        d = od.build_digest([self._preorder()], today=TODAY)
        self.assertEqual(d["preorder_count"], 1)

    def test_shipped_preorder_not_outstanding(self):
        d = od.build_digest([self._preorder(delivery_status="transit")], today=TODAY)
        self.assertEqual(d["preorder_count"], 0)

    def test_wave_fn_called_with_zone(self):
        calls = []

        def fake_zone(zip_code):
            return 6

        def fake_wave(zone, today):
            calls.append((zone, today))
            return {
                "season": "spring",
                "ship_start": date(2027, 3, 15),
                "ship_end": date(2027, 3, 31),
            }

        d = od.build_digest(
            [self._preorder()],
            today=TODAY,
            next_wave_fn=fake_wave,
            usda_zone_fn=fake_zone,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 6)
        entry = d["outstanding_preorders"][0]
        self.assertEqual(entry["ship_season"], "spring")
        self.assertIsNotNone(entry["ship_start"])

    def test_no_zone_fn_gives_tbd_window(self):
        d = od.build_digest([self._preorder()], today=TODAY)
        entry = d["outstanding_preorders"][0]
        self.assertEqual(entry["ship_start"], None)
        self.assertEqual(entry["ship_season"], "")

    def test_ships_soon_flag_set_within_30d(self):
        def fake_zone(z):
            return 6

        def fake_wave(zone, today):
            return {"season": "fall", "ship_start": today, "ship_end": today}

        d = od.build_digest([self._preorder()], today=TODAY, next_wave_fn=fake_wave, usda_zone_fn=fake_zone)
        self.assertTrue(d["outstanding_preorders"][0]["ships_soon"])

    def test_ships_soon_false_far_future(self):
        def fake_zone(z):
            return 6

        def fake_wave(zone, today):
            return {
                "season": "spring",
                "ship_start": date(2027, 6, 1),
                "ship_end": date(2027, 6, 15),
            }

        d = od.build_digest([self._preorder()], today=TODAY, next_wave_fn=fake_wave, usda_zone_fn=fake_zone)
        self.assertFalse(d["outstanding_preorders"][0]["ships_soon"])


class TestPickupSection(unittest.TestCase):
    def test_pickup_awaiting_counted(self):
        orders = [_order(is_pickup=True, checkout_status="paid", delivery_status=None)]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["pickup_count"], 1)

    def test_collected_pickup_not_awaiting(self):
        orders = [_order(is_pickup=True, delivery_status="collected")]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["pickup_count"], 0)

    def test_shipped_pickup_not_awaiting(self):
        orders = [_order(is_pickup=True, delivery_status="transit")]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["pickup_count"], 0)


class TestRenderers(unittest.TestCase):
    def _digest(self):
        return od.build_digest(
            [
                _order(name="S00001", delivery_status="transit"),
                _order(
                    name="S00002",
                    checkout_status="deposit_paid",
                    preorder_ids="7",
                    date_order=date(2026, 5, 1),
                ),
                _order(name="S00003", is_pickup=True, checkout_status="paid"),
            ],
            today=TODAY,
        )

    def test_text_render_contains_key_sections(self):
        text = od.render_digest_text(self._digest())
        self.assertIn("Weekly order rollup", text)
        self.assertIn("Orders placed", text)
        self.assertIn("Revenue", text)
        self.assertIn("Units shipped", text)
        self.assertIn("preorder", text.lower())
        self.assertIn("pickup", text.lower())

    def test_html_render_contains_key_sections(self):
        html = od.render_digest_html(self._digest())
        self.assertIn("<h2>", html)
        self.assertIn("Outstanding preorders", html)
        self.assertIn("Pickups awaiting collection", html)

    def test_text_no_em_dashes(self):
        text = od.render_digest_text(self._digest())
        self.assertNotIn("—", text)

    def test_html_no_em_dashes(self):
        html = od.render_digest_html(self._digest())
        self.assertNotIn("—", html)

    def test_fmt_currency(self):
        self.assertEqual(od._fmt_currency(1234.5), "$1,234.50")
        self.assertEqual(od._fmt_currency(0), "$0.00")

    def test_wave_label_tbd_when_no_dates(self):
        entry = {"ship_start": None, "ship_end": None, "ship_season": ""}
        self.assertEqual(od._wave_label(entry), "ship window TBD")

    def test_wave_label_with_season(self):
        entry = {
            "ship_start": date(2027, 3, 15),
            "ship_end": date(2027, 3, 31),
            "ship_season": "spring",
        }
        label = od._wave_label(entry)
        self.assertIn("spring", label)
        self.assertIn("Mar 15, 2027", label)
