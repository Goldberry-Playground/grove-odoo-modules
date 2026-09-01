"""Thin Shippo REST client for label purchase (pure Python, mockable).

Injection point: pass `post=` (default requests.post) so Odoo methods and
tests share one code path. Design: vault wiki/Software/Grove Shipping.
"""

import os
import re

import requests

try:
    from . import shipping_boxes
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu
    import os as _os

    _sb_path = _os.path.join(_os.path.dirname(__file__), "shipping_boxes.py")
    _spec = _ilu.spec_from_file_location("grove_shipping_boxes", _sb_path)
    shipping_boxes = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(shipping_boxes)

API = "https://api.goshippo.com"

TRACKING_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")


def is_valid_tracking(value) -> bool:
    """Alphanumeric 6-40 chars — rejects SQL LIKE wildcards (%, _) and junk."""
    return bool(value) and isinstance(value, str) and bool(TRACKING_RE.match(value))


# Ship-from origin. `street1` previously held the literal placeholder
# "SET_AT_DEPLOY" with a comment claiming GROVE_SHIP_FROM_STREET overrode it —
# but nothing ever read that variable and ORIGIN was never mutated, so every
# Shippo call shipped from the literal string (GOL-988). Rate quotes hid this
# because UPS rates off city/state/ZIP, but a purchased label carried a garbage
# return address. The real farm street is now the default and the documented
# env override is actually wired.
ORIGIN = {
    "name": "Goldberry Grove",
    "street1": os.environ.get("GROVE_SHIP_FROM_STREET", "2291 Armstrong Road"),
    "city": "Summersville",
    "state": "WV",
    "zip": "26651",
    "country": "US",
}


class ShippoError(RuntimeError):
    pass


def build_shipment_payload(address: dict, box_id: str, count: int, mode: str) -> dict:
    """Shippo shipment payload for ONE packed box (Box Engine v2).

    Declares the estimated actual scale weight; the carrier applies DIM billing
    on its side from the dimensions, so we never under- or over-declare. Both
    ground services we quote (UPS Ground, USPS Ground Advantage) bill the greater
    of actual vs dimensional weight, so one declared parcel serves the race.
    """
    box = shipping_boxes.BOXES[box_id]
    parcel = {
        "length": str(box["length"]),
        "width": str(box["width"]),
        "height": str(box["height"]),
        "distance_unit": "in",
        "weight": str(max(1.0, shipping_boxes.actual_weight_lb(box_id, count, mode))),
        "mass_unit": "lb",
    }
    addr_to = dict(address)
    addr_to["is_residential"] = True
    return {"address_from": dict(ORIGIN), "address_to": addr_to, "parcels": [parcel], "async": False}


# Ground services we will quote AND buy, in a carrier-neutral least-cost race
# (GOL-1906, CEO ruling 2026-08-31: compare both carriers, take the cheapest per
# shipment — do NOT switch wholesale to one carrier). UPS Ground runs on Grove's
# own Shippo carrier account; USPS Ground Advantage runs on Shippo's shared
# commercial account. This is an ALLOWLIST, never a global min() over every
# returned rate: an unconstrained minimum could select an unexpected service, a
# carrier the account cannot actually purchase through, or an unacceptable
# transit. (provider, servicelevel.token) pairs, matched exactly.
GROUND_SERVICE_ALLOWLIST = frozenset(
    {
        ("UPS", "ups_ground"),
        ("USPS", "usps_ground_advantage"),
    }
)

# Transit guard for live plants (GOL-1906 constraint (a)). Pure least-cost will
# sometimes buy a slower service to save a small amount, on a product that is a
# living tree in a box. Never select a cheaper rate whose estimated transit
# exceeds the fastest allowlisted option by more than this many days. Starting
# tolerance = 1 day; Josh tunes this (horticultural call, not engineering). A
# rate that omits estimated_days is NOT excluded (unknown transit != slow) so a
# missing ETA can never drop the only purchasable rate.
GROUND_TRANSIT_TOLERANCE_DAYS = 1


def _allowlisted_ground_rates(rates: list) -> list:
    """Rates whose (provider, servicelevel.token) is in GROUND_SERVICE_ALLOWLIST."""
    out = []
    for r in rates:
        key = (r.get("provider"), (r.get("servicelevel") or {}).get("token"))
        if key in GROUND_SERVICE_ALLOWLIST:
            out.append(r)
    return out


def _rate_eta(rate: dict):
    days = rate.get("estimated_days")
    return days if isinstance(days, (int, float)) else None


def select_cheapest_ground(rates: list, tolerance_days: int = GROUND_TRANSIT_TOLERANCE_DAYS) -> dict | None:
    """Pick the cheapest allowlisted ground rate, subject to the transit guard.

    Returns the chosen rate dict, or None when no allowlisted rate is present —
    the caller decides whether that is a soft single-carrier condition or a hard
    error. Failover is free: if only one allowlisted carrier quotes, it wins
    (a whole-carrier outage stops being an order-blocking failure). Shared by
    the rate-table builder (scripts/rate_check) and label purchase below so the
    charge and the label are computed by identical rules.
    """
    candidates = _allowlisted_ground_rates(rates)
    if not candidates:
        return None
    etas = [_rate_eta(r) for r in candidates]
    known = [e for e in etas if e is not None]
    if known:
        limit = min(known) + tolerance_days
        guarded = [r for r in candidates if _rate_eta(r) is None or _rate_eta(r) <= limit]
    else:
        guarded = candidates
    return min(guarded, key=lambda r: float(r["amount"]))


def buy_cheapest_ground_label(api_key: str, payload: dict, post=requests.post) -> dict:
    """Buy the cheapest allowlisted ground label (UPS Ground vs USPS Ground
    Advantage) for one packed box, transit-guarded. Persists which carrier and
    service actually shipped so fulfilment and cost reconciliation can audit the
    charge against the label (GOL-1906)."""
    headers = {"Authorization": f"ShippoToken {api_key}"}
    resp = post(f"{API}/shipments/", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    rate = select_cheapest_ground(resp.json().get("rates", []))
    if rate is None:
        raise ShippoError("no allowlisted ground rate (UPS Ground / USPS Ground Advantage) returned for shipment")
    resp2 = post(
        f"{API}/transactions/",
        json={"rate": rate["object_id"], "label_file_type": "PDF", "async": False},
        headers=headers,
        timeout=60,
    )
    resp2.raise_for_status()
    txn = resp2.json()
    if txn.get("status") != "SUCCESS":
        raise ShippoError(f"label purchase failed: {txn.get('messages') or txn.get('status')}")
    servicelevel = rate.get("servicelevel") or {}
    return {
        "tracking_number": txn["tracking_number"],
        "label_url": txn["label_url"],
        "transaction_id": txn["object_id"],
        "carrier": rate.get("provider"),
        "servicelevel": servicelevel.get("token"),
        "amount": rate.get("amount"),
        "estimated_days": rate.get("estimated_days"),
    }
