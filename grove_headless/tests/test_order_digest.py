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


def _picking(date_done=TODAY, units=1):
    return {"date_done": date_done, "units": units}


class TestBuildDigestEmpty(unittest.TestCase):
    def test_empty_orders(self):
        d = od.build_digest([], today=TODAY)
        self.assertEqual(d["orders_placed"], 0)
        self.assertEqual(d["revenue_total"], 0.0)
        self.assertEqual(d["units_ordered"], 0)
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


class TestUnitsMetric(unittest.TestCase):
    """Units shipped is sourced from stock.picking (date_done) within the
    window; units ordered from orders PLACED in the window. The two legitimately
    diverge (GOL-1978 review, Josh's decision)."""

    def test_no_pickings_zero_shipped(self):
        d = od.build_digest([_order(units=4)], today=TODAY)
        self.assertEqual(d["units_shipped"], 0)

    def test_picking_inside_window_counted(self):
        d = od.build_digest([_order()], pickings=[_picking(date_done=TODAY, units=4)], today=TODAY)
        self.assertEqual(d["units_shipped"], 4)

    def test_picking_on_period_start_boundary_counted(self):
        d = od.build_digest([], pickings=[_picking(date_done=PERIOD_START, units=3)], today=TODAY)
        self.assertEqual(d["units_shipped"], 3)

    def test_picking_outside_window_excluded(self):
        # Completed before the 7-day window opened -> not counted.
        old = _picking(date_done=date(2026, 1, 1), units=9)
        d = od.build_digest([], pickings=[old], today=TODAY)
        self.assertEqual(d["units_shipped"], 0)

    def test_picking_future_date_excluded(self):
        # Guard the upper bound too: a date_done after `today` is out of window.
        future = _picking(date_done=date(2026, 9, 8), units=5)
        d = od.build_digest([], pickings=[future], today=TODAY)
        self.assertEqual(d["units_shipped"], 0)

    def test_units_shipped_sums_across_pickings(self):
        d = od.build_digest([], pickings=[_picking(units=3), _picking(units=2)], today=TODAY)
        self.assertEqual(d["units_shipped"], 5)

    def test_picking_iso_string_date(self):
        d = od.build_digest([], pickings=[{"date_done": "2026-09-01", "units": 2}], today=TODAY)
        self.assertEqual(d["units_shipped"], 2)

    def test_units_ordered_only_counts_window_orders(self):
        orders = [
            _order(name="S1", date_order=TODAY, units=5),
            _order(name="S2", date_order=date(2026, 1, 1), units=8),  # outside window
        ]
        d = od.build_digest(orders, today=TODAY)
        self.assertEqual(d["units_ordered"], 5)

    def test_ordered_and_shipped_diverge_preorder_this_week(self):
        # A preorder placed this week contributes ordered units but nothing has
        # shipped yet -> ordered > 0, shipped == 0.
        preorder = _order(
            name="P1",
            date_order=TODAY,
            checkout_status="deposit_paid",
            preorder_ids="42",
            units=5,
        )
        d = od.build_digest([preorder], pickings=[], today=TODAY)
        self.assertEqual(d["units_ordered"], 5)
        self.assertEqual(d["units_shipped"], 0)


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
        self.assertIn("Units ordered", text)
        self.assertIn("Units shipped", text)
        self.assertIn("preorder", text.lower())
        self.assertIn("pickup", text.lower())

    def test_html_render_contains_key_sections(self):
        html = od.render_digest_html(self._digest())
        self.assertIn("<h2>", html)
        self.assertIn("Units ordered", html)
        self.assertIn("Units shipped", html)
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


class TestHtmlEscaping(unittest.TestCase):
    """Checkout-derived fields (partner_name, order_name, ship_season) must be
    HTML-escaped in the merchant email (GOL-1933/1978 review convention)."""

    def test_preorder_partner_and_order_name_escaped(self):
        malicious = _order(
            name="<script>x</script>",
            partner_name="<img src=x onerror=alert(1)>",
            date_order=date(2026, 5, 1),
            checkout_status="deposit_paid",
            preorder_ids="7",
        )
        html_out = od.render_digest_html(od.build_digest([malicious], today=TODAY))
        self.assertNotIn("<script>x</script>", html_out)
        self.assertNotIn("<img src=x", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("&lt;img", html_out)

    def test_pickup_partner_name_escaped(self):
        pk = _order(
            name="S9",
            partner_name="<b>evil</b>",
            is_pickup=True,
            checkout_status="paid",
            delivery_status=None,
        )
        html_out = od.render_digest_html(od.build_digest([pk], today=TODAY))
        self.assertNotIn("<b>evil</b>", html_out)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", html_out)

    def test_ship_season_escaped_in_html_only(self):
        def fake_zone(z):
            return 6

        def fake_wave(zone, today):
            return {
                "season": "<em>spring</em>",
                "ship_start": date(2027, 3, 15),
                "ship_end": date(2027, 3, 31),
            }

        pre = _order(
            name="S1",
            date_order=date(2026, 5, 1),
            checkout_status="deposit_paid",
            preorder_ids="7",
        )
        digest = od.build_digest([pre], today=TODAY, next_wave_fn=fake_wave, usda_zone_fn=fake_zone)
        html_out = od.render_digest_html(digest)
        self.assertNotIn("<em>spring</em>", html_out)
        self.assertIn("&lt;em&gt;spring&lt;/em&gt;", html_out)
        # Plain-text Discord output stays raw (not entity-encoded).
        text_out = od.render_digest_text(digest)
        self.assertIn("<em>spring</em>", text_out)
