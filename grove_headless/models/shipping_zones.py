"""5-zone tiered shipping rate engine for the Grove headless checkout (GOL-15).

STATUS — engine COMPLETE, rates in JSON
========================================
The rate-computation plumbing below is finished and unit-tested. Rates are
loaded from ``data/shipping_rates.json`` with a two-tier structure (bareroot and
potted products). Design is documented in the vault wiki at ``Software/Grove Shipping``.

Fail-safe by design: while ``ZONE_BY_STATE`` / ``ZONE_RATES`` are empty,
``compute_shipping_rate`` returns ``None`` for every address, so the checkout
adds NO shipping line and current behaviour is preserved — we never ship a
wrong or guessed charge.

The engine is deliberately a pure-Python module with no Odoo imports so it can
be unit-tested without a database (see ``tests/test_shipping_zones.py``) and so
the rate table is one obvious source of truth a non-engineer can edit.
"""

import json
import os

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

RATE_ZONE_IDS: tuple[str, ...] = tuple(f"zone_{i}" for i in range(1, 6))
TIERS: tuple[str, ...] = ("bareroot", "potted")
DEFAULT_TIER = "potted"  # never undercharge an untagged product

# Box rule (UPS additional-handling triggers at > 48.0"): documented constants
# the packing docs and the rate-checker's reference parcels share.
MAX_BOX_LONGEST_SIDE_IN = 48.0
PARCEL_PROFILES = {
    "bareroot": {"length": 48, "width": 6, "height": 6, "weight_lb": 4},
    "potted": {"length": 30, "width": 16, "height": 16, "weight_lb": 25},
}

_RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "shipping_rates.json")


def _load_rates() -> dict:
    try:
        with open(_RATES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


ZONE_RATES: dict[str, dict] = _load_rates()

# state code -> zone id. Example: {"WV": "zone_1", "OH": "zone_1", "PA": "zone_2", ...}
ZONE_BY_STATE: dict[str, str] = {}


def is_configured() -> bool:
    """True once both blocked tables have been populated from the doc."""
    return bool(ZONE_BY_STATE) and bool(ZONE_RATES)


def _normalize_state(state: str) -> str:
    return (state or "").strip().upper()


def zone_for_state(state: str) -> str | None:
    """Return the zone id a destination state maps to, or None if unmapped."""
    return ZONE_BY_STATE.get(_normalize_state(state))


def compute_shipping_rate(
    state: str,
    tier: str = DEFAULT_TIER,
    *,
    weight: float = 0.0,
    subtotal: float = 0.0,
) -> float | None:
    """Per-tree shipping charge for one unit of `tier` to `state`, or None.

    None (not 0.0) means "no rate configured — add no shipping line".
    Unknown tier strings price as DEFAULT_TIER (potted) so a mistagged
    product can never be undercharged.
    """
    zone = zone_for_state(state)
    if not zone:
        return None
    tier_key = tier if tier in TIERS else DEFAULT_TIER
    rule = (ZONE_RATES.get(zone) or {}).get(tier_key)
    if not rule:
        return None

    free_over = rule.get("free_over")
    if free_over is not None and subtotal >= float(free_over):
        return 0.0

    rate = float(rule.get("base", 0.0))
    per_lb = rule.get("per_lb")
    if per_lb:
        rate += float(per_lb) * max(0.0, float(weight))
    return round(rate, 2)
