#!/usr/bin/env python3
"""One-time parser that fills the planner facts from the existing prose facts.

Context (GOL-3378, order-builder spec 2026-10-14 section A): the order-builder
planner scores on a typed subset of the growing facts — years to first crop,
pollination partners, harvest months, site tolerances and a wildlife rating —
that the display Char fields (grove_years_to_fruit, grove_pollination,
grove_harvest_season, grove_soil, grove_watering, grove_sun, grove_wildlife) hold
only as prose. This script walks the real plant templates and derives the typed
fields from that prose with the small rule-set below, then writes them through
the normal ``write()`` path (so chatter shows the change) and clears the
``grove_facts_reviewed`` sign-off on every template it touches — the existing
"machine writes clear the sign-off" rule, so the review pass (spec section E)
catches them.

Same shape as ``backfill_listing_content.py``: XML-RPC, ``DRY_RUN=1`` by default,
QA first. In dry-run it prints a before/after table for Josh to confirm; only
``DRY_RUN=0`` actually writes.

The parse rules (spec section A) — each is a pure function below so the test
suite can unit-case it:

- ``grove_years_to_fruit_min``  = first integer in ``grove_years_to_fruit``.
- ``grove_pollination_min``     = 1 on self-fertile / self-pollinating /
  parthenocarpic; 3 on three / 3+; 2 on partner / second / two / 2+ / cross;
  else unset.
- ``grove_harvest_months``      from ``grove_harvest_season`` by a small table
  (Summer = 7,8; Summer–Fall = 8,9,10; Fall = 9,10,11; Late fall = 10,11,12;
  Spring = 4,5,6); month numbers already present in the text win.
- ``grove_tolerates_wet``       true if ``grove_soil`` mentions wet / moist /
  bottomland / flood, or ``grove_watering == "high"``.
- ``grove_tolerates_poor_soil`` true if ``grove_soil`` mentions rocky / clay /
  thin / poor / sandy / drought.
- ``grove_tolerates_shade``     from ``grove_sun`` (partial / shade). The model
  computes this too; the script sets it only to make the backfill explicit.
- ``grove_wildlife_value``      high if ``grove_wildlife`` mentions deer or
  turkey; medium on birds or squirrels; else unset.

A field is written only when its parse yields a *set* value (integer > 0,
non-blank month list, a True tolerance, a non-blank wildlife rating); a template
with no parsed value is left untouched and keeps its sign-off.

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    DRY_RUN=1 python3 scripts/backfill_planner_facts.py   # plan only (default)
    # DRY_RUN=0 -> actually writes.

QA first; prod runs only with Josh's explicit go after he confirms the table.
Exit codes: 0 ok, 1 auth or setup failure.
"""

from __future__ import annotations

import os
import re
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# DRY_RUN defaults to ON: only DRY_RUN=0 actually writes.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

_HISTORICAL_MARKER = "[in-store historical]"

# Source prose fields read per template, plus the planner fields read for the
# before/after table.
_SOURCE_FIELDS = [
    "name",
    "grove_years_to_fruit",
    "grove_pollination",
    "grove_harvest_season",
    "grove_soil",
    "grove_watering",
    "grove_sun",
    "grove_wildlife",
]
_PLANNER_FIELDS = [
    "grove_years_to_fruit_min",
    "grove_pollination_min",
    "grove_harvest_months",
    "grove_tolerates_wet",
    "grove_tolerates_poor_soil",
    "grove_tolerates_shade",
    "grove_wildlife_value",
]

_WET_WORDS = ("wet", "moist", "bottomland", "flood")
_POOR_WORDS = ("rocky", "clay", "thin", "poor", "sandy", "drought")


# ── Pure parse rules (unit-tested in test_planner_facts.py) ──────────────────
def parse_years_to_fruit_min(text: str | None) -> int:
    """First integer in the Years-to-Fruit prose; 0 when there is none."""
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else 0


def parse_pollination_min(text: str | None) -> int:
    """Pollination-partner floor from the Pollination prose; 0 = unset.

    Order matters: self-fertile wins (1), then the more specific "three/3+" (3)
    before the generic partner/second/two/2+/cross group (2) — "needs three
    partners" must score 3, not 2.
    """
    t = (text or "").lower()
    if not t.strip():
        return 0
    if re.search(r"self[\s-]?fertile|self[\s-]?pollinat|parthenocarp", t):
        return 1
    if re.search(r"\bthree\b|3\+", t):
        return 3
    if re.search(r"\bpartner|\bsecond\b|\btwo\b|2\+|\bcross", t):
        return 2
    return 0


def parse_harvest_months(text: str | None) -> str:
    """Comma list of month numbers from the Harvest-Season prose; "" = unset.

    Month numbers already present in the text win (deduped, in order); otherwise
    a small season→months table. Non-table seasons return "".
    """
    text = (text or "").strip()
    if not text:
        return ""
    nums = [int(n) for n in re.findall(r"\d+", text)]
    nums = [n for n in nums if 1 <= n <= 12]
    if nums:
        ordered: list[int] = []
        for n in nums:
            if n not in ordered:
                ordered.append(n)
        return ",".join(str(n) for n in ordered)
    norm = text.lower().replace("–", "-").replace("—", "-")
    if "summer" in norm and "fall" in norm:
        return "8,9,10"
    if "late fall" in norm:
        return "10,11,12"
    if "summer" in norm:
        return "7,8"
    if "fall" in norm:
        return "9,10,11"
    if "spring" in norm:
        return "4,5,6"
    return ""


def parse_tolerates_wet(soil: str | None, watering: str | None) -> bool:
    """Suited to a wet spot: soil prose names a wet word, or watering is high."""
    s = (soil or "").lower()
    if any(word in s for word in _WET_WORDS):
        return True
    return (watering or "").lower() == "high"


def parse_tolerates_poor_soil(soil: str | None) -> bool:
    """Suited to rough ground: soil prose names a poor-soil word."""
    s = (soil or "").lower()
    return any(word in s for word in _POOR_WORDS)


def parse_tolerates_shade(sun: str | None) -> bool:
    """Suited to shade: sun requirement is partial or shade."""
    return (sun or "") in ("partial", "shade")


def parse_wildlife_value(text: str | None) -> str | None:
    """Wildlife rating from the Wildlife prose; None = unset.

    High on deer/turkey (the deer track's headline species), medium on
    birds/squirrels, else unset.
    """
    t = (text or "").lower()
    if re.search(r"deer|turkey", t):
        return "high"
    if re.search(r"bird|squirrel", t):
        return "medium"
    return None


def parse_planner_facts(rec: dict) -> dict:
    """Build the write-vals of *set* planner values parsed from one template row.

    Only fields whose parse yields a set value are included — an integer > 0, a
    non-blank month list, a True tolerance, a non-blank wildlife rating — so a
    template with nothing to add is left untouched (and keeps its sign-off).
    """
    vals: dict = {}
    years = parse_years_to_fruit_min(rec.get("grove_years_to_fruit"))
    if years > 0:
        vals["grove_years_to_fruit_min"] = years
    pollination = parse_pollination_min(rec.get("grove_pollination"))
    if pollination > 0:
        vals["grove_pollination_min"] = pollination
    months = parse_harvest_months(rec.get("grove_harvest_season"))
    if months:
        vals["grove_harvest_months"] = months
    if parse_tolerates_wet(rec.get("grove_soil"), rec.get("grove_watering")):
        vals["grove_tolerates_wet"] = True
    if parse_tolerates_poor_soil(rec.get("grove_soil")):
        vals["grove_tolerates_poor_soil"] = True
    if parse_tolerates_shade(rec.get("grove_sun")):
        vals["grove_tolerates_shade"] = True
    wildlife = parse_wildlife_value(rec.get("grove_wildlife"))
    if wildlife:
        vals["grove_wildlife_value"] = wildlife
    return vals


# ── XML-RPC orchestration (not exercised by the unit tests) ──────────────────
def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"authentication failed for {ODOO_USER} on {ODOO_URL} db={ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB} ({ODOO_URL})")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def plants_root_id(models, uid) -> int:
    rows = call(
        models,
        uid,
        "ir.model.data",
        "search_read",
        [[("module", "=", "grove_headless"), ("name", "=", "categ_plants")]],
        {"fields": ["res_id"], "limit": 1},
    )
    if not rows:
        fail("category xmlid grove_headless.categ_plants not found — is grove_headless installed on this host?")
    return rows[0]["res_id"]


def real_plant_templates(models, uid) -> list[dict]:
    """Gated plant templates minus historical rows, ordered by name."""
    root = plants_root_id(models, uid)
    domain = [
        ("categ_id", "child_of", root),
        ("type", "=", "consu"),
        ("grove_gate_exempt", "=", False),
        ("name", "not ilike", _HISTORICAL_MARKER),
    ]
    return call(
        models,
        uid,
        "product.template",
        "search_read",
        [domain],
        {"fields": _SOURCE_FIELDS + _PLANNER_FIELDS, "order": "name"},
    )


def _before_after_rows(rec: dict, parsed: dict) -> list[str]:
    """One table line per field the parse would change (old -> new)."""
    rows = []
    for name, new in parsed.items():
        old = rec.get(name)
        old_disp = "" if old in (False, None, 0, "") else old
        rows.append(f"      {name}: {old_disp!r} -> {new!r}")
    return rows


def main() -> None:
    models, uid = authenticate()
    templates = real_plant_templates(models, uid)
    print(f"\n── Backfill planner facts ── {len(templates)} real plant template(s)")
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no writes. Set DRY_RUN=0 to apply.\n")

    written = 0
    for rec in templates:
        name, tid = rec["name"], rec["id"]
        parsed = parse_planner_facts(rec)
        if not parsed:
            print(f"  · {name} (id={tid}): nothing to parse")
            continue
        print(f"  ~ {name} (id={tid}): {len(parsed)} field(s)")
        for line in _before_after_rows(rec, parsed):
            print(line)
        if DRY_RUN:
            continue
        # Clear the human sign-off alongside the parsed values (machine write).
        call(models, uid, "product.template", "write", [[tid], dict(parsed, grove_facts_reviewed=False)])
        written += 1

    if DRY_RUN:
        print(
            f"\nDone (dry-run). {len(templates)} template(s) inspected; 0 written. Confirm the table, then DRY_RUN=0."
        )
    else:
        print(f"\nDone. {written} template(s) written; grove_facts_reviewed cleared on each. Run the review pass next.")


if __name__ == "__main__":
    main()
