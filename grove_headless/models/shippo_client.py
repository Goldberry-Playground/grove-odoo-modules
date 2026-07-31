"""Thin Shippo REST client for label purchase (pure Python, mockable).

Injection point: pass `post=` (default requests.post) so Odoo methods and
tests share one code path. Design: vault wiki/Software/Grove Shipping.
"""

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


ORIGIN = {
    "name": "Goldberry Grove",
    "street1": "SET_AT_DEPLOY",  # env GROVE_SHIP_FROM_STREET overrides
    "city": "Summersville",
    "state": "WV",
    "zip": "26651",
    "country": "US",
}


class ShippoError(RuntimeError):
    pass


def build_shipment_payload(address: dict, box_id: str, count: int, mode: str) -> dict:
    """Shippo shipment payload for ONE packed box (Box Engine v2).

    Declares the estimated actual scale weight; UPS applies DIM billing on
    its side from the dimensions, so we never under- or over-declare.
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


def buy_ups_ground_label(api_key: str, payload: dict, post=requests.post) -> dict:
    headers = {"Authorization": f"ShippoToken {api_key}"}
    resp = post(f"{API}/shipments/", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    rates = [
        r
        for r in resp.json().get("rates", [])
        if r.get("provider") == "UPS" and r.get("servicelevel", {}).get("token") == "ups_ground"
    ]
    if not rates:
        raise ShippoError("no UPS Ground rate returned for shipment")
    rate = min(rates, key=lambda r: float(r["amount"]))
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
    return {
        "tracking_number": txn["tracking_number"],
        "label_url": txn["label_url"],
        "transaction_id": txn["object_id"],
    }
