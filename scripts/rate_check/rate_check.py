#!/usr/bin/env python3
"""Morning shipping rate-checker (design: vault wiki/Software/Grove Shipping).

Quotes Shippo (UPS Ground, residential) for each rate zone x tier reference
parcel, computes target = ceil(quote + 3.50 + 2.00), and rewrites
grove_headless/data/shipping_rates.json when any zone drifts >= $1.
Exit codes: 0 no material drift | 3 rates file rewritten | 1 API failure.
Requires env SHIPPO_API_KEY (unless --dry-run with --fixture).
"""

import argparse
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
PARCELS = {
    "bareroot": {"length": "48", "width": "6", "height": "6", "distance_unit": "in", "weight": "4", "mass_unit": "lb"},
    "potted": {"length": "30", "width": "16", "height": "16", "distance_unit": "in", "weight": "25", "mass_unit": "lb"},
}
PACKAGING, BUFFER = 3.50, 2.00
RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "data", "shipping_rates.json")
OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


def pick_ups_ground(shipment_json: dict) -> float | None:
    rates = [
        float(r["amount"])
        for r in shipment_json.get("rates", [])
        if r.get("provider") == "UPS" and r.get("servicelevel", {}).get("token") == "ups_ground"
    ]
    return min(rates) if rates else None


def target_rate(quote: float) -> int:
    return math.ceil(quote + PACKAGING + BUFFER)


def quote_zone_tier(api_key: str, zone: str, tier: str) -> float | None:
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
        "parcels": [PARCELS[tier]],
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
    for zone, tiers in proposed.items():
        for tier, new in tiers.items():
            old = (current.get(zone, {}).get(tier) or {}).get("base")
            if old is None or abs(float(old) - float(new)) >= 1.0:
                drift.append((zone, tier, old, new))
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
        for tier in PARCELS:
            if args.fixture:
                with open(args.fixture, encoding="utf-8") as fh:
                    quote = pick_ups_ground(json.load(fh))
            else:
                api_key = os.environ.get("SHIPPO_API_KEY", "")
                if not api_key:
                    print("SHIPPO_API_KEY not set", file=sys.stderr)
                    return 1
                try:
                    quote = quote_zone_tier(api_key, zone, tier)
                except requests.RequestException as exc:
                    print(f"shippo error for {zone}/{tier}: {exc}", file=sys.stderr)
                    return 1
            if quote is None:
                print(f"no UPS Ground rate for {zone}/{tier}", file=sys.stderr)
                return 1
            proposed[zone][tier] = target_rate(quote)

    drift = compute_drift(current, proposed)
    if not drift:
        print("no material drift (<$1 everywhere)")
        return 0

    lines = ["| zone | tier | current | proposed |", "|---|---|---|---|"]
    lines += [f"| {z} | {t} | {o} | {n} |" for z, t, o, n in drift]
    summary = "\n".join(lines)
    print(summary)
    if args.dry_run:
        return 0

    new_doc = {
        "_comment": "Maintained by scripts/rate_check (morning rate-checker). "
        "Derivation: ceil(Shippo UPS Ground + 3.50 + 2.00). "
        "Design: vault wiki/Software/Grove Shipping."
    }
    for zone in sorted(proposed):
        new_doc[zone] = {t: {"base": float(v)} for t, v in sorted(proposed[zone].items())}
    with open(RATES_PATH, "w", encoding="utf-8") as fh:
        json.dump(new_doc, fh, indent=2)
        fh.write("\n")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
