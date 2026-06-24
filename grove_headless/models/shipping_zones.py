"""12-zone shipping rate engine for the Grove headless checkout (GOL-15).

STATUS — engine COMPLETE, zone DATA BLOCKED
===========================================
The rate-computation plumbing below is finished and unit-tested. What is NOT
yet here is the actual 12-zone rate table: it lives in Josh's Obsidian vault
("wrote out the full 12-zone shipping pricing doc", daily journal 2026-06-23)
and is not reachable from the agent runtime (not in Notion, Drive, or the
read-only /opt/vault mount). See ``docs/shipping-zones.md`` for the fill-in
schema and the open questions for Rick.

Fail-safe by design: while ``ZONE_BY_STATE`` / ``ZONE_RATES`` are empty,
``compute_shipping_rate`` returns ``None`` for every address, so the checkout
adds NO shipping line and current behaviour is preserved — we never ship a
wrong or guessed charge. The moment the two tables are filled from the doc the
feature goes live with no further code changes.

The engine is deliberately a pure-Python module with no Odoo imports so it can
be unit-tested without a database (see ``tests/test_shipping_zones.py``) and so
the rate table is one obvious source of truth a non-engineer can edit.
"""

# ── Destination universe ────────────────────────────────────────────────────
# Every US destination we expect to quote. Used by the test to assert that the
# finished table covers every state exactly once (no gaps, no double-assigns).
# DC + the shippable territories are included; trim per the doc if the business
# does not ship to a given territory.
US_STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
    # Territories — keep or drop per the doc.
    "PR", "VI", "GU", "AS", "MP",
)

# The 12 zones. Ids are stable keys used by ZONE_BY_STATE and ZONE_RATES;
# labels are display-only and may be renamed to match the doc's naming.
ZONE_IDS: tuple[str, ...] = tuple(f"zone_{i}" for i in range(1, 13))

ZONE_LABELS: dict[str, str] = {zid: f"Zone {zid.split('_')[1]}" for zid in ZONE_IDS}

# ── BLOCKED DATA — fill from Josh's 12-zone doc ─────────────────────────────
# state code -> zone id. Empty until the doc lands. Example once filled:
#   ZONE_BY_STATE = {"WV": "zone_1", "OH": "zone_1", "PA": "zone_2", ...}
ZONE_BY_STATE: dict[str, str] = {}

# zone id -> rate rule. Empty until the doc lands. The rule shape supports both
# a flat per-zone charge and (optionally) weight and free-shipping-threshold
# rules, so the doc's exact pricing can be expressed without changing code:
#
#   "zone_1": {
#       "base": 8.00,         # flat charge applied for any order to this zone
#       "per_lb": 0.0,        # optional surcharge per pound of order weight
#       "free_over": 75.0,    # optional: free shipping when subtotal >= this
#   }
#
# Only "base" is required; "per_lb" and "free_over" default to off.
ZONE_RATES: dict[str, dict] = {}


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
    *,
    weight: float = 0.0,
    subtotal: float = 0.0,
) -> float | None:
    """Shipping charge for a destination ``state``, or ``None`` if no zone is
    configured for it.

    Returns ``None`` (not ``0.0``) for an unmapped/unconfigured destination so
    the caller can distinguish "no shipping configured — add no line" from a
    genuine ``0.0`` charge (a free-shipping threshold met, or a $0 zone).

    ``weight`` (lbs) and ``subtotal`` (order untaxed total) are accepted now so
    the call site is already wired for the doc's weight / free-over rules; with
    a flat-only table they are simply ignored.
    """
    zone = zone_for_state(state)
    if not zone:
        return None
    rule = ZONE_RATES.get(zone)
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
