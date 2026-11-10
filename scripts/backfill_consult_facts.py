#!/usr/bin/env python3
"""One-time parser that proposes the eight-value syntropic stratum (consult).

Context (GOL-3574, consult-dashboard spec 2026-10-22 section B): the consultant
facts add an eight-value ``grove_syntropic_stratum`` behind the retail planner's
three-value stratum. This script walks the real plant templates and proposes
that eight-value stratum from the existing retail ``grove_layer`` plus the retail
``grove_planner_role`` (planner beta A), then writes it through the normal
``write()`` path (so chatter shows the change) and clears the
``grove_facts_reviewed`` sign-off on every template it touches — the existing
"machine writes clear the sign-off" rule, so the review sitting catches the
proposals.

Only the stratum is proposed: the four-value ``grove_succession_stage`` is a
consultant judgement left blank, so the retail-value projection does NOT activate
(it needs both) and nothing in flight breaks until a consultant fills the stage.

Same shape as ``backfill_planner_roles.py``: XML-RPC, ``DRY_RUN=1`` by default,
QA first. In dry-run it prints a before/after table for Josh to confirm; only
``DRY_RUN=0`` actually writes.

The parse rule (spec section B) — a pure function below so the test suite can
unit-case it. The retail layer (+ role for the canopy split) proposes a stratum:

- ``fungal``                       -> ``fungal``
- ``canopy`` + ``anchor``          -> ``high_canopy``
- ``canopy`` + anything else       -> ``low_canopy``
- ``understory``                   -> ``low_canopy``
- ``shrub``                        -> ``shrub``
- ``vine``                         -> ``herbaceous``
- ``ground``                       -> ``ground_cover``
- unset / anything else            -> no proposal (left blank)

``emergent`` and ``root_tuber`` are never auto-proposed: they are finer calls a
consultant makes in the dashboard. A re-run is idempotent: a stratum already
holding its proposed value is dropped from the write, so the sign-off is only
cleared when something actually changes.

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    DRY_RUN=1 python3 scripts/backfill_consult_facts.py   # plan only (default)
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

# Source fields read per template, plus the target field read for the
# before/after table.
_SOURCE_FIELDS = [
    "name",
    "grove_layer",
    "grove_planner_role",
]
_TARGET_FIELDS = [
    "grove_syntropic_stratum",
]

# Retail layer (+ role for the canopy split) -> proposed eight-value stratum.
# A layer with no mapping, or an unset layer, yields no proposal.
_STRATUM_BY_LAYER = {
    "understory": "low_canopy",
    "shrub": "shrub",
    "vine": "herbaceous",
    "ground": "ground_cover",
    "fungal": "fungal",
}


# ── Pure parse rule (unit-tested in test_consult_facts.py) ───────────────────
def parse_syntropic_stratum(layer: str | None, role: str | None) -> str | bool:
    """Propose the eight-value stratum from the retail layer and role.

    Canopy splits on the retail role (anchor -> high_canopy, else low_canopy);
    every other mapped layer is fixed. Returns ``False`` (no proposal) for an
    unset or unmapped layer.
    """
    if layer == "canopy":
        return "high_canopy" if role == "anchor" else "low_canopy"
    return _STRATUM_BY_LAYER.get(layer, False)


def parse_consult_facts(rec: dict) -> dict:
    """Build the intent vals for one template row.

    Only ``grove_syntropic_stratum`` is proposed, and only when the layer maps to
    one; the caller drops a field already holding its proposed value before
    writing, so a re-run clears no sign-off.
    """
    stratum = parse_syntropic_stratum(rec.get("grove_layer"), rec.get("grove_planner_role"))
    return {"grove_syntropic_stratum": stratum} if stratum else {}


def changed_vals(rec: dict, parsed: dict) -> dict:
    """Subset of parsed vals that differ from the record's current values.

    An Odoo read returns False for an unset Selection, so a first-run value
    differs from False and is kept, while a second run re-deriving the same value
    is dropped.
    """
    changed = {}
    for name, new in parsed.items():
        old = rec.get(name)
        if (old or "") != new:
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
    print(f"\n── Backfill consult facts ── {len(templates)} real plant template(s)")
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no writes. Set DRY_RUN=0 to apply.\n")

    written = 0
    for rec in templates:
        name, tid = rec["name"], rec["id"]
        changed = changed_vals(rec, parse_consult_facts(rec))
        if not changed:
            print(f"  · {name} (id={tid}): nothing to change")
            continue
        print(f"  ~ {name} (id={tid}): {len(changed)} field(s)")
        for line in _before_after_rows(rec, changed):
            print(line)
        if DRY_RUN:
            continue
        # Clear the human sign-off alongside the proposed value (machine write).
        call(models, uid, "product.template", "write", [[tid], dict(changed, grove_facts_reviewed=False)])
        written += 1

    if DRY_RUN:
        print(
            f"\nDone (dry-run). {len(templates)} template(s) inspected; 0 written. Confirm the table, then DRY_RUN=0."
        )
    else:
        print(
            f"\nDone. {written} template(s) written; grove_facts_reviewed cleared on each. Run the review sitting next."
        )


if __name__ == "__main__":
    main()
