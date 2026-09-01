#!/usr/bin/env python3
"""Monotonicity guard for the Box Engine v2 per-box rate table.

The daily rate-checker (``rate_check.py``) rewrites
``grove_headless/data/shipping_rates.json`` wholesale from live Shippo quotes.
A single bad quote (or a hand-edit) can produce a table where a *bigger box*
is somehow cheaper — which makes the packer's "fewer, bigger boxes for bulk"
outcomes non-intuitive and lets a customer game the cart into an undercharge by
splitting into more/heavier boxes. This module is the guard against that.

It enforces two properties on the table, reading the table exactly as the
checkout prices it (source of truth = the zone engine's ``rate_feed()``):

1. **Coverage** — every one of the 6 catalog boxes has a rate in every one of
   the 4 zones.
2. **Box monotonicity** — within a zone, a heavier box (by representative
   billable weight, the same ordering the engine's own test asserts) is never
   cheaper than a lighter one.

**Why no cross-zone monotonicity.** A prior version also asserted "for a fixed
box, a farther zone (zone_1 -> zone_4) is never cheaper than a nearer one." That
invariant is FALSE against real USPS Ground Advantage from Summersville WV 26651:
the state-distance bands don't perfectly track USPS's own zone cost ordering at
every box weight (e.g. a small parcel to zone_4/MN can quote under the same box
to zone_3/ME). It was only ever a proxy for the
thing we actually care about — *never undercharging* — and it blocked correct
tables while protecting nothing real. That guarantee is now held upstream by
``rate_check`` quoting each zone's **worst-case (priciest) destination in the
band** so every published per-zone rate is an upper bound for the whole band
(GOL-1495, board-approved 2026-08-14). Only box monotonicity — the invariant
that stops cart-gaming within a single lane — is enforced here.

Two entry points:

* ``find_violations(table, boxes_light_to_heavy, zones)`` — pure, no imports,
  returns a list of human-readable violation strings (empty == sound).
  ``rate_check`` calls this on the *proposed* table before writing, so a
  non-monotone table is never published; tests call it on fixtures. ``zones``
  is just the set of zones to check (order no longer carries meaning).
* ``main()`` — CLI. Checks the *live committed* table (via the zone engine
  ``rate_feed()``) so it is runnable in CI and day-of ops with no Shippo key.
  Exit 0 = sound, 2 = violations found, 1 = could not load the table.

Design: vault wiki/Software/Grove Shipping (Box Engine v2, 2026-07-31).
"""

import argparse
import importlib.util as _ilu
import os
import sys


def _base(rule) -> float | None:
    """Extract the base USD from a rate cell.

    Accepts either the on-disk shape ``{"base": 18.0}`` or a bare number
    (the shape ``rate_check`` builds its *proposed* table in), so the guard
    can run against both without the caller reshaping first. Returns None for
    a missing or unparseable cell.
    """
    if rule is None:
        return None
    if isinstance(rule, dict):
        rule = rule.get("base")
    if rule is None:
        return None
    try:
        return float(rule)
    except (TypeError, ValueError):
        return None


def ordered_boxes(box_ids, weight_of) -> list:
    """Box ids sorted lightest -> heaviest by ``weight_of(box_id)``.

    Ties break by box id so the ordering is fully deterministic — the same
    ordering ``test_shipping_zones`` uses to assert the table is monotone.
    """
    return sorted(box_ids, key=lambda b: (weight_of(b), b))


def find_violations(table: dict, boxes_light_to_heavy, zones) -> list:
    """Return a list of coverage/box-monotonicity violations for ``table``.

    ``table``: ``{zone: {box_id: {"base": usd}}}`` (``_``-prefixed keys must
    already be stripped) — or the same with bare-number cells. An empty list
    means the table is sound. Missing cells are reported as coverage gaps and
    then skipped by the box-monotonicity pass (a gap is not also a "cheaper"
    finding), so the output stays one problem per line. ``zones`` is the set of
    zones to check; its order is not significant (cross-zone monotonicity is
    intentionally not enforced — see the module docstring, GOL-1495).
    """
    violations: list[str] = []

    def base(zone, box):
        return _base((table.get(zone) or {}).get(box))

    # 1. Coverage — every box priced in every zone.
    for zone in zones:
        for box in boxes_light_to_heavy:
            if base(zone, box) is None:
                violations.append(f"coverage: {zone} has no rate for box {box}")

    # 2. Box monotonicity — within a zone, heavier is never cheaper. This is the
    # only cart-gaming vector left once each zone quotes its band's worst case.
    for zone in zones:
        for lighter, heavier in zip(boxes_light_to_heavy, boxes_light_to_heavy[1:]):
            lo, hi = base(zone, lighter), base(zone, heavier)
            if lo is None or hi is None:
                continue
            if hi < lo:
                violations.append(
                    f"box order: {zone} heavier box {heavier} (${hi:g}) cheaper than lighter {lighter} (${lo:g})"
                )

    return violations


def _load_zone_engine():
    """Load ``shipping_zones`` (the SoR) by file path — standalone, no Odoo."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "models", "shipping_zones.py")
    spec = _ilu.spec_from_file_location("grove_shipping_zones", path)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    argparse.ArgumentParser(description="Check the live per-box shipping rate table is monotone.").parse_args()

    sz = _load_zone_engine()
    # Read the table exactly as checkout prices it: rate_feed() is the same
    # snapshot compute_order_shipping charges from and the storefront mirrors.
    feed = sz.rate_feed()
    table = feed["zones"]
    zones = [z for z in sz.RATE_ZONE_IDS if z in table]
    boxes = ordered_boxes(sz.shipping_boxes.BOXES, sz.shipping_boxes.representative_billable_lb)

    if not table:
        print("rate table is empty (not configured) — nothing to check")
        return 0

    violations = find_violations(table, boxes, zones)
    if violations:
        print(f"MONOTONICITY VIOLATIONS ({len(violations)}):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    print(f"monotonicity OK: {len(boxes)} boxes x {len(zones)} zones, coverage complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
