"""Consult travel estimator (spec C.7).

Straight-line distance from the farm ZIP centroid to the client ZIP centroid,
scaled by a road factor, turned into drive hours and billable hours for a
consult visit. Pure Python, stdlib only (same testability contract as
``shipping_zones.py`` / ``shipping_calendar.py``) so the maths can be unit-tested
without a database; the C.7 route in ``controllers/consult.py`` resolves the two
ZIP centroids from ``data/zip_usda_zone.csv`` and calls :func:`estimate`.

The rules (Josh, draft; the exact dollars live on the ``travel_*`` service
products in ``data/consult_products.xml`` and Josh edits them — this estimate is
advisory, so a changed policy never needs a code change to the money):

* ``ROAD_FACTOR`` (1.3) turns the great-circle mile into a driven mile.
* ``MPH`` (45) turns driven miles into hours, one way.
* ``FREE_HOURS`` (1.0) — the first hour each way is unbilled ("Travel, outbound
  beyond the first hour" is the billable product), so billable one-way hours are
  ``max(0, one_way_hours - 1)``.
* ``OVERNIGHT_THRESHOLD_H`` (3.0) — the $600 three-hour rule: a visit whose
  one-way drive reaches three hours needs an overnight, flagged ``overnight`` so
  the quote adds the ``travel_night`` allowance (``OVERNIGHT_USD`` = 600).
"""

import csv
import math
import os
from functools import lru_cache

ROAD_FACTOR = 1.3
MPH = 45.0
FREE_HOURS = 1.0
OVERNIGHT_THRESHOLD_H = 3.0
OVERNIGHT_USD = 600.0
MILE_PER_KM = 0.621371

_MATRIX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "zip_usda_zone.csv")


@lru_cache(maxsize=1)
def _centroid_matrix() -> dict[str, tuple[float, float]]:
    """``{zip5: (lat, lon)}`` from the vendored ZIP table. ZIPs whose ``lat``/
    ``lon`` cells are blank (the Gazetteer had no ZCTA) are omitted."""
    out: dict[str, tuple[float, float]] = {}
    try:
        with open(_MATRIX_PATH, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                lat, lon = (row.get("lat") or "").strip(), (row.get("lon") or "").strip()
                if not lat or not lon:
                    continue
                try:
                    out[row["zip"]] = (float(lat), float(lon))
                except (ValueError, KeyError):
                    continue
    except (OSError, csv.Error):
        return {}
    return out


def centroid_for_zip(zip_code) -> tuple[float, float] | None:
    """``(lat, lon)`` centroid for a 5-digit ZIP, or ``None`` if unknown."""
    if not zip_code or not isinstance(zip_code, str):
        return None
    raw = zip_code.strip()
    if not (len(raw) == 5 or (len(raw) == 10 and raw[5] == "-")):
        return None
    zip5 = raw[:5]
    if not zip5.isdigit():
        return None
    return _centroid_matrix().get(zip5)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles between two lat/lon points."""
    r_miles = 3958.7613  # mean Earth radius
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))


def estimate(
    origin: tuple[float, float],
    dest: tuple[float, float],
    *,
    road_factor: float = ROAD_FACTOR,
    mph: float = MPH,
    free_hours: float = FREE_HOURS,
    overnight_threshold_h: float = OVERNIGHT_THRESHOLD_H,
    overnight_usd: float = OVERNIGHT_USD,
) -> dict:
    """Travel estimate from ``origin`` to ``dest`` (both ``(lat, lon)``).

    Returns the C.5 ``grove_travel_estimate`` shape: ``km`` and ``miles`` are
    one-way driven distance (great-circle × ``road_factor``); ``hours`` is the
    one-way drive time; ``billable_hours`` is one-way hours past the first
    (``travel_hour``); ``overnight`` is the $600 three-hour rule; ``round_trip_miles``
    feeds the per-mile product. All distances are one way unless named round trip.
    """
    straight_miles = haversine_miles(origin[0], origin[1], dest[0], dest[1])
    road_miles = straight_miles * road_factor
    one_way_hours = road_miles / mph if mph else 0.0
    billable_hours = max(0.0, one_way_hours - free_hours)
    overnight = one_way_hours >= overnight_threshold_h
    return {
        "straight_miles": round(straight_miles, 1),
        "miles": round(road_miles, 1),
        "round_trip_miles": round(road_miles * 2, 1),
        "km": round(road_miles / MILE_PER_KM, 1),
        "hours": round(one_way_hours, 2),
        "billable_hours": round(billable_hours, 2),
        "overnight": overnight,
        "overnight_usd": overnight_usd if overnight else 0.0,
        "road_factor": road_factor,
        "mph": mph,
    }


def estimate_for_zips(farm_zip, client_zip) -> dict | None:
    """Convenience: resolve both centroids from the ZIP table and estimate, or
    ``None`` when either ZIP has no centroid."""
    origin = centroid_for_zip(farm_zip)
    dest = centroid_for_zip(client_zip)
    if origin is None or dest is None:
        return None
    out = estimate(origin, dest)
    out["farm_zip"] = str(farm_zip)
    out["zip"] = str(client_zip)
    return out
