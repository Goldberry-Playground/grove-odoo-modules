#!/usr/bin/env python3
"""Morning shipping rate-checker — Pirate Ship rate source (GOL-2270).

Quotes Pirate Ship's public rate calculator (least-cost allowlisted ground:
UPS Ground / UPS Ground Saver / USPS Ground Advantage, residential) for each
rate zone x catalog box (shipping_boxes at representative billable weight),
computes target = ceil(quote + per-box packaging + 2.00), and rewrites
grove_headless/data/shipping_rates.json when any zone drifts >= $1. Pirate Ship
retires Shippo from quoting (design: spec docs/superpowers/specs/
2026-09-09-pirateship-fulfillment-design.md section A, ratified Josh 2026-09-09;
vault wiki/Software/Grove Pirate Ship Fulfillment).

Each probe POSTs the `RatesQuery` operation to
``https://ship.pirateship.com/api/graphql?opname=RatesQuery`` (no auth) with the
box's dimensions and its representative billable weight IN OUNCES, requesting
mail classes ["03","93","GroundAdvantage"] (UPS Ground, UPS Ground Saver, USPS
Ground Advantage), package type ["Parcel"], residential destination. Selection
stays "cheapest allowlisted ground within the transit ceiling": the allowlist is
{("UPS","03"),("UPS","93"),("USPS","GroundAdvantage")} matched on (carrier
title, mailClassKey), and transit days come from Pirate Ship's
``deliveryDescription`` estimated-delivery date minus the probe date (an
unparsable date is NOT excluded — unknown transit != slow, same rule the label
purchase uses).

schema 3: each published cell records the WINNER —
``{"base": 22.0, "carrier": "UPS", "service": "03", "service_title": "UPS Ground"}``.
The Odoo loader (shipping_zones._load_rates) reads ``base`` only and ignores the
extra keys (backward compatible); rate_feed passes ``carrier``/``service_title``
through for storefront copy; sub-project B reads the winner to pick the service
in Pirate Ship. The visibility report prints the winning service per cell, so
"which carrier set this rate" is never a question again.

Before writing, the proposed table runs through the monotonicity guard
(monotonicity.find_violations): within a zone a bigger box must never be cheaper
(cart-gaming). A violation aborts the rewrite (exit 4). Each zone quotes its
band's worst-case (priciest) corner(s) and publishes the per-box MAX so the
published rate is a band-wide upper bound — no undercharge (GOL-1495, GOL-2128).

Exit codes: 0 no material drift (or Pirate Ship returns no allowlisted ground
rate for any probe AND the current table is the provisional placeholder — not
ready, skipped cleanly) | 3 rates file rewritten | 1 a partial rate gap (some
boxes quoted, some did not), or zero ground rates for every probe while real
published rates exist | 4 proposed table failed the monotonicity guard.

No secret is required: the rate calculator is public.
"""

import argparse
import importlib.util as _ilu
import json
import math
import os
import re
import sys
from datetime import date

import requests

# Probe origin (Goldberry Grove, Summersville WV). Pirate Ship rates off the
# origin zip/city/region; no street is sent to the quote endpoint.
ORIGIN = {"city": "Summersville", "state": "WV", "zip": "26651"}

# One or more reference residential destinations per rate zone — each a
# WORST-CASE (priciest ground) corner in that zone's state band; the published
# per-zone, per-box rate is the MAX across the zone's corners, so it is an upper
# bound for every customer in the band and no one is undercharged (GOL-1495,
# GOL-2128). The city MUST match the zip (carriers validate city against zip).
REFERENCE_ZIPS = {
    # band {WV,VA,KY,NC,DE,DC,TN}; corners = NC coast + TN's farthest tip
    # (Memphis). GOL-2238 (2026-09-14): TN joins zone_1 — Memphis quotes the
    # zone_1 rate exactly — and Memphis is kept as a corner so the published
    # zone_1 rate stays >= TN's worst going forward (never undercharge).
    "zone_1": [
        ("Wilmington", "NC", "28401"),
        ("Memphis", "TN", "38103"),
    ],
    "zone_2": [("New York", "NY", "10001")],  # band {MD,PA,OH,IN,NJ,NY}
    "zone_3": [("Chicago", "IL", "60601")],  # band {IL,MI,CT,RI}
    "zone_4": [("Boston", "MA", "02108")],  # band {WI,MN,MA,VT,NH}
    # band {ME,GA,SC,AL,MS,LA,AR,MO,IA} — max across the Gulf/NE + mid-continent
    # corners. GOL-2238 (2026-09-14) folded AR/MO/IA back here (their worst
    # corners quote the zone_5 rate exactly) after the 2026-09-08 zone_6/zone_7
    # split proved to overcharge them; those bands are retired.
    "zone_5": [
        ("Portland", "ME", "04101"),
        ("Mobile", "AL", "36602"),
        ("Gulfport", "MS", "39501"),
        ("Lake Charles", "LA", "70601"),
        ("Texarkana", "AR", "71854"),
        ("Joplin", "MO", "64801"),
        ("Sioux City", "IA", "51101"),
    ],
}

# Box Engine v2: reference parcels come straight from the box catalog — one
# quote per box id per zone, at the box's representative billable weight (worst
# typical fill; never undercharge). Loaded by file path so this script stays
# standalone (no grove_headless package import).
_SB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "models", "shipping_boxes.py")
_spec = _ilu.spec_from_file_location("grove_shipping_boxes", _SB_PATH)
shipping_boxes = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(shipping_boxes)

# Monotonicity guard — loaded by file path so this script stays standalone
# whether run directly or imported by tests via spec.
_MONO_PATH = os.path.join(os.path.dirname(__file__), "monotonicity.py")
_mspec = _ilu.spec_from_file_location("grove_rate_monotonicity", _MONO_PATH)
monotonicity = _ilu.module_from_spec(_mspec)
_mspec.loader.exec_module(monotonicity)

# Probe every GO-LIVE-SHIPPABLE catalog at its own representative billable
# weight (GOL-2199): bareroot (BOXES) at representative_billable_lb and
# potted/peat-and-bagged (POTTED_BOXES, GOL-2031) at
# potted_representative_billable_lb.
_CATALOGS = (
    (shipping_boxes.BOXES, shipping_boxes.representative_billable_lb),
    (shipping_boxes.POTTED_BOXES, shipping_boxes.potted_representative_billable_lb),
)
# Reference parcel geometry + declared weight (lb) per box id. The GraphQL
# request converts the weight to ounces (Pirate Ship's unit).
PARCELS = {
    box_id: {
        "length": box["length"],
        "width": box["width"],
        "height": box["height"],
        "weight_lb": weight_of(box_id),
    }
    for catalog, weight_of in _CATALOGS
    for box_id, box in catalog.items()
}
# Per-box packaging (box + consumables) replaces the old flat $3.50/tree.
PACKAGING = {box_id: box["packaging_usd"] for catalog, _ in _CATALOGS for box_id, box in catalog.items()}
BUFFER = 2.00
RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "data", "shipping_rates.json")
OUT_DIR = os.path.join(os.path.dirname(__file__), "out")

# ── Pirate Ship rate calculator ─────────────────────────────────────────────
PIRATESHIP_URL = "https://ship.pirateship.com/api/graphql?opname=RatesQuery"
# The `RatesQuery` operation, reverse-engineered from the Pirate Ship web app's
# compiled query (2026-09-09) and reduced to the fields the checker consumes.
# Only the mail classes we allowlist below are requested.
RATES_QUERY = (
    "query RatesQuery($originZip: String!, $originCity: String, $originRegionCode: String, "
    "$destinationZip: String, $isResidential: Boolean, $destinationCountryCode: String, "
    "$weight: Float, $dimensionX: Float, $dimensionY: Float, $dimensionZ: Float, "
    "$mailClassKeys: [String!]!, $packageTypeKeys: [String!]!, $showUpsRatesWhen2x7Selected: Boolean) { "
    "rates(originZip: $originZip, originCity: $originCity, originRegionCode: $originRegionCode, "
    "destinationZip: $destinationZip, isResidential: $isResidential, "
    "destinationCountryCode: $destinationCountryCode, weight: $weight, dimensionX: $dimensionX, "
    "dimensionY: $dimensionY, dimensionZ: $dimensionZ, mailClassKeys: $mailClassKeys, "
    "packageTypeKeys: $packageTypeKeys, showUpsRatesWhen2x7Selected: $showUpsRatesWhen2x7Selected) { "
    "title deliveryDescription mailClassKey carrier { carrierKey title } totalPrice } }"
)
MAIL_CLASS_KEYS = ["03", "93", "GroundAdvantage"]
PACKAGE_TYPE_KEYS = ["Parcel"]

# Ground services we will quote AND (in sub-project B) buy, in a carrier-neutral
# least-cost race. (carrier title, mailClassKey), matched exactly — this is an
# ALLOWLIST, never a global min() over every returned rate (GOL-1906). Pirate
# Ship's UPS service codes: "03" UPS Ground, "93" UPS Ground Saver; USPS
# "GroundAdvantage" Ground Advantage.
GROUND_SERVICE_ALLOWLIST = frozenset(
    {
        ("UPS", "03"),
        ("UPS", "93"),
        ("USPS", "GroundAdvantage"),
    }
)
# Human titles for the visibility report (independent of Pirate Ship's own
# per-rate title, which carries ®/™ marks).
SERVICE_TITLES = {
    ("UPS", "03"): "UPS Ground",
    ("UPS", "93"): "UPS Ground Saver",
    ("USPS", "GroundAdvantage"): "USPS Ground Advantage",
}
# Absolute transit ceiling (days), matching the label purchase's DORMANT ceiling
# (shippo_client.MAX_TRANSIT_DAYS["dormant"], GOL-1906): a rate that omits a
# parsable delivery date is NOT excluded (unknown transit != slow), and if NO
# allowlisted rate fits the ceiling the FASTEST known wins (ties break cheapest)
# so a slow week never strands an order unshippable.
MAX_TRANSIT_DAYS = 7

# "Estimated delivery [b]Wednesday 9/16 by 11:00 PM[/b] if shipped today"
_DELIVERY_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")


def parse_transit_days(delivery_description, probe_date):
    """Days from ``probe_date`` to Pirate Ship's estimated delivery date, or None.

    Parses the ``M/D`` in ``deliveryDescription``; the year is inferred as the
    next occurrence of that month/day on or after the probe date (so a late-
    December probe of an early-January date rolls to next year). An absent or
    unparsable date returns None — the caller treats unknown transit as
    acceptable, never as slow (mirrors the Shippo path's missing-ETA rule)."""
    if not delivery_description:
        return None
    m = _DELIVERY_DATE_RE.search(delivery_description)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        target = date(probe_date.year, month, day)
    except ValueError:
        return None
    if target < probe_date:
        try:
            target = date(probe_date.year + 1, month, day)
        except ValueError:
            return None
    return (target - probe_date).days


def _normalize_title(title):
    """Strip ®/™ marks and collapse whitespace from a Pirate Ship rate title."""
    return re.sub(r"\s+", " ", (title or "").replace("®", "").replace("™", "")).strip()


def normalize_rate(rate, probe_date):
    """A Pirate Ship rate object -> normalized dict, or None if unpriceable.

    ``{carrier, service, service_title, price, transit_days}`` where ``carrier``
    is the carrier title ("UPS"/"USPS"), ``service`` the mailClassKey, and
    ``price`` the total. Returns None when the total price is missing/unparsable."""
    try:
        price = float(rate.get("totalPrice"))
    except (TypeError, ValueError):
        return None
    return {
        "carrier": (rate.get("carrier") or {}).get("title") or "",
        "service": rate.get("mailClassKey"),
        "service_title": _normalize_title(rate.get("title")),
        "price": price,
        "transit_days": parse_transit_days(rate.get("deliveryDescription"), probe_date),
    }


def allowlisted_ground(rates, probe_date):
    """Normalized allowlisted ground rates from a RatesQuery ``rates`` list."""
    out = []
    for r in rates:
        n = normalize_rate(r, probe_date)
        if n and (n["carrier"], n["service"]) in GROUND_SERVICE_ALLOWLIST:
            out.append(n)
    return out


def select_cheapest_ground(rates, probe_date):
    """Cheapest allowlisted ground rate within the transit ceiling.

    Returns the winning normalized rate dict, or None when no allowlisted rate
    is present (the caller decides missing vs lapse). Unknown transit is not
    excluded; if none fit the ceiling, the fastest known wins (ties break
    cheapest). Same rules label purchase uses, so the published table matches
    what will actually be bought (GOL-1906)."""
    candidates = allowlisted_ground(rates, probe_date)
    if not candidates:
        return None
    within = [r for r in candidates if r["transit_days"] is None or r["transit_days"] <= MAX_TRANSIT_DAYS]
    if within:
        return min(within, key=lambda r: r["price"])
    return min(candidates, key=lambda r: (r["transit_days"], r["price"]))


def present_services(rates, probe_date):
    """Set of allowlisted (carrier, service) pairs Pirate Ship returned here.

    Visibility into which ground services reach the quote endpoint, independent
    of which one wins on price — a service at 0/N is silently absent (GOL-1906)."""
    return {(r["carrier"], r["service"]) for r in allowlisted_ground(rates, probe_date)}


def visibility_report(counts, total):
    """Human-readable per-service visibility summary for the probe run."""
    lines = ["Service visibility — allowlisted ground rates returned by Pirate Ship:"]
    for carrier, service in sorted(GROUND_SERVICE_ALLOWLIST):
        n = counts.get((carrier, service), 0)
        title = SERVICE_TITLES.get((carrier, service), f"{carrier} {service}")
        flag = "" if n else "  <-- NEVER RETURNED (dropped by Pirate Ship for this account/route?)"
        lines.append(f"  {carrier} {service} ({title}): {n}/{total} probe(s){flag}")
    return "\n".join(lines)


def target_rate(quote: float, box_id: str) -> int:
    return math.ceil(quote + PACKAGING[box_id] + BUFFER)


def rates_from_response(payload: dict) -> list:
    """Extract the ``rates`` list from a RatesQuery response.

    Raises on a GraphQL ``errors[]`` so the caller can log-and-skip the corner
    (spec A error handling); a well-formed response with no data returns []."""
    if payload.get("errors"):
        raise RuntimeError(f"pirateship graphql errors: {payload['errors']}")
    return ((payload.get("data") or {}).get("rates")) or []


def _request_rates(zip5: str, city: str, state: str, box_id: str, post=None) -> dict:
    """POST one RatesQuery for ``box_id`` to a single reference corner."""
    post = post or requests.post
    box = PARCELS[box_id]
    variables = {
        "originZip": ORIGIN["zip"],
        "originCity": ORIGIN["city"],
        "originRegionCode": ORIGIN["state"],
        "destinationZip": zip5,
        "isResidential": True,
        "destinationCountryCode": "US",
        "weight": float(box["weight_lb"]) * 16.0,  # Pirate Ship expects ounces
        "dimensionX": box["length"],
        "dimensionY": box["width"],
        "dimensionZ": box["height"],
        "mailClassKeys": MAIL_CLASS_KEYS,
        "packageTypeKeys": PACKAGE_TYPE_KEYS,
        "showUpsRatesWhen2x7Selected": True,
    }
    resp = post(
        PIRATESHIP_URL,
        json={"operationName": "RatesQuery", "query": RATES_QUERY, "variables": variables},
        timeout=30,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def quote_zone_box(zone: str, box_id: str, probe_date, post=None):
    """Winning ground rate for ``box_id`` at the WORST (max price) of the zone's
    reference corners, plus the union of allowlisted services seen across them.

    Publishing the per-box max keeps the single published rate an upper bound for
    every corner of a multi-state band (GOL-2128). A corner with an HTTP or
    GraphQL error, or no allowlisted rate, is skipped; the winner is ``None``
    only when NO corner yields an allowlisted ground rate (the caller's
    missing/lapse logic then applies). Returns ``(winner_or_None, present_set)``
    where ``winner`` is the normalized rate dict that set the published rate."""
    best = None
    present = set()
    for city, state, zip5 in REFERENCE_ZIPS[zone]:
        try:
            rates = rates_from_response(_request_rates(zip5, city, state, box_id, post=post))
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"pirateship error for {zone}/{box_id} @ {city},{state}: {exc}", file=sys.stderr)
            continue
        present |= present_services(rates, probe_date)
        winner = select_cheapest_ground(rates, probe_date)
        if winner is not None and (best is None or winner["price"] > best["price"]):
            best = winner
    return best, present


def compute_drift(current: dict, proposed: dict) -> list:
    """[(zone, tier, old, new)] where |old - new| >= 1.0. Reads the ``base`` of
    each proposed cell (schema-3 dict) or a bare number (test tables)."""
    drift = []
    for zone, boxes in proposed.items():
        for box_id, cell in boxes.items():
            new = cell["base"] if isinstance(cell, dict) else cell
            old = (current.get(zone, {}).get(box_id) or {}).get("base")
            if old is None or abs(float(old) - float(new)) >= 1.0:
                drift.append((zone, box_id, old, new))
    return sorted(drift)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", help="single canned RatesQuery response used for EVERY probe (testing)")
    ap.add_argument(
        "--fixture-dir",
        help="directory of pirateship_rates_<box_id>.json responses; used for every zone (offline dry-run)",
    )
    args = ap.parse_args(argv)

    probe_date = date.today()

    with open(RATES_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    # `_provisional` marks the launch-hypothesis placeholder table: an
    # all-missing result is the "not ready" state, not a lapse (GOL-1312).
    provisional = bool(raw.get("_provisional"))
    current = {k: v for k, v in raw.items() if not k.startswith("_")}

    proposed = {}
    missing = []
    seen_counts = {}
    probes = 0
    winners_log = []  # (zone, box_id, carrier, service, price)
    for zone in REFERENCE_ZIPS:
        proposed[zone] = {}
        for box_id in PARCELS:
            probes += 1
            if args.fixture or args.fixture_dir:
                path = args.fixture or os.path.join(args.fixture_dir, f"pirateship_rates_{box_id}.json")
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
                try:
                    rates = rates_from_response(payload)
                except RuntimeError as exc:
                    print(f"pirateship graphql errors for {zone}/{box_id}: {exc}", file=sys.stderr)
                    rates = []
                present = present_services(rates, probe_date)
                winner = select_cheapest_ground(rates, probe_date)
            else:
                winner, present = quote_zone_box(zone, box_id, probe_date)
            for key in present:
                seen_counts[key] = seen_counts.get(key, 0) + 1
            if winner is None:
                # No allowlisted ground rate for this probe. Record it and keep
                # going so we can tell a total absence from a partial gap.
                missing.append(f"{zone}/{box_id}")
                continue
            proposed[zone][box_id] = {
                "base": float(target_rate(winner["price"], box_id)),
                "carrier": winner["carrier"],
                "service": winner["service"],
                "service_title": winner["service_title"],
            }
            winners_log.append((zone, box_id, winner["carrier"], winner["service"], winner["price"]))

    # Surface which allowlisted ground services actually reached the quote
    # endpoint, then the winner (carrier/service) per cell so "which carrier set
    # this rate" is answered in the log. Emit before the missing/monotonicity
    # gates so the readout survives an early return.
    print(visibility_report(seen_counts, probes), file=sys.stderr)
    if winners_log:
        print("Winning service per cell (carrier service @ quoted price):", file=sys.stderr)
        for zone, box_id, carrier, service, price in winners_log:
            print(f"  {zone}/{box_id}: {carrier} {service} @ ${price:.2f}", file=sys.stderr)

    if missing:
        total = len(REFERENCE_ZIPS) * len(PARCELS)
        if len(missing) == total:
            if provisional or not current:
                # Zero ground rates across every probe AND no real published
                # rates to protect: the not-ready state. Skip cleanly (exit 0).
                print(
                    "::notice::Pirate Ship returned no allowlisted ground rate for any probe — "
                    "rate calculator not answering / not ready; rate-check skipped"
                )
                print("no ground rates available yet — skipped")
                return 0
            # Real published rates exist yet Pirate Ship now returns zero ground
            # rates for EVERY probe: the quote source has failed. Fail loudly so
            # a fossilized table gets investigated (GOL-1312).
            print(
                f"no ground rate for any of {total} probe(s) but "
                "shipping_rates.json holds real published rates — "
                "Pirate Ship quote source down? (see GOL-1312)",
                file=sys.stderr,
            )
            return 1
        # A PARTIAL gap (some boxes rated, some not) is a real quote problem and
        # must fail loudly, never silently drop a rate.
        print(
            f"no ground rate for {len(missing)} of {total} probe(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    # Guard before publishing: within a zone a bigger box must never be cheaper
    # (cart-gaming). Enforced WITHIN each catalog, never across them (bareroot
    # and potted are independent axes).
    violations = []
    for catalog, weight_of in _CATALOGS:
        box_order = monotonicity.ordered_boxes(catalog, weight_of)
        violations += monotonicity.find_violations(proposed, box_order, list(REFERENCE_ZIPS))
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
        "Per-box rates (Box Engine v2): ceil(Pirate Ship least-cost allowlisted "
        "ground [UPS Ground / UPS Ground Saver / USPS Ground Advantage] at the "
        "box's representative billable weight + per-box packaging + 2.00 buffer). "
        "Each cell records the winning carrier/service (schema 3); the Odoo loader "
        "reads `base` only. Carries BOTH shippable catalogs (GOL-2199): bareroot "
        "small/large and potted/peat-and-bagged p24x10x4/p24x10x6. "
        "Design: spec 2026-09-09-pirateship-fulfillment-design.md (GOL-2270).",
        "_schema": 3,
    }
    for zone in sorted(proposed):
        new_doc[zone] = {b: proposed[zone][b] for b in sorted(proposed[zone])}
    with open(RATES_PATH, "w", encoding="utf-8") as fh:
        json.dump(new_doc, fh, indent=2)
        fh.write("\n")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
