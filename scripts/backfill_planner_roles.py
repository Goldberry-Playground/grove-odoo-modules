#!/usr/bin/env python3
"""One-time parser that seeds the planner roles, succession and living-mulch bit.

Context (GOL-3548, planner winter-beta spec 2026-10-21 section A): every planner
plant now carries a role (anchor / companion), a succession word (fast / mid /
long-lived) and — for ground covers — a living-mulch flag, on top of the
scoreable facts ``backfill_planner_facts.py`` already filled. This script walks
the real plant templates and derives those three fields from the existing layer,
years-to-first-crop and prose, then writes them through the normal ``write()``
path (so chatter shows the change) and clears the ``grove_facts_reviewed``
sign-off on every template it touches — the existing "machine writes clear the
sign-off" rule, so the review sitting (spec section E) catches them.

Same shape as ``backfill_planner_facts.py``: XML-RPC, ``DRY_RUN=1`` by default,
QA first. In dry-run it prints a before/after table for Josh to confirm; only
``DRY_RUN=0`` actually writes.

The parse rules (spec section A.2) — each is a pure function below so the test
suite can unit-case it:

- ``grove_planner_role``:    ``anchor`` when ``grove_layer == "canopy"``, or when
  ``grove_layer == "understory"`` and ``grove_years_to_fruit_min >= 3``; else
  ``companion``. Spawn (``grove_layer == "fungal"``): wine cap is ``anchor``, the
  others ``companion``.
- ``grove_succession``:      ``fast`` when ``grove_years_to_fruit_min <= 2``;
  ``long`` when ``>= 4`` and ``grove_layer == "canopy"``; else ``mid``. (An
  unset years value is 0, which reads as ``fast`` — the facts backfill runs
  first, and the dry-run table and review sitting catch anything still unset.)
- ``grove_living_mulch``:    true for a ``ground`` layer plant whose
  ``grove_wildlife`` or ``grove_soil`` prose mentions mulch, cover, fix, clover
  or comfrey.

Role and succession are set on every template (every planner plant carries one);
living mulch is set only when true. A re-run is idempotent: a field already
holding its parsed value is dropped from the write, so the sign-off is only
cleared when something actually changes.

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    DRY_RUN=1 python3 scripts/backfill_planner_roles.py   # plan only (default)
    # DRY_RUN=0 -> actually writes.

QA first; prod runs only with Josh's explicit go after he confirms the table.
Exit codes: 0 ok, 1 auth or setup failure.
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# DRY_RUN defaults to ON: only DRY_RUN=0 actually writes.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

_HISTORICAL_MARKER = "[in-store historical]"

# Source fields read per template, plus the target fields read for the
# before/after table.
_SOURCE_FIELDS = [
    "name",
    "grove_layer",
    "grove_years_to_fruit_min",
    "grove_wildlife",
    "grove_soil",
]
_TARGET_FIELDS = [
    "grove_planner_role",
    "grove_succession",
    "grove_living_mulch",
]

# Ground-cover prose that marks a plant as living mulch (spec A.2). "fix" is a
# substring so it also catches "fixes" / "nitrogen-fixing".
_MULCH_WORDS = ("mulch", "cover", "fix", "clover", "comfrey")


# ── Pure parse rules (unit-tested in test_planner_roles.py) ──────────────────
def is_wine_cap(name: str | None) -> bool:
    """True when a spawn product reads as wine cap (the one spawn anchor)."""
    return "wine cap" in (name or "").lower()


def parse_role(layer: str | None, years_min: int | None, name: str | None) -> str:
    """Anchor / companion from layer, years-to-first-crop and (for spawn) name."""
    years = years_min or 0
    if layer == "fungal":
        return "anchor" if is_wine_cap(name) else "companion"
    if layer == "canopy":
        return "anchor"
    if layer == "understory" and years >= 3:
        return "anchor"
    return "companion"


def parse_succession(layer: str | None, years_min: int | None) -> str:
    """Fast / mid / long-lived from years-to-first-crop and layer."""
    years = years_min or 0
    if years <= 2:
        return "fast"
    if years >= 4 and layer == "canopy":
        return "long"
    return "mid"


def parse_living_mulch(layer: str | None, wildlife: str | None, soil: str | None) -> bool:
    """True for a ground cover whose Wildlife/Soil prose names a mulch word."""
    if layer != "ground":
        return False
    text = f"{wildlife or ''} {soil or ''}".lower()
    return any(word in text for word in _MULCH_WORDS)


def parse_planner_roles(rec: dict) -> dict:
    """Build the full intent vals for one template row.

    Role and succession are always present (every planner plant carries one);
    living mulch is included only when true. The caller drops any field already
    holding its parsed value before writing, so a re-run clears no sign-off.
    """
    layer = rec.get("grove_layer")
    years = rec.get("grove_years_to_fruit_min")
    vals: dict = {
        "grove_planner_role": parse_role(layer, years, rec.get("name")),
        "grove_succession": parse_succession(layer, years),
    }
    if parse_living_mulch(layer, rec.get("grove_wildlife"), rec.get("grove_soil")):
        vals["grove_living_mulch"] = True
    return vals


def changed_vals(rec: dict, parsed: dict) -> dict:
    """Subset of parsed vals that differ from the record's current values.

    An Odoo read returns False for an unset Selection/Boolean, so a first-run
    role of "companion" differs from False and is kept, while a second run that
    re-derives the same value is dropped.
    """
    changed = {}
    for name, new in parsed.items():
        old = rec.get(name)
        if name == "grove_living_mulch":
            if bool(old) != bool(new):
                changed[name] = new
        elif (old or "") != new:
            changed[name] = new
    return changed


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
        {"fields": _SOURCE_FIELDS + _TARGET_FIELDS, "order": "name"},
    )


def _before_after_rows(rec: dict, changed: dict) -> list[str]:
    """One table line per field the parse would change (old -> new)."""
    rows = []
    for name, new in changed.items():
        old = rec.get(name)
        old_disp = "" if old in (False, None, 0, "") else old
        rows.append(f"      {name}: {old_disp!r} -> {new!r}")
    return rows


def main() -> None:
    models, uid = authenticate()
    templates = real_plant_templates(models, uid)
    print(f"\n── Backfill planner roles ── {len(templates)} real plant template(s)")
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no writes. Set DRY_RUN=0 to apply.\n")

    written = 0
    for rec in templates:
        name, tid = rec["name"], rec["id"]
        changed = changed_vals(rec, parse_planner_roles(rec))
        if not changed:
            print(f"  · {name} (id={tid}): nothing to change")
            continue
        print(f"  ~ {name} (id={tid}): {len(changed)} field(s)")
        for line in _before_after_rows(rec, changed):
            print(line)
        if DRY_RUN:
            continue
        # Clear the human sign-off alongside the parsed values (machine write).
        call(models, uid, "product.template", "write", [[tid], dict(changed, grove_facts_reviewed=False)])
        written += 1

    if DRY_RUN:
        print(
            f"\nDone (dry-run). {len(templates)} template(s) inspected; 0 written. Confirm the table, then DRY_RUN=0."
        )
    else:
        print(
            f"\nDone. {written} template(s) written; grove_facts_reviewed cleared on each. "
            "Run the review sitting next."
        )


if __name__ == "__main__":
    main()
