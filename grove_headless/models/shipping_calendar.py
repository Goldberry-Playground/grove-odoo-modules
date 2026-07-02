"""USDA-zone shipping calendar for Grove checkout (design: vault
wiki/Software/Grove Shipping). Pure Python, stdlib only — mirrors the
shipping_zones.py testability contract.

Everything here keys off the DESTINATION USDA hardiness zone (int 2-10),
resolved from the shipping ZIP via the vendored PHZM matrix — never off
state (states span multiple USDA zones; WV alone runs 5a-7a).
"""

import csv
import os
from datetime import date, timedelta
from functools import lru_cache

_MATRIX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "zip_usda_zone.csv")


@lru_cache(maxsize=1)
def _zip_matrix() -> dict[str, int]:
    try:
        with open(_MATRIX_PATH, newline="", encoding="utf-8") as fh:
            return {row["zip"]: int(row["zone"]) for row in csv.DictReader(fh)}
    except (OSError, ValueError, KeyError):
        return {}


def usda_zone_for_zip(zip_code) -> int | None:
    """Integer USDA zone (2-10) for a 5-digit ZIP, or None if unknown."""
    if not zip_code or not isinstance(zip_code, str):
        return None
    zip5 = zip_code.strip()[:5]
    if len(zip5) != 5 or not zip5.isdigit():
        return None
    return _zip_matrix().get(zip5)
