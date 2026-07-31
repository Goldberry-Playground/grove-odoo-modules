#!/usr/bin/env python3
"""Morning shipping rate-checker (design: vault wiki/Software/Grove Shipping).

Quotes Shippo (UPS Ground, residential) for each rate zone x catalog box
(shipping_boxes.BOXES at representative billable weight), computes
target = ceil(quote + per-box packaging + 2.00), and rewrites
grove_headless/data/shipping_rates.json when any zone drifts >= $1.
Exit codes: 0 no material drift | 3 rates file rewritten | 1 API failure.
Requires env SHIPPO_API_KEY (unless --dry-run with --fixture).
"""

import argparse
import importlib.util as _ilu
import json
import math
import os
import sys

import requests

ORIGIN = {
    "name": "Goldberry Grove",
    "street1": "PO handled at label time",
    "city": "Summersville",
    "state": "WV",
    "zip": "26651",
    "country": "US",
}
# One representative residential destination per rate zone.
REFERENCE_ZIPS = {
    "zone_1": ("NC", "27601"),
    "zone_2": ("OH", "43215"),
    "zone_3": ("IL", "60601"),
    "zone_4": ("MN", "55401"),
    "zone_5": ("ME", "04101"),
}
# Box Engine v2: reference parcels come straight from the box catalog —
# one quote per box id per zone, at the box's representative billable weight
# (worst typical fill across modes; never undercharge). Loaded by file path
# so this script stays standalone (no grove_headless package import).
_SB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "models", "shipping_boxes.py")
_spec = _ilu.spec_from_file_location("grove_shipping_boxes", _SB_PATH)
shipping_boxes = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(shipping_boxes)

PARCELS = {
    box_id: {
        "length": str(box["length"]),
        "width": str(box["width"]),
        "height": str(box["height"]),
        "distance_unit": "in",
        "weight": str(shipping_boxes.representative_billable_lb(box_id)),
        "mass_unit": "lb",
    }
    for box_id, box in shipping_boxes.BOXES.items()
}
# Per-box packaging (box + consumables: bag, paper, corrugate, bands, tape,
# sticker, care card, thank-you note) replaces the old flat $3.50/tree.
PACKAGING = {box_id: box["packaging_usd"] for box_id, box in shipping_boxes.BOXES.items()}
BUFFER = 2.00
RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "data", "shipping_rates.json")
OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


def pick_ups_ground(shipment_json: dict) -> float | None:
    rates = [
        float(r["amount"])
        for r in shipment_json.get("rates", [])
        if r.get("provider") == "UPS" and r.get("servicelevel", {}).get("token") == "ups_ground"
    ]
    return min(rates) if rates else None


def target_rate(quote: float, box_id: str) -> int:
    return math.ceil(quote + PACKAGING[box_id] + BUFFER)


def quote_zone_box(api_key: str, zone: str, box_id: str) -> float | None:
    state, zip5 = REFERENCE_ZIPS[zone]
    payload = {
        "address_from": ORIGIN,
        "address_to": {
            "name": "Rate Probe",
            "street1": "100 Main St",
            "city": "n/a",
            "state": state,
            "zip": zip5,
            "country": "US",
            "is_residential": True,
        },
        "parcels": [PARCELS[box_id]],
        "async": False,
    }
    resp = requests.post(
        "https://api.goshippo.com/shipments/",
        json=payload,
        timeout=30,
        headers={"Authorization": f"ShippoToken {api_key}"},
    )
    resp.raise_for_status()
    return pick_ups_ground(resp.json())


def compute_drift(current: dict, proposed: dict) -> list:
    """[(zone, tier, old, new)] where |old - new| >= 1.0."""
    drift = []
    for zone, boxes in proposed.items():
        for box_id, new in boxes.items():
            old = (current.get(zone, {}).get(box_id) or {}).get("base")
            if old is None or abs(float(old) - float(new)) >= 1.0:
                drift.append((zone, box_id, old, new))
    return sorted(drift)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", help="path to canned shipment JSON (testing)")
    args = ap.parse_args()

    with open(RATES_PATH, encoding="utf-8") as fh:
        current = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}

    proposed = {}
    for zone in REFERENCE_ZIPS:
        proposed[zone] = {}
        for box_id in PARCELS:
            if args.fixture:
                with open(args.fixture, encoding="utf-8") as fh:
                    quote = pick_ups_ground(json.load(fh))
            else:
                api_key = os.environ.get("SHIPPO_API_KEY", "")
                if not api_key:
                    print("SHIPPO_API_KEY not set", file=sys.stderr)
                    return 1
                try:
                    quote = quote_zone_box(api_key, zone, box_id)
                except requests.RequestException as exc:
                    print(f"shippo error for {zone}/{box_id}: {exc}", file=sys.stderr)
                    return 1
            if quote is None:
                print(f"no UPS Ground rate for {zone}/{box_id}", file=sys.stderr)
                return 1
            proposed[zone][box_id] = target_rate(quote, box_id)

    drift = compute_drift(current, proposed)
    if not drift:
        print("no material drift (<$1 everywhere)")
        return 0

    lines = ["| zone | box | current | proposed |", "|---|---|---|---|"]
    lines += [f"| {z} | {t} | {o} | {n} |" for z, t, o, n in drift]
    summary = "\n".join(lines)
    print(summary)
    if args.dry_run:
        return 0

    new_doc = {
        "_comment": "Maintained by scripts/rate_check (morning rate-checker). "
        "Per-box rates (Box Engine v2): ceil(Shippo UPS Ground at the box's "
        "representative billable weight + per-box packaging + 2.00 buffer). "
        "Design: vault wiki/Software/Grove Shipping.",
        "_schema": 2,
    }
    for zone in sorted(proposed):
        new_doc[zone] = {b: {"base": float(v)} for b, v in sorted(proposed[zone].items())}
    with open(RATES_PATH, "w", encoding="utf-8") as fh:
        json.dump(new_doc, fh, indent=2)
        fh.write("\n")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
