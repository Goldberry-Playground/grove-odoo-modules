"""Pure tests for the Printify product mapping (no Odoo). Spec: 2026-10-13-printify-merch-design.md."""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "printify_sync.py")
_spec = importlib.util.spec_from_file_location("grove_printify_sync", _MODULE_PATH)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

RAW = {
    "id": "6650abc",
    "title": "Grove Tee",
    "description": "<p>Soft tee.</p>",
    "images": [{"src": "https://img/1.png", "is_default": False}, {"src": "https://img/2.png", "is_default": True}],
    "options": [
        {"name": "Colors", "type": "color", "values": [{"id": 521, "title": "Natural"}, {"id": 522, "title": "Sage"}]},
        {"name": "Sizes", "type": "size", "values": [{"id": 14, "title": "M"}, {"id": 18, "title": "2XL"}]},
    ],
    "variants": [
        {"id": 11001, "price": 2800, "cost": 1250, "is_enabled": True, "options": [521, 14]},
        {"id": 11002, "price": 3100, "cost": 1500, "is_enabled": True, "options": [521, 18]},
        {"id": 11003, "price": 2800, "cost": 1250, "is_enabled": False, "options": [522, 14]},
    ],
}


class TestMapProduct(unittest.TestCase):
    def test_maps_variants_to_color_and_size(self):
        m = ps.map_product(RAW)
        self.assertEqual(m["printify_product_id"], "6650abc")
        self.assertEqual(m["name"], "Grove Tee")
        self.assertEqual(m["image_url"], "https://img/2.png")
        first = m["variants"][0]
        self.assertEqual(
            first,
            {
                "printify_variant_id": 11001,
                "color": "Natural",
                "size": "M",
                "price": 28.0,
                "cost": 12.5,
                "enabled": True,
            },
        )
        self.assertFalse(m["variants"][2]["enabled"])

    def test_first_image_when_no_default(self):
        raw = dict(RAW, images=[{"src": "https://img/only.png"}])
        self.assertEqual(ps.map_product(raw)["image_url"], "https://img/only.png")

    def test_no_images(self):
        self.assertIsNone(ps.map_product(dict(RAW, images=[]))["image_url"])
