"""Product-photo resolution guardrail (GOL-837) — pure Python, no ORM, no Pillow.

The storefront renders the detail hero at ~1056 device px (528 CSS @ DPR2) and
grid cards at ~684 device px. Any uploaded source whose long edge is below that
upscales and reads blurry, and the frontend cannot add resolution that is not in
the source — so the fix has to live at ingestion, in Odoo.

This module reads pixel dimensions straight from the image *header* bytes (no
full decode) for the formats product photos actually arrive as (PNG, JPEG, GIF,
WebP, BMP). It deliberately avoids Pillow so it stays importable — and unit
testable under CI's bare ``pytest`` job — without dragging in the Odoo runtime
or an extra dependency. Same standalone-testability contract as
``shipping_zones.py``.

Callers treat an unknown/unreadable header as (0, 0), i.e. "don't flag", so a
corrupt or exotic format is never mistaken for a low-resolution photo.
"""

import base64
import binascii
import logging

_logger = logging.getLogger(__name__)

# Minimum acceptable long edge for an uploaded product photo. Odoo caps stored
# originals at 1920 (product.template.image_1920), and the storefront hero needs
# ~1056+ device px, so 1600 sits comfortably above the render size while leaving
# headroom below the 1920 store cap. Below this the photo upscales and blurs.
GROVE_MIN_IMAGE_LONG_EDGE = 1600

# JPEG Start-Of-Frame markers that carry the frame dimensions. Excludes DHT
# (0xC4), DAC (0xCC) and the restart markers (0xD0-0xD7), which share the 0xCn
# range but are not SOF segments.
_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def read_image_dimensions(image_data):
    """Return ``(width, height)`` in px from base64 image bytes, else ``(0, 0)``.

    ``image_data`` is the base64 payload of an Odoo image field (str or bytes).
    Returns ``(0, 0)`` for empty/None input or an unrecognized/corrupt header so
    callers treat "unknown" as "do not flag" rather than false-flagging.
    """
    if not image_data:
        return 0, 0
    try:
        raw = base64.b64decode(image_data)
    except (binascii.Error, ValueError):
        return 0, 0
    try:
        return _dimensions_from_bytes(raw)
    except (IndexError, ValueError):
        # Truncated header on an otherwise-recognized magic number.
        _logger.warning("grove_headless: could not parse product image header", exc_info=True)
        return 0, 0


def is_low_res(width, height, threshold=GROVE_MIN_IMAGE_LONG_EDGE):
    """True when a *present* image's long edge is below ``threshold``.

    A (0, 0) / imageless product is not low-res — it is handled by the
    frontend's branded "Photo coming soon" placeholder, not flagged here.
    """
    long_edge = max(int(width or 0), int(height or 0))
    return bool(long_edge) and long_edge < threshold


def _dimensions_from_bytes(raw):
    # Shortest header we can read dims from is GIF (10 bytes). Over-short slices
    # below simply miss their magic check and fall through to (0, 0); Python
    # slicing never raises on out-of-range bounds.
    if len(raw) < 10:
        return 0, 0

    # PNG — IHDR is the first chunk; width/height are big-endian uint32 at 16/20.
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")

    # GIF — logical screen descriptor: little-endian uint16 at 6/8.
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")

    # BMP — DIB header: signed little-endian int32 at 18/22 (height may be < 0
    # for top-down bitmaps, so take the magnitude).
    if raw[:2] == b"BM":
        width = int.from_bytes(raw[18:22], "little", signed=True)
        height = int.from_bytes(raw[22:26], "little", signed=True)
        return abs(width), abs(height)

    # WebP — RIFF container with three sub-formats.
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return _webp_dimensions(raw)

    # JPEG — scan segment markers for the Start-Of-Frame.
    if raw[:2] == b"\xff\xd8":
        return _jpeg_dimensions(raw)

    return 0, 0


def _webp_dimensions(raw):
    fmt = raw[12:16]
    if fmt == b"VP8X":  # extended format: 24-bit (value-1) width/height at 24/27
        width = 1 + int.from_bytes(raw[24:27], "little")
        height = 1 + int.from_bytes(raw[27:30], "little")
        return width, height
    if fmt == b"VP8L":  # lossless: 14-bit dims packed after the 0x2F signature
        b = raw[21:25]
        width = 1 + (((b[1] & 0x3F) << 8) | b[0])
        height = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return width, height
    if fmt == b"VP8 ":  # lossy: 14-bit dims little-endian at 26/28
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return width, height
    return 0, 0


def _jpeg_dimensions(raw):
    i = 2
    n = len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in _JPEG_SOF_MARKERS:
            # Segment layout: FF marker, len(2), precision(1), height(2), width(2)
            height = int.from_bytes(raw[i + 5 : i + 7], "big")
            width = int.from_bytes(raw[i + 7 : i + 9], "big")
            return width, height
        # 0xFF fill byte before the real marker — consume one and re-align.
        if marker == 0xFF:
            i += 1
            continue
        # Standalone markers (SOI/EOI/RSTn) carry no length payload.
        if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(raw[i + 2 : i + 4], "big")
        if seg_len < 2:
            return 0, 0
        i += 2 + seg_len
    return 0, 0
