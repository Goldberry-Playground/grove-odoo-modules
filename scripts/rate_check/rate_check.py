#!/usr/bin/env python3
"""Morning shipping rate-checker (design: vault wiki/Software/Grove Shipping).

Quotes Shippo (UPS Ground, residential) for each rate zone x catalog box
(shipping_boxes.BOXES at representative billable weight), computes
target = ceil(quote + per-box packaging + 2.00), and rewrites
grove_headless/data/shipping_rates.json when any zone drifts >= $1.

Before writing, the proposed table is run through the monotonicity guard
(monotonicity.find_violations): a bigger box or a farther zone must never be
cheaper. A violation aborts the rewrite (exit 4) so a bad quote can never
publish an exploitable table — the workflow alerts and rates stay untouched.

Exit codes: 0 no material drift (or Shippo has no UPS Ground rates at all yet
AND the current table is the provisional placeholder — account not finished,
skipped cleanly) | 3 rates file rewritten | 1 API failure, a partial rate gap
(some boxes quoted, some did not), or zero UPS Ground rates for every probe
while real published rates exist (UPS connection lapsed) | 4 proposed table
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
# One representative residential destination per rate zone. Each entry is a
# real (city, state, zip) — a placeholder city like "n/a" makes UPS refuse to
# rate some dense multi-city ZIPs (Columbus 43215, Chicago 60601), so every
# probe city must be the ZIP's actual city (GOL-1447).
REFERENCE_ZIPS = {
    "zone_1": ("Raleigh", "NC", "27601"),
    "zone_2": ("Columbus", "OH", "43215"),
    "zone_3": ("Chicago", "IL", "60601"),
    "zone_4": ("Minneapolis", "MN", "55401"),
    "zone_5": ("Portland", "ME", "04101"),
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
        raw = json.load(fh)
    # `_provisional` marks the launch-hypothesis placeholder table: real
    # published rates do not exist yet, so an all-missing Shippo result is the
    # "UPS not connected" not-ready state, not a lapse. The writer below emits a
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
                # Shippo answered (HTTP 200) but returned no UPS Ground rate
                # for this probe. Record it and keep going so we can tell a
                # total absence (account not finished) from a partial gap.
                missing.append(f"{zone}/{box_id}")
                continue
            proposed[zone][box_id] = target_rate(quote, box_id)

    if missing:
        total = len(REFERENCE_ZIPS) * len(PARCELS)
        if len(missing) == total:
            if provisional or not current:
                # Zero UPS Ground rates across every probe AND the current
                # table is the placeholder/empty one (no real published
                # rates to protect): the SHIPPO_API_KEY secret exists but no
                # UPS carrier is connected in the Shippo account yet — the
                # same not-ready state the workflow's pre-key guard covers.
                # Skip cleanly (exit 0) instead of paging ops every morning;
                # the job self-heals the day UPS goes live (GOL-1296).
                print(
                    "::notice::Shippo returned no UPS Ground rate for any probe — "
                    "UPS carrier not connected in the Shippo account yet; "
                    "rate-check skipped (see GOL-1296)"
                )
                print("no UPS Ground rates available yet — skipped")
                return 0
            # Real published rates exist (non-placeholder table) yet Shippo
            # now returns zero UPS Ground rates for EVERY probe: the carrier
            # connection that produced those rates has lapsed (auth expiry,
            # billing, Shippo-side disconnect). Fail loudly — a clean skip
            # here would let shipping_rates.json fossilize while a later UPS
            # rate hike silently under-bills every order (GOL-1312).
            print(
                f"no UPS Ground rate for any of {total} probe(s) but "
                "shipping_rates.json holds real published rates — "
                "UPS connection lost? (see GOL-1312)",
                file=sys.stderr,
            )
            return 1
        # A PARTIAL gap (some boxes rated, some not) is a real quote problem
        # — e.g. an oversize box or a bad reference address — and must fail
        # loudly so it gets investigated, never silently drop a rate.
        print(
            f"no UPS Ground rate for {len(missing)} of {total} probe(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    # Guard before publishing: a bigger box or farther zone must never be
    # cheaper. A bad Shippo quote that inverts the table is refused, not
    # written — the workflow's failure alert fires and rates stay untouched.
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
