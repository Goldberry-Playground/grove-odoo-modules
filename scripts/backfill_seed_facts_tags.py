#!/usr/bin/env python3
"""Backfill grove_* facts + product tags onto the canonical seed templates.

Context (GOL-639 / GOL-641): the seed-dup remediation
(``remediate_seed_duplicates.py``) merged each duplicate's *cultivars*, prices
and stock into the CANONICAL original, then archived the dup. But it never
copied the two remaining seed payloads — the ``grove_*`` fact block
(botanical name, zones, layer, sun, size, spacing, soil) and the
``product_tag_ids`` — because those live only in ``seed_variety_products.py``'s
CREATE path, which the seed SKIPs for a species that already has a canonical
template ("Extend it via the remediation path, not the seed").

Net effect on QA: the multi-variant trees (Pear 162, Persimmon 163,
Serviceberry 168, Fig 155, Kiwi 158) publish with ``variant_count>1`` and
correct prices, but with an EMPTY facts block and NO tags — so the GOL-634
spec block renders empty and the /shop zone/tag facets have nothing to filter
on. Only Aronia (183), which the seed genuinely created, carries facts+tags.

This one-shot, idempotent migration writes the seed's authoritative facts+tags
onto the canonical templates. It:

  * sets each ``grove_*`` fact ONLY when the template's current value is empty
    (never clobbers a manual edit);
  * ADDS tags with ``(4, id)`` (never removes an existing tag);
  * asserts each id resolves to the expected species before any write, so a
    shifted catalog aborts loudly instead of mutating the wrong template.

It touches no prices, stock, cultivars or publish state — only descriptive
metadata that drives the spec block and the zone/tag facets. Pear (162) is
intentionally left tagless (the seed defines ``tags: []`` for Pear); its facts
are still backfilled.

Usage
-----
    ODOO_URL=http://localhost:8069 ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/backfill_seed_facts_tags.py   # plan only
    # DRY_RUN unset -> applies the writes.

Exit codes: 0 ok, 1 auth/validation failure (a mismatched id aborts before any
write).
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

# The eight grove_* fact fields, mirroring seed_variety_products.FACT_FIELDS.
FACT_FIELDS = (
    "botanical_name",
    "zone_min",
    "zone_max",
    "layer",
    "sun",
    "mature_size",
    "spacing",
    "soil",
)

# Canonical template id -> the seed's authoritative facts+tags for that species.
# ids match remediate_seed_duplicates.MERGE_MAP originals; facts/tags are copied
# verbatim from seed_variety_products.PRODUCTS. "species" is asserted (substring,
# alnum-normalised) against the live template name before any write.
BACKFILL = {
    158: {
        "species": "kiwi",
        "tags": ["Food Forest", "Silvopasture"],
        "facts": {
            "botanical_name": "Actinidia arguta",
            "zone_min": 4,
            "zone_max": 8,
            "layer": "vine",
            "sun": "partial",
            "mature_size": "20-30 ft vine",
            "spacing": "10-15 ft",
            "soil": "Moist, well-drained",
        },
    },
    155: {
        "species": "fig",
        "tags": ["Food Forest", "Silvopasture"],
        "facts": {
            "botanical_name": "Ficus carica",
            "zone_min": 7,
            "zone_max": 9,
            "layer": "shrub",
            "sun": "full",
            "mature_size": "10-15 ft",
            "spacing": "10-12 ft",
            "soil": "Well-drained",
        },
    },
    162: {
        "species": "pear",
        "tags": [],  # Pear is intentionally tagless in the seed.
        "facts": {
            "botanical_name": "Pyrus communis",
            "zone_min": 4,
            "zone_max": 8,
            "layer": "canopy",
            "sun": "full",
            "mature_size": "15-20 ft",
            "spacing": "15-20 ft",
            "soil": "Deep, well-drained loam",
        },
    },
    163: {
        "species": "persimmon",
        "tags": ["Food Forest", "Silvopasture"],
        "facts": {
            "botanical_name": "Diospyros kaki",
            "zone_min": 6,
            "zone_max": 9,
            "layer": "understory",
            "sun": "full",
            "mature_size": "10-15 ft",
            "spacing": "12-15 ft",
            "soil": "Well-drained",
        },
    },
    168: {
        "species": "serviceberry",  # canonical name is 'Service Berry'
        "tags": ["Wildlife", "Native", "Food Forest"],
        "facts": {
            "botanical_name": "Amelanchier laevis",
            "zone_min": 4,
            "zone_max": 8,
            "layer": "understory",
            "sun": "partial",
            "mature_size": "15-25 ft",
            "spacing": "10-15 ft",
            "soil": "Moist, well-drained",
        },
    },
}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB}")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def tag_id(models, uid, name: str) -> int:
    found = call(models, uid, "product.tag", "search", [[("name", "=", name)]], {"limit": 1})
    if found:
        return found[0]
    if DRY_RUN:
        print(f"    (dry-run) WOULD create product.tag '{name}'")
        return -1
    tid = call(models, uid, "product.tag", "create", [{"name": name}])
    print(f"    created product.tag '{name}' (id={tid})")
    return tid


def main() -> None:
    models, uid = authenticate()

    fact_read_fields = [f"grove_{f}" for f in FACT_FIELDS]
    print("\n── Backfill facts + tags ──")
    changed = 0
    for tmpl_id, spec in BACKFILL.items():
        rows = call(
            models,
            uid,
            "product.template",
            "read",
            [[tmpl_id], ["name", "product_tag_ids", *fact_read_fields]],
            {"context": {"active_test": False}},
        )
        if not rows:
            fail(f"template id={tmpl_id} not found (expected species '{spec['species']}')")
        row = rows[0]
        if spec["species"] not in norm(row["name"]):
            fail(
                f"template id={tmpl_id} is '{row['name']}', expected species "
                f"'{spec['species']}' — catalog shifted; aborting before any write."
            )

        # Facts: set only the fields that are currently empty.
        fact_writes: dict = {}
        for f in FACT_FIELDS:
            desired = spec["facts"][f]
            current = row.get(f"grove_{f}")
            if not current and current != desired:
                fact_writes[f"grove_{f}"] = desired

        # Tags: add any missing, never remove.
        have_tag_ids = set(row.get("product_tag_ids") or [])
        want_tag_ids = {tag_id(models, uid, t) for t in spec["tags"]}
        add_tag_ids = {tid for tid in want_tag_ids if tid > 0 and tid not in have_tag_ids}

        if not fact_writes and not add_tag_ids:
            print(f"  SKIP {row['name']} (id={tmpl_id}) — facts+tags already present")
            continue

        summary = []
        if fact_writes:
            summary.append(f"facts={sorted(fact_writes)}")
        if add_tag_ids:
            summary.append(f"+{len(add_tag_ids)} tag(s)")
        if DRY_RUN:
            print(f"  ~ WOULD UPDATE {row['name']} (id={tmpl_id}): {', '.join(summary)}")
            continue

        write_vals: dict = dict(fact_writes)
        if add_tag_ids:
            write_vals["product_tag_ids"] = [(4, tid) for tid in add_tag_ids]
        call(models, uid, "product.template", "write", [[tmpl_id], write_vals])
        print(f"  ~ UPDATED {row['name']} (id={tmpl_id}): {', '.join(summary)}")
        changed += 1

    print(f"\nDone. {changed} template(s) updated{' (dry-run: 0 written)' if DRY_RUN else ''}.")


if __name__ == "__main__":
    main()
