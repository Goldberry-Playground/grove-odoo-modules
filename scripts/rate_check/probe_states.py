#!/usr/bin/env python3
"""GOL-2128 one-shot: probe candidate green-list states and assign each to the
cheapest existing rate zone that never undercharges it.

For each candidate state we quote its WORST corner (farthest residential
destination from origin 26651) for every catalog box via the SAME least-cost
ground selector label purchase + the daily rate-checker use
(shippo_client.select_cheapest_ground: {UPS Ground, USPS Ground Advantage}).
We compute the state's own target rate per box — ceil(quote + per-box packaging
+ 2.00), identical to rate_check.target_rate — then find the cheapest existing
published zone whose rate >= that target for EVERY box. If no zone dominates,
the state needs pricing beyond the current 5-zone table and is flagged, not
guessed. Output is a table + a proposed ZONE_BY_STATE fragment.

Not wired into CI; run once to derive the ratified-spec assignments, then delete.
"""

import importlib.util as ilu
import json
import math
import os
import sys

import requests

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..", "..")


def _load(name, path):
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shipping_boxes = _load("sb", os.path.join(ROOT, "grove_headless", "models", "shipping_boxes.py"))
shippo_client = _load("sc", os.path.join(ROOT, "grove_headless", "models", "shippo_client.py"))

ORIGIN = {
    "name": "Goldberry Grove",
    "street1": "1 Farm Rd",
    "city": "Summersville",
    "state": "WV",
    "zip": "26651",
    "country": "US",
}
BUFFER = 2.00

# Corner per candidate state.
#
# REPRESENTATIVE-ZIP RULE (CEO directive, GOL-2238 P1 2026-09-14; bake into the
# Pirate Ship re-derive GOL-2270): a state's zone must reflect where its
# customers actually are, NOT its farthest tip. Binning by the far corner had TN
# (which borders WV via KY/VA) priced above Iowa because the probe quoted Memphis
# — the state's SW extremity. So for a near/border state whose spread crosses a
# zone boundary, probe a population-weighted / capital-city REPRESENTATIVE ZIP
# and bin on that; keep the far corner only as an undercharge sanity check, and
# add it to rate_check.REFERENCE_ZIPS so the published rate still dominates the
# worst member. For distant states with no near population (the far-plains
# tranche) the far corner IS representative, so worst-corner and representative
# coincide and either is fine.
#
# Format: {state: [(city, zip)]}; multiple entries -> pricier wins (worst-corner
# probe). Real city/ZIP pairs so UPS does not hard-reject the mismatch (GOL-1446).
CORNERS = {
    # Near/border states — REPRESENTATIVE ZIP (capital / population center).
    # 2026-09-14 re-probe: TN Nashville AND its far tip (Memphis) both quote the
    # zone_1 rate, so TN bins at zone_1. AR/MO/IA representative ZIPs quote the
    # zone_5 rate (same as their far corners), so they bin at zone_5.
    "TN": [("Nashville", "37201")],
    "AR": [("Little Rock", "72201")],
    "MO": [("Jefferson City", "65101")],
    "IA": [("Des Moines", "50309")],
    # Gulf-tier states — far corner IS the population, keep worst-corner.
    "GA": [("Valdosta", "31601")],
    "AL": [("Mobile", "36602")],
    "SC": [("Charleston", "29401")],
    "MS": [("Gulfport", "39501")],
    "LA": [("Lake Charles", "70601")],
    # Far-plains / desert tranche — worst-corner == representative (no near pop).
    "OK": [("Guymon", "73942"), ("Oklahoma City", "73102")],
    "KS": [("Goodland", "67735")],
    "NE": [("Scottsbluff", "69361")],
    "SD": [("Rapid City", "57701")],
    "ND": [("Williston", "58801")],
    "TX": [("El Paso", "79901"), ("Brownsville", "78520")],
    "NM": [("Gallup", "87301")],
    "AZ": [("Yuma", "85364")],
    "DC": [("Washington", "20001")],
    "FL": [("Miami", "33101"), ("Key West", "33040")],
}

PARCELS = {
    bid: {
        "length": str(b["length"]),
        "width": str(b["width"]),
        "height": str(b["height"]),
        "distance_unit": "in",
        "weight": str(shipping_boxes.representative_billable_lb(bid)),
        "mass_unit": "lb",
    }
    for bid, b in shipping_boxes.BOXES.items()
}
PKG = {bid: b["packaging_usd"] for bid, b in shipping_boxes.BOXES.items()}


def quote(api_key, state, city, zip5, box_id):
    payload = {
        "address_from": ORIGIN,
        "address_to": {
            "name": "Rate Probe",
            "street1": "100 Main St",
            "city": city,
            "state": state,
            "zip": zip5,
            "country": "US",
            "is_residential": True,
        },
        "parcels": [PARCELS[box_id]],
        "async": False,
    }
    r = requests.post(
        "https://api.goshippo.com/shipments/",
        json=payload,
        timeout=40,
        headers={"Authorization": f"ShippoToken {api_key}"},
    )
    r.raise_for_status()
    rate = shippo_client.select_cheapest_ground(r.json().get("rates", []))
    return float(rate["amount"]) if rate else None


def main():
    api_key = os.environ["SHIPPO_API_KEY"]
    rates = json.load(open(os.path.join(ROOT, "grove_headless", "data", "shipping_rates.json")))
    zones = {z: rates[z] for z in rates if not z.startswith("_")}
    boxes = list(PARCELS)

    results = {}
    for st, corners in CORNERS.items():
        # target per box = max over the state's corners of ceil(quote+pkg+buffer)
        target = {}
        gap = []
        for bid in boxes:
            best = None
            for city, zip5 in corners:
                q = quote(api_key, st, city, zip5, bid)
                if q is None:
                    continue
                t = math.ceil(q + PKG[bid] + BUFFER)
                best = t if best is None else max(best, t)
            if best is None:
                gap.append(bid)
            else:
                target[bid] = best
        # cheapest dominating zone: zone whose published rate >= target for ALL boxes,
        # minimizing total overbill.
        candidates = []
        for z, table in zones.items():
            if all(bid in target and bid in table and table[bid]["base"] >= target[bid] for bid in boxes):
                overbill = sum(table[bid]["base"] - target[bid] for bid in boxes)
                candidates.append((overbill, z))
        assigned = min(candidates)[1] if candidates else None
        results[st] = {
            "target": target,
            "gap": gap,
            "assigned": assigned,
            "worst_box": {bid: target.get(bid) for bid in boxes},
        }
        line = f"{st}: assigned={assigned or 'NONE(needs new zone)'}"
        if gap:
            line += f"  MISSING_QUOTE={gap}"
        print(line, file=sys.stderr)
        print("   target/box:", {b: target.get(b) for b in boxes}, file=sys.stderr)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main())
