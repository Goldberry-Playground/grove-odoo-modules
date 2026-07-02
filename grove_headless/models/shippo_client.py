"""Thin Shippo REST client for label purchase (pure Python, mockable).

Injection point: pass `post=` (default requests.post) so Odoo methods and
tests share one code path. Design: vault wiki/Software/Grove Shipping.
"""

import requests

try:
    from .shipping_zones import PARCEL_PROFILES
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu
    import os as _os

    _sz_path = _os.path.join(_os.path.dirname(__file__), "shipping_zones.py")
    _spec = _ilu.spec_from_file_location("grove_shipping_zones", _sz_path)
    _sz = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_sz)
    PARCEL_PROFILES = _sz.PARCEL_PROFILES

API = "https://api.goshippo.com"
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


def build_shipment_payload(address: dict, tier: str) -> dict:
    profile = PARCEL_PROFILES.get(tier, PARCEL_PROFILES["potted"])
    parcel = {
        "length": str(profile["length"]),
        "width": str(profile["width"]),
        "height": str(profile["height"]),
        "distance_unit": "in",
        "weight": str(profile["weight_lb"]),
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
