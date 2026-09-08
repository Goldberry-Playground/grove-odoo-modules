"""5-zone per-box shipping rate engine for the Grove headless checkout.

v2 (Box Engine, GOL-15 successor): shipping is bareroot-only and priced PER
PACKED BOX instead of per tree. The box catalog + packing algorithm live in
``shipping_boxes.py``; this module owns the destination zones, the compliance
gate, and the money. Potted products have no ship rates by design — potted is
farm pickup only.

Rates are loaded from ``data/shipping_rates.json`` at startup, keyed
``zone -> box_id -> {"base": usd}``. The file is maintained by the daily
rate-checker (``scripts/rate_check/rate_check.py``), which rewrites it
wholesale from live Shippo quotes — hand-editing extra keys there while the
checker is active is not safe; they will be dropped on the next rates PR.
Design is documented in the vault wiki at ``Software/Grove Shipping``.

Fail-safe by design: ``compute_order_shipping`` returns ``None`` for any
address outside the 31-state green list, any cart containing a potted line,
and any cart the packer cannot plan — the checkout then adds NO shipping line
(and the checkout endpoint blocks with an explicit message via
``unshippable_reason``). We never emit a wrong or guessed charge.

The engine is deliberately a pure-Python module with no Odoo imports so it can
be unit-tested without a database (see ``tests/test_shipping_zones.py``) and so
the rate table is one obvious source of truth a non-engineer can edit.
"""

import json
import os
from datetime import date as _date

try:
    from . import shipping_boxes, shipping_calendar
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu

    def _load_sibling(name):
        path = os.path.join(os.path.dirname(__file__), f"{name}.py")
        spec = _ilu.spec_from_file_location(f"grove_{name}", path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    shipping_boxes = _load_sibling("shipping_boxes")
    shipping_calendar = _load_sibling("shipping_calendar")

# ── Destination universe ────────────────────────────────────────────────────
# Every US destination we expect to quote. Used by the test to assert that the
# finished table covers every state exactly once (no gaps, no double-assigns).
# DC + the shippable territories are included; trim per the doc if the business
# does not ship to a given territory.
US_STATES: tuple[str, ...] = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    # Territories — keep or drop per the doc.
    "PR",
    "VI",
    "GU",
    "AS",
    "MP",
)

RATE_ZONE_IDS: tuple[str, ...] = tuple(f"zone_{i}" for i in range(1, 8))

# Product tiers survive v2 as the shippability gate. GOL-2199 potted go-live
# (CEO directive 2026-09-08): potted / peat-and-bagged now SHIPS on its own
# engine (POTTED_BOXES, packed by unit count via pack_potted — GOL-2031),
# alongside bareroot (packed by length class via pack_order). DEFAULT_TIER
# stays potted: a mistagged product now ships at POTTED rates, the pricier
# catalog at every weight point (12/22 lb potted vs 7/14 lb dormant bareroot),
# so the never-undercharge property is preserved.
# Fail-safe until the rate feed carries potted rows: pack_potted returns None
# when no potted box has a zone rate, compute_order_shipping propagates the
# None, and the checkout's $0-shipping circuit breaker (GOL-1036) blocks the
# order with the friendly retry message — potted ship-to carts cannot be
# under-billed in the window between this flip and the rate-checker's first
# potted-inclusive table.
TIERS: tuple[str, ...] = ("bareroot", "potted")
SHIPPABLE_TIERS: frozenset[str] = frozenset({"bareroot", "potted"})
DEFAULT_TIER = "potted"

# Box-geometry authority lives in shipping_boxes; re-exported for callers
# (rate-checker reference parcels, packing docs) that pinned it here in v1.
MAX_BOX_LONGEST_SIDE_IN = shipping_boxes.MAX_BOX_LONGEST_SIDE_IN

_RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "shipping_rates.json")


def _load_rates() -> dict:
    try:
        with open(_RATES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


ZONE_RATES: dict[str, dict] = _load_rates()

GREEN_STATES: frozenset[str] = frozenset(
    {
        "AL",
        "AR",
        "CT",
        "DC",
        "DE",
        "GA",
        "IA",
        "IL",
        "IN",
        "KY",
        "LA",
        "MA",
        "MD",
        "ME",
        "MI",
        "MN",
        "MO",
        "MS",
        "NC",
        "NH",
        "NJ",
        "NY",
        "OH",
        "PA",
        "RI",
        "SC",
        "TN",
        "VA",
        "VT",
        "WI",
        "WV",
    }
)

# state code -> zone id. Example: {"WV": "zone_1", "OH": "zone_1", "PA": "zone_2", ...}
ZONE_BY_STATE: dict[str, str] = {
    # zone_1 — nearest (UPS ~2-4 from 26651)
    "WV": "zone_1",
    "VA": "zone_1",
    "KY": "zone_1",
    "NC": "zone_1",
    "DE": "zone_1",
    "DC": "zone_1",  # GOL-2128 probe: DC corner (Washington 20001) <= zone_1 for every box
    # zone_2
    "MD": "zone_2",
    "PA": "zone_2",
    "OH": "zone_2",
    "IN": "zone_2",
    "NJ": "zone_2",
    "NY": "zone_2",
    # zone_3
    "IL": "zone_3",
    "MI": "zone_3",
    "CT": "zone_3",
    "RI": "zone_3",
    # zone_4
    "WI": "zone_4",
    "MN": "zone_4",
    "MA": "zone_4",
    "VT": "zone_4",
    "NH": "zone_4",
    # zone_5 — farthest priced band (originally Maine only). GOL-2128 opened a
    # ratified south/mid-continent tranche after a live Shippo probe; GOL-2238
    # then RE-PROBED that tranche against the two-SKU (small/large) catalog
    # (2026-09-08, origin 26651, cheapest-of-{UPS Ground, USPS Ground Advantage}
    # — the SAME selector label purchase uses; for each state quote its worst
    # (farthest) corner for every box, target = ceil(quote+pkg+2)). GOL-2128
    # had lumped the whole tranche at zone_5 because no cheaper band DOMINATED
    # their targets; the re-probe shows only GA/SC/AL/MS/LA genuinely belong at
    # zone_5 (39/43) — GA/SC sit just below (37/41), AL/MS/LA exactly at the
    # zone_5 corner. TN, AR, MO and IA quote materially cheaper and now get
    # their own real distance bands (zone_6 / zone_7 below), so they stop paying
    # a Maine-tier overcharge. Every published per-zone rate still dominates its
    # members' worst-corner targets for every box, so no state is ever
    # undercharged; the daily rate-checker probes MULTIPLE corners per zone and
    # publishes the max (see scripts/rate_check REFERENCE_ZIPS).
    #
    # The ratified FAR states (OK, KS, NE, SD, ND, TX, NM, AZ) are still NOT in
    # GREEN_STATES: the re-probe confirms they clear on cost (all ≤ zone_7's
    # 30/38 except TX at 41/55), but opening them adds new NPB plant-compliance
    # surface (e.g. Carya / pecan weevil in AZ/NM) that the per-product carve-out
    # gate must cover first — tracked as the GOL-2238 far-states follow-up. FL
    # stays out on the same compliance track (GOL-2132).
    "GA": "zone_5",
    "AL": "zone_5",
    "SC": "zone_5",
    "MS": "zone_5",
    "LA": "zone_5",
    "ME": "zone_5",
    # zone_6 — mid-continent band. GOL-2238 re-probe (two-SKU catalog): AR
    # (Texarkana), MO (Joplin) and IA (Sioux City) each target 23/28
    # (small/large); zone_6's 23/28 published rate is their exact worst-corner
    # upper bound, cutting the ~$15/box zone_5 overcharge GOL-2128 had assigned.
    "AR": "zone_6",
    "MO": "zone_6",
    "IA": "zone_6",
    # zone_7 — near-plains band. GOL-2238 re-probe: TN (Memphis) targets 29/32,
    # cheaper than the Gulf zone_5 corner but pricier than the zone_6 states, so
    # it gets its own band (published 29/32 = its worst-corner upper bound). Room
    # for the ratified far plains states (OK/KS/AZ/NM ≤ 30/38) once compliance
    # clears — see the far-states follow-up note above.
    "TN": "zone_7",
}

assert set(ZONE_BY_STATE) == GREEN_STATES


def rate_feed(calendar_override=None, today=None) -> dict:
    """Read-only snapshot of the live rate table + compliance zone map (GOL-952).

    Returns exactly the in-memory tables ``compute_order_shipping`` prices
    orders with, so the storefront estimator (grove-sites ``resolveRateTable``)
    can override its bundled snapshot and never drift from what checkout will
    actually charge. v2 shape:

        {
          "schema": 2,
          "zones": {"zone_1": {"small": {"base": 22.0}, "large": {...}}, ...},
          "zone_by_state": {"WV": "zone_1", ...},
          "green_states": ["CT", "DE", ...],
          "packing": {
            "boxes": {"small": {"length": 24, "width": 6, "height": 4,
                                "capacity": {"dormant": 5, "leafed": 5}}, ...},
            "length_classes": [16, 20],
            "modes": ["dormant", "leafed"],
          },
          "calendar": {   # GOL-1172: per-USDA-zone twice-yearly ship calendar
            "preorder_open": {"fall": [8, 15], "spring": [11, 1]},
            "leafed_window": [[5, 1], [10, 15]],
            "fulfillment_days": [5, 10],
            "zones": {"6": {"fall": [[9, 15], [10, 30]], "spring": [[1, 1], [5, 5]]}, ...},
            "resolved": {   # GOL-1386: per-zone RESOLVED mode at request time
              "6": {"mode": "peat-and-bagged", "season": None,
                    "ship_timing": "Ships in 5-10 business days",
                    "ship_window": None, "order_deadline": None,
                    "fulfillment_days": [5, 10]}, ...},
          },
        }

    ``zones`` mirrors ``data/shipping_rates.json`` (minus the ``_``-prefixed
    keys, already stripped at load). ``zone_by_state`` is the authoritative
    31-state green list -> zone map — the compliance gate the frontend must
    stay in lockstep with. ``packing`` carries the box catalog + capacities so
    the frontend can mirror ``pack_order`` exactly. ``calendar`` is the annual,
    admin-editable shipping calendar keyed to USDA hardiness zone (NOT the
    zone_1..zone_5 distance zones above) — replaces the old single global
    ``dormant_window`` so the frontend can resolve (date, usdaZone) -> one of
    ``bareroot-preorder`` | ``bareroot-in-window`` | ``peat-and-bagged``.

    ``calendar.resolved`` (GOL-1386) is that resolution done SERVER-SIDE: for
    every USDA zone 2-10 it carries the mode/ship_timing/ship_window/
    order_deadline ``resolve_fulfillment`` computes for ``today``, so the
    frontend reads the shopper's exact zone verbatim instead of re-deriving the
    backend state machine (the raw ``calendar`` block stays as the client's
    degraded-feed fallback). See ``shipping_calendar.serialize_resolved``.

    ``calendar_override`` is the parsed ``grove_headless.shipping_calendar``
    system parameter (or None); the controller reads the DB and passes it in so
    this function stays DB-free (no Shippo call, no DB read). ``today`` is the
    server-local date the resolved block is computed for — the controller passes
    ``date.today()``; tests inject a fixed date. It defaults to ``date.today()``
    when omitted so callers that only want the rate table need not supply it. The
    returned dict is a fresh deep copy so a caller can't mutate the engine's
    tables.
    """
    calendar = shipping_calendar.merge_calendar_override(calendar_override)
    if today is None:
        today = _date.today()
    calendar_block = shipping_calendar.serialize_calendar(calendar)
    calendar_block["resolved"] = shipping_calendar.serialize_resolved(calendar, today)
    return {
        "schema": 2,
        "zones": {zone: {box: dict(rule) for box, rule in boxes.items()} for zone, boxes in ZONE_RATES.items()},
        "zone_by_state": dict(ZONE_BY_STATE),
        "green_states": sorted(ZONE_BY_STATE),
        "packing": {
            "boxes": {
                box_id: {
                    "length": b["length"],
                    "width": b["width"],
                    "height": b["height"],
                    "capacity": dict(b["capacity"]),
                }
                for box_id, b in shipping_boxes.BOXES.items()
            },
            "length_classes": list(shipping_boxes.LENGTH_CLASSES),
            "modes": list(shipping_boxes.MODES),
        },
        "calendar": calendar_block,
    }


def is_configured() -> bool:
    """True when both the zone map and rate table are populated.

    ``ZONE_BY_STATE`` and ``ZONE_RATES`` are loaded at startup from
    ``data/shipping_rates.json`` (maintained by the daily rate-checker).
    Returns False only when the JSON file is missing or empty.
    """
    return bool(ZONE_BY_STATE) and bool(ZONE_RATES)


# Full state/territory name -> USPS code, for every entry in US_STATES. The
# checkout receives a client-supplied ship-to state that may arrive as a full
# name ("Ohio", "West Virginia") instead of a 2-letter code, in any case. Both
# the shipping-zone lookup and the destination-tax decision must canonicalize
# it identically, or an order gets silently under-billed for shipping (a green
# state that fails the code lookup adds NO shipping line) or wrongly taxed
# (a WV-only sales tax leaking onto an out-of-state destination). Keeping the
# map here — beside US_STATES — keeps the one destination universe authoritative.
_STATE_NAME_TO_CODE: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    "PUERTO RICO": "PR",
    "VIRGIN ISLANDS": "VI",
    "U.S. VIRGIN ISLANDS": "VI",
    "US VIRGIN ISLANDS": "VI",
    "GUAM": "GU",
    "AMERICAN SAMOA": "AS",
    "NORTHERN MARIANA ISLANDS": "MP",
}

_VALID_STATE_CODES: frozenset[str] = frozenset(US_STATES)


def canonical_state_code(value: str) -> str | None:
    """Canonicalize a US state/territory identifier to its 2-letter USPS code.

    Accepts a 2-letter code in any case (``"oh"``, ``"OH"``) or a full state
    name in any case with arbitrary internal whitespace (``"Ohio"``,
    ``"west  virginia"``). Returns the uppercase 2-letter code, or ``None`` when
    the value is empty or unrecognized.

    This is the single normalization the shipping-zone lookup and the
    destination-tax decision both route through, so a ship-to state supplied in
    a non-code format can never be silently mis-routed into a dropped shipping
    line or a wrong tax.
    """
    if not value:
        return None
    token = " ".join(value.strip().upper().split())
    if token in _VALID_STATE_CODES:
        return token
    return _STATE_NAME_TO_CODE.get(token)


def zone_for_state(state: str) -> str | None:
    """Return the zone id a destination state maps to, or None if unmapped."""
    code = canonical_state_code(state)
    if code is None:
        return None
    return ZONE_BY_STATE.get(code)


def box_rate(state: str, box_id: str) -> float | None:
    """Committed charge for shipping one `box_id` to `state`, or None.

    None (not 0.0) means "no rate configured — this box cannot ship there".
    """
    zone = zone_for_state(state)
    if not zone:
        return None
    rule = (ZONE_RATES.get(zone) or {}).get(box_id)
    if not rule:
        return None
    return round(float(rule.get("base", 0.0)), 2)


def single_tree_rate(
    state: str, length_class: int = shipping_boxes.DEFAULT_LENGTH, mode: str = "leafed"
) -> float | None:
    """Cheapest way to ship exactly one bareroot tree — the product-card
    "shipping from $X" estimate. None when unpriceable."""
    plan = shipping_boxes.pack_order([(length_class, 1)], mode, lambda b: box_rate(state, b))
    if not plan:
        return None
    return round(sum(r for r in (box_rate(state, pb.box_id) for pb in plan)), 2)


def single_potted_rate(state: str) -> float | None:
    """Cheapest way to ship exactly one potted / peat-and-bagged unit — the
    product-card "shipping from $X" estimate for the potted catalog
    (GOL-2199 go-live). None when unpriceable, including the window before the
    rate-checker's first potted-inclusive table publishes."""
    plan = shipping_boxes.pack_potted(1, lambda b: box_rate(state, b))
    if not plan:
        return None
    return round(sum(box_rate(state, pb.box_id) for pb in plan), 2)


def unshippable_reason(items: list[tuple[str, int, float]]) -> str | None:
    """Explicit human-readable reason a cart cannot ship, or None if it can

    have a shipping charge computed (destination permitting). Used by the
    checkout endpoint to BLOCK with a kind message instead of silently
    creating an un-shipped order (2026-07-20 decision, extended to potted).
    items: (tier, length_class, qty).
    """
    # GOL-2199: every current tier is shippable (bareroot dormant-window, potted
    # peat-and-bagged), so this loop only fires for a FUTURE tier added to TIERS
    # without joining SHIPPABLE_TIERS — kept as the fail-closed seam the checkout
    # block message rides on, exactly as it gated potted pre-go-live.
    for tier, _length, qty in items:
        if float(qty) <= 0:
            continue
        tier_key = tier if tier in TIERS else DEFAULT_TIER
        if tier_key not in SHIPPABLE_TIERS:
            return (
                f"{tier_key.capitalize()} trees are available for farm pickup only — "
                "remove them from the cart to ship, or choose pickup for the whole order."
            )
    return None


def pack_for_state(state: str, items: list[tuple[str, int, float]], mode: str):
    """Pack a cart for a destination: list of PackedBox, or None (fail-safe).

    items: (tier, length_class, qty) per order line. Any non-shippable tier,
    unknown state, or unpackable line -> None. Shared by the checkout charge
    and the Shippo label plan so the boxes bought are the boxes priced.
    """
    if zone_for_state(state) is None:
        return None
    if unshippable_reason(items) is not None:
        return None

    def cost_of(box_id):
        return box_rate(state, box_id)

    # GOL-2199: split the cart across the two independent packing engines.
    # Bareroot packs by tree-length class + season mode (Box Engine v2);
    # potted / peat-and-bagged packs by unit count (GOL-2031). Each side is
    # fail-safe on its own (None when it cannot pack AND price), and either
    # side failing fails the whole plan — a half-priced order must never add
    # a shipping line, same contract as before the split.
    bareroot_items: list[tuple[int, float]] = []
    potted_units = 0.0
    for tier, length, qty in items:
        if float(qty) <= 0:
            continue
        tier_key = tier if tier in TIERS else DEFAULT_TIER
        if tier_key == "potted":
            potted_units += qty
        else:
            bareroot_items.append((int(length), qty))
    plan: list = []
    if bareroot_items:
        bareroot_plan = shipping_boxes.pack_order(bareroot_items, mode, cost_of)
        if bareroot_plan is None:
            return None
        plan.extend(bareroot_plan)
    if potted_units:
        potted_plan = shipping_boxes.pack_potted(potted_units, cost_of)
        if potted_plan is None:
            return None
        plan.extend(potted_plan)
    return plan


def compute_order_shipping(state: str, items: list[tuple[str, int, float]], mode: str) -> float | None:
    """Total committed shipping for an order: sum of packed-box zone rates.

    Fail-safe: if the cart can't be packed and priced end-to-end (no zone,
    potted line, missing box rate), return None so the caller adds no
    shipping line at all — we never ship a partial/guessed charge.
    """
    plan = pack_for_state(state, items, mode)
    if plan is None:
        return None
    if not plan:
        return None  # empty cart -> no charge line
    total = 0.0
    for pb in plan:
        rate = box_rate(state, pb.box_id)
        if rate is None:  # pragma: no cover — packer only picks rated boxes
            return None
        total += rate
    return round(total, 2)
