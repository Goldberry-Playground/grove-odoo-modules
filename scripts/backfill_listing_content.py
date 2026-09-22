#!/usr/bin/env python3
"""One-time backfill of listing content for the existing nursery plants.

Context (GOL-2385, spec 2026-09-21 section D): the listing-content gate (A),
the USDA/Perenual fetch-facts button (B) and the Paperclip content drafter (C)
give new plants a path to a complete listing. This script walks the plants that
predate that machinery and runs them through *exactly the production code path*
— no shortcut writes — so the enrichment queue, provenance and draft-request
state are all populated the same way a human clicking the form buttons would.

For each real plant template it calls, over XML-RPC:

  1. ``action_fetch_facts``  — fills empty facts from USDA now, enqueues a
     budgeted Perenual job (see enrich_job_cron.xml). ~2 provider calls each.
  2. ``action_request_draft`` — sets ``grove_draft_state = requested`` so the
     grove-content-drafter routine (section C) drafts the prose.

"Real plant" excludes ``[in-store historical]`` rows, services and bundles: the
gated set (type ``consu``, not ``grove_gate_exempt``, under the Plants category
``grove_headless.categ_plants``), minus historical names. ~19 products ≈ 38
Perenual calls — inside one UTC day's free-tier budget.

``action_request_draft`` lands with section C (GOL-2384). If it is not yet
deployed on the target host this script still runs the fetch-facts half and
reports the draft-request step as skipped per product, so it is safe to run the
moment B is live and re-run once C ships.

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    DRY_RUN=1 python3 scripts/backfill_listing_content.py   # plan only (default)
    # DRY_RUN=0 -> actually calls the buttons.

QA first; prod runs only with Josh's explicit go. Exit codes: 0 ok, 1 auth or
setup failure (a missing Plants category aborts before any call).
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# DRY_RUN defaults to ON: only DRY_RUN=0 actually calls the buttons.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

_HISTORICAL_MARKER = "[in-store historical]"


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
        {"fields": ["name", "grove_botanical_name", "grove_listing_missing"], "order": "name"},
    )


def run_button(models, uid, tmpl_id: int, method: str) -> str:
    """Call a product.template button; return a short status string.

    An xmlrpc Fault naming a missing method (e.g. action_request_draft before
    section C ships) is reported as 'unavailable' rather than aborting the run.
    """
    try:
        call(models, uid, "product.template", method, [[tmpl_id]])
        return "ok"
    except xmlrpc.client.Fault as exc:
        detail = (exc.faultString or "").strip().splitlines()[-1]
        if method in detail or "object has no attribute" in detail or "does not exist" in detail:
            return f"unavailable ({detail})"
        raise


def main() -> None:
    models, uid = authenticate()
    templates = real_plant_templates(models, uid)
    print(f"\n── Backfill listing content ── {len(templates)} real plant template(s)")
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no buttons called. Set DRY_RUN=0 to apply.\n")

    fetched = drafted = 0
    for tmpl in templates:
        name, tid = tmpl["name"], tmpl["id"]
        missing = tmpl.get("grove_listing_missing") or "(complete)"
        if DRY_RUN:
            print(f"  ~ WOULD fetch_facts + request_draft: {name} (id={tid}) — missing: {missing}")
            continue
        fetch_status = run_button(models, uid, tid, "action_fetch_facts")
        draft_status = run_button(models, uid, tid, "action_request_draft")
        if fetch_status == "ok":
            fetched += 1
        if draft_status == "ok":
            drafted += 1
        print(f"  ~ {name} (id={tid}): fetch_facts={fetch_status}; request_draft={draft_status}")

    if DRY_RUN:
        print(f"\nDone (dry-run). {len(templates)} template(s) would be processed; 0 buttons called.")
    else:
        print(
            f"\nDone. {len(templates)} template(s) processed — "
            f"{fetched} fetched, {drafted} draft-requested. "
            "Perenual jobs drain on the budgeted cron; drafts land as the "
            "grove-content-drafter routine polls."
        )


if __name__ == "__main__":
    main()
