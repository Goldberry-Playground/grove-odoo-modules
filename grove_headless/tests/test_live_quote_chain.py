"""Tests for the pure live -> ZIP-area -> zone fallback chain (GOL-3927, spec §5).

Loaded by file path so importing never drags in the Odoo addon package. Covers
each fallback level being reached and recorded, the order-level source rollup,
and the fail-safe (any unpriceable box fails the whole order to None).
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "live_quote_chain.py")
_spec = importlib.util.spec_from_file_location("grove_live_quote_chain", _MODULE_PATH)
lqc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lqc)


class TestResolveBox(unittest.TestCase):
    def test_live_hit_wins(self):
        b = lqc.resolve_box(
            "p24x10x4",
            6.0,
            live={"price": 11.83, "service": "03", "service_title": "UPS Ground"},
            zip_area=20.0,
            zone=22.0,
        )
        self.assertEqual(b.source, lqc.SOURCE_LIVE)
        self.assertEqual(b.price, 11.83)
        self.assertEqual(b.service, "03")
        self.assertIsNone(b.reason)

    def test_live_miss_falls_to_zip_area_with_reason(self):
        b = lqc.resolve_box("p24x10x4", 6.0, live={"reason": "Timeout"}, zip_area=20.0, zone=22.0)
        self.assertEqual(b.source, lqc.SOURCE_ZIP_AREA)
        self.assertEqual(b.price, 20.0)
        self.assertEqual(b.reason, "Timeout")
        self.assertIsNone(b.service)

    def test_live_miss_no_zip_area_falls_to_zone(self):
        b = lqc.resolve_box("p24x10x4", 6.0, live={"reason": "Blocked"}, zip_area=None, zone=22.0)
        self.assertEqual(b.source, lqc.SOURCE_ZONE)
        self.assertEqual(b.price, 22.0)
        self.assertEqual(b.reason, "Blocked")

    def test_live_not_attempted_falls_to_zone(self):
        b = lqc.resolve_box("p24x10x4", 6.0, live=None, zip_area=None, zone=22.0)
        self.assertEqual(b.source, lqc.SOURCE_ZONE)
        self.assertEqual(b.price, 22.0)
        self.assertEqual(b.reason, "no live quote")

    def test_unpriceable_everywhere_is_none(self):
        self.assertIsNone(lqc.resolve_box("p24x10x4", 6.0, live={"reason": "x"}, zip_area=None, zone=None))


class TestOrderRollup(unittest.TestCase):
    def test_all_live_total_and_source(self):
        boxes = [
            lqc.resolve_box("p24x10x4", 6.0, live={"price": 11.83, "service": "03"}),
            lqc.resolve_box("p24x10x6", 13.0, live={"price": 17.20, "service": "93"}),
        ]
        oq = lqc.quote_boxes(boxes)
        self.assertEqual(oq.carrier_total, 29.03)
        self.assertEqual(oq.source, lqc.SOURCE_LIVE)
        self.assertEqual(oq.fallback_reasons, ())

    def test_mixed_source_rolls_up_to_worst(self):
        boxes = [
            lqc.resolve_box("p24x10x4", 6.0, live={"price": 11.83}),
            lqc.resolve_box("p24x10x6", 13.0, live={"reason": "Timeout"}, zone=22.0),
        ]
        oq = lqc.quote_boxes(boxes)
        self.assertEqual(oq.source, lqc.SOURCE_ZONE)  # worst of {live, zone}
        self.assertEqual(oq.carrier_total, 33.83)
        self.assertIn("Timeout", oq.fallback_reasons)

    def test_zip_area_is_between_live_and_zone(self):
        boxes = [
            lqc.resolve_box("p24x10x4", 6.0, live={"price": 11.83}),
            lqc.resolve_box("p24x10x6", 13.0, live={"reason": "NoService"}, zip_area=18.0, zone=22.0),
        ]
        oq = lqc.quote_boxes(boxes)
        self.assertEqual(oq.source, lqc.SOURCE_ZIP_AREA)

    def test_any_unpriceable_box_fails_whole_order(self):
        boxes = [lqc.resolve_box("p24x10x4", 6.0, live={"price": 11.83}), None]
        self.assertIsNone(lqc.quote_boxes(boxes))

    def test_empty_is_none(self):
        self.assertIsNone(lqc.quote_boxes([]))

    def test_order_source_empty_defaults_zone(self):
        self.assertEqual(lqc.order_source([]), lqc.SOURCE_ZONE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
