"""Pure planner-learning helpers: rule defaults, vocabularies, body validators.

Odoo-free so the root conftest can import this by file path and unit-test the
rule defaults and the public-route validators without an Odoo runtime
(GOL-3549, planner winter beta spec section B).

One source of truth:
  * ``PLANNER_RULE_DEFAULTS`` is seeded into ``grove.planner.rule`` (scope
    ``retail``) by ``data/planner_rules.xml`` and is the engine's fallback when
    the feed omits a key, so "the first feed changes nothing" (spec B.1). A DB
    test asserts the seeded rows equal this dict.
  * the validators mirror the strict-400 contract of the ``/grove/api/v1/cart``
    route: a malformed body is rejected before any ORM write, never 500s.
"""

# ── Rule keys and seeded values (spec B.1) ──────────────────────────────────
# Order and values are the shipped order-builder constants: 2, 1.5, 40, 4, 10,
# 10, 3, 2. Seeding from them means the first feed the storefront reads leaves
# the engine's scores unchanged.
PLANNER_RULE_KEYS = (
    "priority_weight",
    "years_weight",
    "marginal_penalty",
    "season_bonus",
    "wildlife_bonus",
    "nitrogen_bonus",
    "timing_fast",
    "timing_patient",
)

PLANNER_RULE_DEFAULTS = {
    "priority_weight": 2.0,
    "years_weight": 1.5,
    "marginal_penalty": 40.0,
    "season_bonus": 4.0,
    "wildlife_bonus": 10.0,
    "nitrogen_bonus": 10.0,
    "timing_fast": 3.0,
    "timing_patient": 2.0,
}

# ── Vocabularies ────────────────────────────────────────────────────────────
SIGNAL_KINDS = ("removed", "swapped", "added", "built", "saved")
# "built" (Build my order pressed) and "saved" (plan named) are plan-level
# events with no single product, so product_tmpl_id is optional only for them.
SIGNAL_KINDS_NO_PRODUCT = ("built", "saved")

# ZIP-unknown USDA bands the storefront buckets a plan into (spec B.2). A plain
# integer zone is also accepted and bucketed server-side.
ZONE_BANDS = ("3-5", "6-7", "8-9")

STRATA = ("overstory", "understory", "mushroom")

# The planner facts a tester can dispute (spec B.3). role/succession/stratum are
# the section-A fields; disputing them is valid even before A lands (the
# evidence row is just data).
EVIDENCE_FACTS = (
    "zone_min",
    "zone_max",
    "years_to_fruit",
    "tolerates_wet",
    "tolerates_poor_soil",
    "tolerates_shade",
    "pollination",
    "harvest_months",
    "role",
    "succession",
    "stratum",
)

# mushroom → fungal is a section-A layer value; Accept-as-candidate guards the
# write when the value is not yet in the grove_layer selection (A not landed).
STRATUM_TO_LAYER = {
    "overstory": "canopy",
    "understory": "understory",
    "mushroom": "fungal",
}

# Length caps mirror grove_headless/models/capture_leads.py so a public body
# can never balloon a Char/Text column.
MAX_NAME = 200
MAX_NOTE = 2000
MAX_LABEL = 100
MAX_URL = 2000
MAX_BROWSER_ID = 64
MAX_LINES = 60

# Per-browser-id sliding-window rate limit shared by signal/suggest/save
# (spec B.2: "60 requests per browser id per hour").
RATE_LIMIT_PER_HOUR = 60
RATE_LIMIT_WINDOW_SECONDS = 3600

# browser_id is client-supplied, so the per-browser cap alone can be defeated by
# rotating the id. A second, global ceiling across *all* browser ids on the same
# model in the window is a flood backstop that does not depend on any client
# value (GOL-3549 audit, finding 5). Sized well above plausible winter-beta
# traffic — a few dozen testers at the 60/hr per-browser cap — so it only bites
# an abusive rotation; the planner_learning_enabled kill switch is the hard stop.
RATE_LIMIT_GLOBAL_PER_HOUR = 1000


def _text(payload, key):
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _opt_int(payload, key):
    """Return ``(value, ok)`` — ``ok`` is False only when a present value is
    not coercible to int. A missing/blank value is ``(None, True)``."""
    raw = payload.get(key)
    if raw in (None, ""):
        return None, True
    try:
        return int(raw), True
    except (TypeError, ValueError):
        return None, False


def zone_band_for_zone(zone):
    """Bucket an integer USDA zone into one of ``ZONE_BANDS``; ``None`` if out of
    the 3–9 beta range or not an int."""
    if not isinstance(zone, int):
        return None
    if 3 <= zone <= 5:
        return "3-5"
    if 6 <= zone <= 7:
        return "6-7"
    if 8 <= zone <= 9:
        return "8-9"
    return None


def _clean_browser_id(payload):
    bid = _text(payload, "browser_id")
    if not bid or len(bid) > MAX_BROWSER_ID:
        return None
    return bid


def validate_signal(payload):
    """``(clean, None)`` or ``(None, error)`` for a POST /planner/signal body."""
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    kind = _text(payload, "kind")
    if kind not in SIGNAL_KINDS:
        return None, f"kind must be one of {', '.join(SIGNAL_KINDS)}"

    browser_id = _clean_browser_id(payload)
    if not browser_id:
        return None, "browser_id is required"

    product_id, ok = _opt_int(payload, "product_tmpl_id")
    if not ok:
        return None, "product_tmpl_id must be an integer"
    if product_id is None and kind not in SIGNAL_KINDS_NO_PRODUCT:
        return None, f"product_tmpl_id is required for kind {kind}"

    zone, ok = _opt_int(payload, "zone")
    if not ok:
        return None, "zone must be an integer"

    zone_band = _text(payload, "zone_band") or None
    if zone_band and zone_band not in ZONE_BANDS:
        return None, f"zone_band must be one of {', '.join(ZONE_BANDS)}"
    # Derive the band from a bare zone so a signal is always bucketable.
    if not zone_band:
        zone_band = zone_band_for_zone(zone)

    catalog_version, ok = _opt_int(payload, "catalog_version")
    if not ok:
        return None, "catalog_version must be an integer"

    clean = {
        "kind": kind,
        "browser_id": browser_id,
        "product_tmpl_id": product_id,
        "zone": zone,
        "zone_band": zone_band,
        "track": _text(payload, "track")[:MAX_LABEL] or False,
        "site": _text(payload, "site")[:MAX_LABEL] or False,
        "timing": _text(payload, "timing")[:MAX_LABEL] or False,
        "plan_url": _text(payload, "plan_url")[:MAX_URL] or False,
        "catalog_version": catalog_version,
    }
    return clean, None


def validate_suggestion(payload):
    """``(clean, None)`` or ``(None, error)`` for a POST /planner/suggest body."""
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    name = _text(payload, "name")
    if not name:
        return None, "name is required"
    stratum = _text(payload, "stratum")
    if stratum and stratum not in STRATA:
        return None, f"stratum must be one of {', '.join(STRATA)}"
    browser_id = _clean_browser_id(payload)
    if not browser_id:
        return None, "browser_id is required"
    clean = {
        "name": name[:MAX_NAME],
        "stratum": stratum or False,
        "note": _text(payload, "note")[:MAX_NOTE] or False,
        "browser_id": browser_id,
    }
    return clean, None


def validate_save(payload):
    """``(clean, None)`` or ``(None, error)`` for a POST /planner/save body.

    ``clean['lines']`` is a list of ``{product_tmpl_id, qty, role, stratum}``
    dicts; the controller resolves template ids to this company's catalog.
    """
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    name = _text(payload, "name")
    if not name:
        return None, "name is required"
    browser_id = _clean_browser_id(payload)
    if not browser_id:
        return None, "browser_id is required"

    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        return None, "lines must be a non-empty array"
    if len(raw_lines) > MAX_LINES:
        return None, f"lines may not exceed {MAX_LINES} entries"

    lines = []
    for entry in raw_lines:
        if not isinstance(entry, dict):
            return None, "each line must be an object"
        tmpl = entry.get("product_tmpl_id", entry.get("template_id"))
        try:
            tmpl = int(tmpl)
        except (TypeError, ValueError):
            return None, "each line needs an integer product_tmpl_id"
        qty_raw = entry.get("qty", 1)
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            return None, "line qty must be a number"
        if qty <= 0:
            return None, "line qty must be positive"
        lines.append(
            {
                "product_tmpl_id": tmpl,
                "qty": qty,
                "role": _text(entry, "role")[:MAX_LABEL] or False,
                "stratum": _text(entry, "stratum")[:MAX_LABEL] or False,
            }
        )

    zone, ok = _opt_int(payload, "zone")
    if not ok:
        return None, "zone must be an integer"

    clean = {
        "name": name[:MAX_NAME],
        "plan_url": _text(payload, "plan_url")[:MAX_URL] or False,
        "zone": zone,
        "track": _text(payload, "track")[:MAX_LABEL] or False,
        "browser_id": browser_id,
        "lines": lines,
    }
    return clean, None


def validate_dispute(payload):
    """``(clean, None)`` or ``(None, error)`` for a POST /planner/dispute body.

    Tester identity (``by``) is resolved from the token by the controller, not
    taken from the body.
    """
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    product_id, ok = _opt_int(payload, "product_tmpl_id")
    if not ok or product_id is None:
        return None, "product_tmpl_id is required"
    fact = _text(payload, "fact")
    if fact not in EVIDENCE_FACTS:
        return None, f"fact must be one of {', '.join(EVIDENCE_FACTS)}"
    zone, ok = _opt_int(payload, "zone")
    if not ok:
        return None, "zone must be an integer"
    clean = {
        "product_tmpl_id": product_id,
        "fact": fact,
        # Default True: a dispute asserts the fact is wrong. An explicit
        # contradicts=false records corroborating evidence.
        "contradicts": bool(payload.get("contradicts", True)),
        "note": _text(payload, "note")[:MAX_NOTE] or False,
        "zone": zone,
    }
    return clean, None


def validate_propose(payload):
    """``(clean, None)`` or ``(None, error)`` for a POST /planner/propose body.

    A tester proposes a new value for one rule key from the what-if sliders.
    """
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    key = _text(payload, "key")
    if key not in PLANNER_RULE_KEYS:
        return None, f"key must be one of {', '.join(PLANNER_RULE_KEYS)}"
    raw = payload.get("value")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "value must be a number"
    clean = {"key": key, "value": value}
    return clean, None
