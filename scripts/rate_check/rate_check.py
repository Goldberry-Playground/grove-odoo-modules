#!/usr/bin/env python3
"""Morning shipping rate-checker (design: vault wiki/Software/Grove Shipping).

Quotes Shippo (USPS Ground Advantage, residential) for each rate zone x catalog box
(shipping_boxes.BOXES at representative billable weight), computes
target = ceil(quote + per-box packaging + 2.00), and rewrites
grove_headless/data/shipping_rates.json when any zone drifts >= $1.

Before writing, the proposed table is run through the monotonicity guard
(monotonicity.find_violations): within a zone a bigger box must never be
cheaper (cart-gaming). A violation aborts the rewrite (exit 4) so a bad quote
can never publish an exploitable table — the workflow alerts and rates stay
untouched. Each zone quotes its band's worst-case (priciest) destination so the
published rate is a band-wide upper bound — no undercharge (GOL-1495).

Exit codes: 0 no material drift (or Shippo has no USPS Ground Advantage rates at all yet
AND the current table is the provisional placeholder — account not finished,
skipped cleanly) | 3 rates file rewritten | 1 API failure, a partial rate gap
(some boxes quoted, some did not), or zero USPS Ground Advantage rates for every probe
while real published rates exist (USPS connection lapsed) | 4 proposed table
failed the monotonicity guard (not published).
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
# One reference residential destination per rate zone = the WORST-CASE
# (priciest) destination in that zone's state band, so the published per-zone
# rate is an upper bound for every customer in the band and no one is ever
# undercharged (GOL-1495, board-approved 2026-08-14). Because the 5
# state-distance bands don't track a carrier's own cost ordering, quoting a
# merely-representative city (e.g. Columbus for all of zone_2) would undercharge
# the band's far corner (NYC); quoting the far corner over-bills the cheapest
# in-band destination modestly -- the accepted cost of static zone pricing.
#
# !! CARRIER-SWITCH CAVEAT (UPS Ground -> USPS Ground Advantage) !!
# These worst-case picks were derived from live Shippo probes of each band's
# corner states against UPS GROUND (br16 + b32; box-invariant, UPS-zone driven).
# USPS prices on its OWN zone map (zones 1-9 by distance from origin 26651),
# which does not necessarily rank the same corners as worst-case. The GOL-1495
# "never undercharge" guarantee is therefore NOT yet proven under USPS -- the
# corner probes must be re-run per band before these picks can be trusted as
# upper bounds. Until that re-derivation lands, treat the published table as
# PROVISIONAL for USPS.
#
# The city MUST match the ZIP: carriers validate city against ZIP and can HARD-
# reject a mismatch, dropping the rate for that probe. A placeholder city
# ("n/a") passed silently on Shippo's shared test account but breaks on a live
# account for strictly-validated ZIPs -- GOL-1446 (observed on UPS; assume the
# same discipline applies to USPS).
REFERENCE_ZIPS = {
    "zone_1": ("Wilmington", "NC", "28401"),  # band {WV,VA,KY,NC,DE}; NC coast
    "zone_2": ("New York", "NY", "10001"),  # band {MD,PA,OH,IN,NJ,NY}
    "zone_3": ("Chicago", "IL", "60601"),  # band {IL,MI,CT,RI}
    "zone_4": ("Boston", "MA", "02108"),  # band {WI,MN,MA,VT,NH}
    "zone_5": ("Portland", "ME", "04101"),  # band {ME}
}
# Box Engine v2: reference parcels come straight from the box catalog —
# one quote per box id per zone, at the box's representative billable weight
# (worst typical fill across modes; never undercharge). Loaded by file path
# so this script stays standalone (no grove_headless package import).
_SB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "models", "shipping_boxes.py")
_spec = _ilu.spec_from_file_location("grove_shipping_boxes", _SB_PATH)
shipping_boxes = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(shipping_boxes)

# Monotonicity guard — loaded by file path so this script stays standalone
# whether run directly (`python3 rate_check.py`) or imported by tests via
# spec (which does not put this dir on sys.path).
_MONO_PATH = os.path.join(os.path.dirname(__file__), "monotonicity.py")
_mspec = _ilu.spec_from_file_location("grove_rate_monotonicity", _MONO_PATH)
monotonicity = _ilu.module_from_spec(_mspec)
_mspec.loader.exec_module(monotonicity)

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


def pick_usps_ground_advantage(shipment_json: dict) -> float | None:
    rates = [
        float(r["amount"])
        for r in shipment_json.get("rates", [])
        if r.get("provider") == "USPS" and r.get("servicelevel", {}).get("token") == "usps_ground_advantage"
    ]
    return min(rates) if rates else None


def target_rate(quote: float, box_id: str) -> int:
    return math.ceil(quote + PACKAGING[box_id] + BUFFER)


def quote_zone_box(api_key: str, zone: str, box_id: str) -> float | None:
    city, state, zip5 = REFERENCE_ZIPS[zone]
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
    resp = requests.post(
        "https://api.goshippo.com/shipments/",
        json=payload,
        timeout=30,
        headers={"Authorization": f"ShippoToken {api_key}"},
    )
    resp.raise_for_status()
    return pick_usps_ground_advantage(resp.json())


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
        raw = json.load(fh)
    # `_provisional` marks the launch-hypothesis placeholder table: real
    # published rates do not exist yet, so an all-missing Shippo result is the
    # "USPS not connected" not-ready state, not a lapse. The writer below emits a
    # doc WITHOUT this key, so once real rates publish the placeholder guard is
    # gone and a later all-missing result fails loudly (GOL-1312).
    provisional = bool(raw.get("_provisional"))
    current = {k: v for k, v in raw.items() if not k.startswith("_")}

    proposed = {}
    missing = []
    for zone in REFERENCE_ZIPS:
        proposed[zone] = {}
        for box_id in PARCELS:
            if args.fixture:
                with open(args.fixture, encoding="utf-8") as fh:
                    quote = pick_usps_ground_advantage(json.load(fh))
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
                # Shippo answered (HTTP 200) but returned no USPS Ground Advantage rate
                # for this probe. Record it and keep going so we can tell a
                # total absence (account not finished) from a partial gap.
                missing.append(f"{zone}/{box_id}")
                continue
            proposed[zone][box_id] = target_rate(quote, box_id)

    if missing:
        total = len(REFERENCE_ZIPS) * len(PARCELS)
        if len(missing) == total:
            if provisional or not current:
                # Zero USPS Ground Advantage rates across every probe AND the current
                # table is the placeholder/empty one (no real published
                # rates to protect): the SHIPPO_API_KEY secret exists but no
                # USPS carrier is connected in the Shippo account yet — the
                # same not-ready state the workflow's pre-key guard covers.
                # Skip cleanly (exit 0) instead of paging ops every morning;
                # the job self-heals the day USPS goes live (GOL-1296).
                print(
                    "::notice::Shippo returned no USPS Ground Advantage rate for any probe — "
                    "USPS carrier not connected in the Shippo account yet; "
                    "rate-check skipped (see GOL-1296)"
                )
                print("no USPS Ground Advantage rates available yet — skipped")
                return 0
            # Real published rates exist (non-placeholder table) yet Shippo
            # now returns zero USPS Ground Advantage rates for EVERY probe: the carrier
            # connection that produced those rates has lapsed (auth expiry,
            # billing, Shippo-side disconnect). Fail loudly — a clean skip
            # here would let shipping_rates.json fossilize while a later USPS
            # rate hike silently under-bills every order (GOL-1312).
            print(
                f"no USPS Ground Advantage rate for any of {total} probe(s) but "
                "shipping_rates.json holds real published rates — "
                "USPS connection lost? (see GOL-1312)",
                file=sys.stderr,
            )
            return 1
        # A PARTIAL gap (some boxes rated, some not) is a real quote problem
        # — e.g. an oversize box or a bad reference address — and must fail
        # loudly so it gets investigated, never silently drop a rate.
        print(
            f"no USPS Ground Advantage rate for {len(missing)} of {total} probe(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    # Guard before publishing: within a zone a bigger box must never be
    # cheaper (cart-gaming). A bad Shippo quote that inverts the table is
    # refused, not written — the workflow's failure alert fires and rates stay
    # untouched. Cross-zone monotonicity is intentionally NOT enforced: real USPS
    # doesn't order our state bands by cost, and worst-case reference ZIPs above
    # already guarantee no undercharge (GOL-1495).
    box_order = monotonicity.ordered_boxes(shipping_boxes.BOXES, shipping_boxes.representative_billable_lb)
    violations = monotonicity.find_violations(proposed, box_order, list(REFERENCE_ZIPS))
    if violations:
        print(f"proposed rate table failed monotonicity guard ({len(violations)}):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 4

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
        "Per-box rates (Box Engine v2): ceil(Shippo USPS Ground Advantage at the box's "
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
