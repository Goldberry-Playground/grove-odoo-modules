"""Tests for the product-photo resolution guardrail (GOL-837).

``models/image_resolution.py`` is pure Python (header-only parse, no Pillow, no
Odoo), so these run under CI's bare ``pytest`` job. The module is loaded by file
path — importing it never drags in the Odoo addon package. Same pattern as
``test_shipping_zones.py``.

Fixtures are hand-built minimal file headers (not full images): the parser only
reads the dimension bytes, so a valid header prefix is enough to prove it.
"""

import base64
import importlib.util
import os
import struct
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "image_resolution.py")
_spec = importlib.util.spec_from_file_location("grove_image_resolution", _MODULE_PATH)
ir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ir)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _png(width: int, height: int) -> bytes:
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + b"\x00" * 4


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 8


def _bmp(width: int, height: int) -> bytes:
    # 14-byte BITMAPFILEHEADER + BITMAPINFOHEADER with signed width/height.
    return b"BM" + b"\x00" * 16 + struct.pack("<ii", width, height) + b"\x00" * 12


def _jpeg(width: int, height: int) -> bytes:
    # SOI, an APP0 segment to skip past, then a real SOF0 carrying the dims.
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x00" * 10
    return soi + app0 + sof0


def _webp_vp8x(width: int, height: int) -> bytes:
    chunk = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
    chunk += struct.pack("<I", width - 1)[:3] + struct.pack("<I", height - 1)[:3]
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


class ReadImageDimensionsTest(unittest.TestCase):
    def test_png(self):
        self.assertEqual(ir.read_image_dimensions(_b64(_png(1920, 1280))), (1920, 1280))

    def test_gif(self):
        self.assertEqual(ir.read_image_dimensions(_b64(_gif(600, 450))), (600, 450))

    def test_bmp(self):
        self.assertEqual(ir.read_image_dimensions(_b64(_bmp(800, 600))), (800, 600))

    def test_bmp_top_down_negative_height(self):
        # Top-down bitmaps store a negative height; the magnitude is the size.
        self.assertEqual(ir.read_image_dimensions(_b64(_bmp(800, -600))), (800, 600))

    def test_jpeg_skips_app_segment(self):
        self.assertEqual(ir.read_image_dimensions(_b64(_jpeg(450, 337))), (450, 337))

    def test_webp_vp8x(self):
        self.assertEqual(ir.read_image_dimensions(_b64(_webp_vp8x(2048, 1536))), (2048, 1536))

    def test_empty_and_none(self):
        self.assertEqual(ir.read_image_dimensions(None), (0, 0))
        self.assertEqual(ir.read_image_dimensions(""), (0, 0))
        self.assertEqual(ir.read_image_dimensions(False), (0, 0))

    def test_unrecognized_and_corrupt(self):
        self.assertEqual(ir.read_image_dimensions(_b64(b"not an image at all")), (0, 0))
        self.assertEqual(ir.read_image_dimensions("!!!not base64!!!"), (0, 0))
        # Recognized magic number but truncated before the dimension bytes.
        self.assertEqual(ir.read_image_dimensions(_b64(b"\x89PNG\r\n\x1a\nIHDR")), (0, 0))


class IsLowResTest(unittest.TestCase):
    def test_below_threshold_is_flagged(self):
        # Real nursery.qa offenders from the GOL-837 audit.
        for long_edge in (450, 480, 531, 600, 612, 680, 830):
            self.assertTrue(ir.is_low_res(long_edge, int(long_edge * 0.75)), long_edge)

    def test_at_or_above_threshold_is_ok(self):
        self.assertFalse(ir.is_low_res(1600, 1200))
        self.assertFalse(ir.is_low_res(1200, 1600))  # long edge is the height
        self.assertFalse(ir.is_low_res(1920, 1280))

    def test_just_below_threshold_is_flagged(self):
        self.assertTrue(ir.is_low_res(1599, 1000))

    def test_imageless_is_not_low_res(self):
        # (0, 0) means "no photo" — handled by the frontend placeholder, not flagged.
        self.assertFalse(ir.is_low_res(0, 0))

    def test_custom_threshold(self):
        self.assertTrue(ir.is_low_res(1000, 800, threshold=1200))
        self.assertFalse(ir.is_low_res(1000, 800, threshold=800))

    def test_default_threshold_constant(self):
        self.assertEqual(ir.GROVE_MIN_IMAGE_LONG_EDGE, 1600)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
