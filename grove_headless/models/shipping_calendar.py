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
    except (OSError, ValueError, KeyError, csv.Error):
        return {}


def usda_zone_for_zip(zip_code) -> int | None:
    """Integer USDA zone (2-10) for a 5-digit ZIP, or None if unknown."""
    if not zip_code or not isinstance(zip_code, str):
        return None
    raw = zip_code.strip()
    if not (len(raw) == 5 or (len(raw) == 10 and raw[5] == "-")):
        return None
    zip5 = raw[:5]
    if not zip5.isdigit():
        return None
    return _zip_matrix().get(zip5)


# ── Calendar data (Josh, 2026-07-02; vault wiki/Software/Grove Shipping) ────
# (month, day) tuples; year resolved at query time.
WAVE_SCHEDULE: dict[int, dict] = {
    2: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 13), "order_by": (11, 12)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    3: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 13), "order_by": (11, 12)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    4: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 19), "order_by": (11, 16)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    5: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 19), "order_by": (11, 16)},
        "spring": {"ship_start": (4, 12), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    6: {
        "fall": {"ship_start": (11, 9), "ship_end": (11, 26), "order_by": (11, 21)},
        "spring": {"ship_start": (4, 5), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    7: {
        "fall": {"ship_start": (11, 9), "ship_end": (11, 26), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 16), "ship_end": (5, 24), "order_by": (5, 17)},
    },
    8: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
    9: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
    10: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
}

# On-demand no-ship ranges (conservative launch defaults — loosen with
# nursery-manager experience via PR). Jan+Feb is the global floor regardless.
NO_SHIP_MONTHS = (1, 2)
FREEZE_WINDOWS: dict[int, tuple] = {
    2: ((12, 1), (3, 15)),
    3: ((12, 1), (3, 15)),
    4: ((12, 1), (3, 15)),
    5: ((12, 1), (3, 15)),
    6: ((12, 15), (3, 1)),
    7: ((12, 15), (3, 1)),
    8: ((1, 1), (2, 28)),
    9: ((1, 1), (2, 28)),
    10: ((1, 1), (2, 28)),
}


def _in_md_window(today: date, start_md, end_md) -> bool:
    """Is `today` inside a (month, day) window that may wrap the year end?"""
    t = (today.month, today.day)
    if start_md <= end_md:
        return start_md <= t <= end_md
    return t >= start_md or t <= end_md  # wraps Dec -> Mar


def _next_occurrence(md, today: date) -> date:
    """The next date with (month, day) == md on or after today."""
    m, d = md
    candidate = date(today.year, m, min(d, 28) if (m, d) == (2, 29) else d)
    return candidate if candidate >= today else date(today.year + 1, m, d)


def _next_wave(zone: int, today: date) -> dict | None:
    """Return the next bareroot wave (fall or spring) with the earliest ship_start."""
    waves = WAVE_SCHEDULE.get(zone)
    if not waves:
        return None
    candidates = []
    for season, w in waves.items():
        order_by = _next_occurrence(w["order_by"], today)
        # Wave ship dates share the same year as their order_by deadline.
        ship_start = date(order_by.year, *w["ship_start"])
        ship_end = date(order_by.year, *w["ship_end"])
        candidates.append(
            {
                "season": season,
                "ship_start": ship_start,
                "ship_end": ship_end,
                "order_by": order_by,
            }
        )
    return min(candidates, key=lambda c: c["ship_start"])


def _freeze_end(zone: int, today: date) -> date:
    end_md = FREEZE_WINDOWS[zone][1]
    return _next_occurrence(end_md, today) + timedelta(days=1)


def ship_options(zip_code, tier: str, today: date) -> dict:
    """Can this order ship now, and if not, when? See vault spec.

    Conservative on unknowns: unrecognized ZIP -> ships_now False.
    """
    zone = usda_zone_for_zip(zip_code)
    result = {"usda_zone": zone, "ships_now": False, "next_wave": None, "defer_to": None}
    if zone is None:
        return result
    frozen = today.month in NO_SHIP_MONTHS or _in_md_window(today, *FREEZE_WINDOWS[zone])
    result["ships_now"] = not frozen
    if tier == "bareroot":
        result["next_wave"] = _next_wave(zone, today)
        if frozen:
            result["defer_to"] = result["next_wave"]["ship_start"] if result["next_wave"] else None
    elif frozen:
        result["defer_to"] = _freeze_end(zone, today)
    return result
