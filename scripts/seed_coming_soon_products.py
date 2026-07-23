#!/usr/bin/env python3
"""Seed the 11 "coming soon" placeholder plant products (GOL-757, PR 2).

These are species the nursery intends to carry but has NO stock for yet. Josh's
spec (Asana 1216758513298024, 2026-07-23) wants their product pages to RENDER —
so a visitor browsing the catalog can see they are on the way — while being
NOT purchasable. This is a distinct storefront state from an in-stock, sold-out,
or unpublished product, and the grove_headless catalog controller already draws
the line we need:

  * ``product_detail`` (``/grove/api/v1/products/<id>``) filters ONLY on
    ``website_published = True`` — so a published placeholder renders its own
    product page.
  * ``build_product_domain`` (the ``/shop`` grid and every ``?cat=<slug>``
    facet) requires BOTH ``website_published = True`` AND ``sale_ok = True``.

So a placeholder set to ``website_published = True`` + ``sale_ok = False``:
  * renders as a not-purchasable product page (sale_ok False = no add-to-cart), and
  * is EXCLUDED from the cat-bar counts and the shop grid.

That exclusion is not incidental — it is required by Josh's own verify-done
targets. Seven of the eleven placeholders are Native (Black Cherry, PawPaw,
Eastern Redbud, American Hazelnut, Shagbark Hickory, Black Walnut, Butternut),
but the target is ``?cat=native`` returns 5 (the live natives only). A
``sale_ok = True`` + ``qty 0`` model would push all seven into that facet
(``build_product_domain`` has no quantity filter), inflating native to 12 and
breaking the target. ``sale_ok = False`` is therefore the correct, spec-
consistent encoding of "published but not purchasable" — qty 0 is retained as a
belt-and-suspenders signal for the buy box, but it is ``sale_ok`` that keeps the
facet counts honest.

When a species graduates to live stock, the operator flips ``sale_ok = True``,
sets ``list_price``, and adds stock quants (or re-runs ``seed_variety_products``
after archiving the placeholder) — the Format axis and use-type categories are
already in place.

Categories (public_categ_ids, m2m) MUST already exist: this script RESOLVES
them by name and FAILS LOUD if any is missing, because their creation +
Berries->Berry & Nut Shrubs rename is owned by ``taxonomy_reconcile.py``
(GOL-757 PR 1). Run that first. Enforcing the sequence here (rather than
find_or_create-ing the categories) keeps a single source of truth for the
category set and avoids forking a fresh "Berry & Nut Shrubs" alongside an
un-renamed "Berries".

Idempotent per species: matched by norm_species against the live ACTIVE
template index (so archived GOL-641 dups are ignored) and by this script's own
default_code. A converged catalog is a no-op re-run.

Usage
-----
    # Dry run (read-only): resolves categories, reports what would be created.
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/seed_coming_soon_products.py

    # Live: DRY_RUN unset -> creates the placeholder templates.

Exit codes: 0 ok, 1 auth/data failure (fails loudly; each template is one
create call).
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

COMPANY_NAME = "At The Grove Nursery"

# The Format axis names MUST be exactly these — the catalog serializer keys on
# them by name (see seed_variety_products.py / grove_headless _structure_variant).
FORMAT_ATTR = "Format"
FORMAT_VALUES = ("Bareroot", "Potted")  # both, so a graduated species has the axis ready
FORMAT_ABBR = {"Potted": "PT", "Bareroot": "BR"}

# Use-type storefront categories (must pre-exist — created by taxonomy_reconcile.py).
FRUIT_TREES = "Fruit Trees"
NATIVE = "Native"
NUT_TREES = "Nut Trees"
BERRY_NUT_SHRUBS = "Berry & Nut Shrubs"

# ── The 11 coming-soon placeholders (GOL-757 spec, 2026-07-23) ──────────────
# ``cats`` is the EXACT public_categ_ids set. ``internal`` is the growth-habit
# accounting bucket (decoupled from the storefront use-type). ``layer`` is the
# food-forest canopy layer (factual; drives the layer facet when graduated). No
# price, zone, sun, size, spacing, or soil is set — the spec supplies only the
# botanical name, and inventing horticultural facts for unstocked species is a
# grounding risk (GOL-589). The operator fills those in at graduation.
# fmt: off
PRODUCTS: list[dict[str, Any]] = [
    {"name": "Che",              "code": "CHE",        "sku": "TREE-CHE",
     "botanical": "Maclura tricuspidata",           "cats": [FRUIT_TREES],
     "internal": "Trees",  "layer": "understory"},
    {"name": "Black Cherry",     "code": "BLKCHERRY",  "sku": "TREE-BLKCHERRY",
     "botanical": "Prunus serotina",                "cats": [FRUIT_TREES, NATIVE],
     "internal": "Trees",  "layer": "canopy"},
    {"name": "Sour Cherry",      "code": "SOURCHERRY", "sku": "TREE-SOURCHERRY",
     "botanical": "Prunus cerasus",                 "cats": [FRUIT_TREES],
     "internal": "Trees",  "layer": "understory"},
    {"name": "Sweet Cherry",     "code": "SWTCHERRY",  "sku": "TREE-SWTCHERRY",
     "botanical": "Prunus avium",                   "cats": [FRUIT_TREES],
     "internal": "Trees",  "layer": "canopy"},
    {"name": "American Hazelnut", "code": "AMHAZEL",   "sku": "SHRUB-AMHAZEL",
     "botanical": "Corylus americana",              "cats": [BERRY_NUT_SHRUBS, NATIVE],
     "internal": "Shrubs", "layer": "shrub"},
    {"name": "Hybrid Hazelnut",  "code": "HYBHAZEL",   "sku": "SHRUB-HYBHAZEL",
     "botanical": "Corylus americana × avellana", "cats": [NUT_TREES],
     "internal": "Shrubs", "layer": "shrub"},
    {"name": "Shagbark Hickory", "code": "SHAGHICK",   "sku": "TREE-SHAGHICK",
     "botanical": "Carya ovata",                    "cats": [NUT_TREES, NATIVE],
     "internal": "Trees",  "layer": "canopy"},
    {"name": "Black Walnut",     "code": "BLKWALNUT",  "sku": "TREE-BLKWALNUT",
     "botanical": "Juglans nigra",                  "cats": [NUT_TREES, NATIVE],
     "internal": "Trees",  "layer": "canopy"},
    {"name": "Butternut",        "code": "BUTTERNUT",  "sku": "TREE-BUTTERNUT",
     "botanical": "Juglans cinerea",                "cats": [NUT_TREES, NATIVE],
     "internal": "Trees",  "layer": "canopy"},
    {"name": "Eastern Redbud",   "code": "REDBUD",     "sku": "TREE-REDBUD",
     "botanical": "Cercis canadensis",              "cats": [NATIVE],
     "internal": "Trees",  "layer": "understory"},
    {"name": "PawPaw",           "code": "PAWPAW",     "sku": "TREE-PAWPAW",
     "botanical": "Asimina triloba",                "cats": [NATIVE, FRUIT_TREES],
     "internal": "Trees",  "layer": "understory"},
]
# fmt: on


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


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


def call(models: xmlrpc.client.ServerProxy, uid: int, model: str, method: str, args: list, kwargs: dict | None = None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def find_or_create(models, uid, model: str, domain: list, vals: dict, label: str) -> int:
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if ids:
        print(f"  = {model} '{label}' exists (id={ids[0]})")
        return ids[0]
    if DRY_RUN:
        print(f"  + WOULD CREATE {model} '{label}'")
        return 0
    new_id = call(models, uid, model, "create", [vals])
    print(f"  + created {model} '{label}' (id={new_id})")
    return new_id


def norm_species(name: str) -> str:
    """Collapse case + non-alphanumerics so 'Service Berry' == 'serviceberry'.

    Mirrors the seed / reconcile normalizer so placeholder names are matched
    against the live catalog the same way (and archived GOL-641 dups are
    ignored — we only index ACTIVE templates).
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def variant_sku(product: dict, fmt: str) -> str:
    """TREE-CHE-PT — species code + format abbreviation."""
    return f"{product['code']}-{FORMAT_ABBR[fmt]}"


def resolve_category(models, uid, name: str) -> int:
    """Resolve a public category by name, failing loud if it does not exist.

    Category creation + the Berries->Berry & Nut Shrubs rename is owned by
    taxonomy_reconcile.py (GOL-757 PR 1). If a category is missing here, PR 1
    has not been applied to this database yet — stop rather than fork a
    duplicate category set.
    """
    ids = call(models, uid, "product.public.category", "search", [[("name", "=", name)]], {"limit": 1})
    if not ids:
        fail(
            f"Public category '{name}' not found. Run scripts/taxonomy_reconcile.py "
            f"(GOL-757 PR 1) against this database first — it creates the Native / "
            f"Nut Trees categories and renames Berries -> Berry & Nut Shrubs."
        )
    return ids[0]


def main() -> None:
    print(f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}")
    models, uid = authenticate()

    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    ctx = {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}

    print("\n── Prerequisites ──")
    # Resolve every use-type category the placeholders reference (fail loud if
    # PR 1 has not run). Also resolve the internal accounting categories.
    wanted_cats = sorted({c for p in PRODUCTS for c in p["cats"]})
    cat_id = {name: resolve_category(models, uid, name) for name in wanted_cats}
    for name, cid in cat_id.items():
        print(f"  = public category '{name}' (id={cid})")

    plants = call(models, uid, "product.category", "search", [[("name", "=", "Plants")]], {"limit": 1})
    internal_cat: dict[str, int] = {}
    for name in sorted({p["internal"] for p in PRODUCTS}):
        vals = {"name": name}
        if plants:
            vals["parent_id"] = plants[0]
        internal_cat[name] = find_or_create(
            models, uid, "product.category", [("name", "=", name)], vals, f"internal:{name}"
        )

    format_attr = find_or_create(
        models,
        uid,
        "product.attribute",
        [("name", "=", FORMAT_ATTR)],
        {"name": FORMAT_ATTR, "display_type": "radio", "create_variant": "always"},
        FORMAT_ATTR,
    )
    format_value_ids = {}
    for fv in FORMAT_VALUES:
        format_value_ids[fv] = find_or_create(
            models,
            uid,
            "product.attribute.value",
            [("name", "=", fv), ("attribute_id", "=", format_attr)],
            {"name": fv, "attribute_id": format_attr},
            f"{FORMAT_ATTR}:{fv}",
        )

    # Species-name index of every live ACTIVE template in this company — match the
    # canonical template by species, never fork a duplicate (GOL-641). Active-only
    # so archived dups do not shadow the live ids.
    live_templates = call(
        models,
        uid,
        "product.template",
        "search_read",
        [[("company_id", "in", [company_id, False]), ("active", "=", True)]],
        {"fields": ["id", "name", "default_code"]},
    )
    existing_by_species: dict[str, int] = {}
    for t in live_templates:
        existing_by_species.setdefault(norm_species(t["name"]), t["id"])

    print("\n── Coming-soon placeholders ──")
    created = 0
    # sku -> template id for the placeholders this run created (or found as our
    # own prior placeholder). The verify pass reads these back BY ID: a template
    # with >1 variant has product.template.default_code == False (Odoo only
    # mirrors a single variant's code up to the template), so the placeholders —
    # each carrying Bareroot+Potted variants — can never be found by default_code.
    seeded_ids: dict[str, int] = {}
    for product in PRODUCTS:
        sku = product["sku"]
        species = norm_species(product["name"])
        cats = [cat_id[c] for c in product["cats"]]

        if species in existing_by_species:
            print(
                f"  SKIP {product['name']} ({sku}) — a template of this species "
                f"already exists (id={existing_by_species[species]}); not forking a placeholder."
            )
            continue
        existing = call(
            models,
            uid,
            "product.template",
            "search",
            [[("default_code", "=", sku), ("company_id", "in", [company_id, False])]],
            {"limit": 1},
        )
        if existing:
            print(f"  = {product['name']} ({sku}) already seeded (id={existing[0]})")
            seeded_ids[sku] = existing[0]
            continue

        cat_names = " + ".join(product["cats"])
        if DRY_RUN:
            print(
                f"  + WOULD CREATE {sku} ({product['name']}) — {product['botanical']}; "
                f"cats=[{cat_names}]; published, NOT sale_ok, qty 0; Format={list(FORMAT_VALUES)}"
            )
            continue

        vals: dict[str, Any] = {
            "name": product["name"],
            "default_code": sku,
            "list_price": 0.0,  # placeholder — operator sets a price at graduation
            "categ_id": internal_cat[product["internal"]],
            "public_categ_ids": [(6, 0, cats)],
            "company_id": company_id,
            "type": "consu",
            "is_storable": True,
            "is_published": True,  # website_published -> the product page renders
            "sale_ok": False,  # excluded from /shop grid + ?cat= facets; not purchasable
            "purchase_ok": False,
            "attribute_line_ids": [
                (0, 0, {"attribute_id": format_attr, "value_ids": [(6, 0, list(format_value_ids.values()))]}),
            ],
            "grove_botanical_name": product["botanical"],
            "grove_layer": product["layer"],
        }
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        seeded_ids[sku] = tmpl_id
        print(f"  CREATE {sku} → template id={tmpl_id} ({product['botanical']}; [{cat_names}])")

        # Per-variant SKUs so a graduated placeholder already has clean codes. No
        # stock quants: qty stays 0 (unstocked), which is the point.
        variants = call(
            models,
            uid,
            "product.product",
            "search_read",
            [[("product_tmpl_id", "=", tmpl_id)]],
            {"fields": ["product_template_variant_value_ids"]},
        )
        for variant in variants:
            ptav_names = call(
                models,
                uid,
                "product.template.attribute.value",
                "read",
                [variant["product_template_variant_value_ids"]],
                {"fields": ["name"]},
            )
            fmt = next((r["name"] for r in ptav_names if r["name"] in FORMAT_VALUES), None)
            if fmt is None:
                fail(f"{sku}: variant {variant['id']} has no Format value")
            code = variant_sku(product, fmt)
            call(models, uid, "product.product", "write", [[variant["id"]], {"default_code": code}])
            print(f"    variant {code}: qty 0")
        created += 1

    # ── Verify pass ──────────────────────────────────────────────────────────
    # Read back the placeholders and assert the storefront-visibility invariant
    # that makes them "published but not purchasable and out of the facets".
    if not DRY_RUN:
        print("\n── Verify ──")
        # Read back by the template ids we seeded this run — NOT by default_code,
        # which is False on these multi-variant templates. Species-skipped rows
        # (a real product of that species already exists) are deliberately absent
        # here: they are not our placeholders, so their invariants are not ours to
        # assert. A converged re-run seeds nothing and verifies nothing.
        rows = (
            call(
                models,
                uid,
                "product.template",
                "search_read",
                [[("id", "in", list(seeded_ids.values()))]],
                {"fields": ["id", "name", "website_published", "sale_ok", "public_categ_ids"]},
            )
            if seeded_ids
            else []
        )
        by_id = {r["id"]: r for r in rows}
        problems = []
        for p in PRODUCTS:
            tmpl_id = seeded_ids.get(p["sku"])
            if tmpl_id is None:
                continue  # species-skipped this run; not a placeholder we own
            r = by_id.get(tmpl_id)
            if not r:
                problems.append(f"{p['sku']}: template id={tmpl_id} not found after seed")
                continue
            if not r["website_published"]:
                problems.append(f"{p['sku']}: website_published is False (page would not render)")
            if r["sale_ok"]:
                problems.append(f"{p['sku']}: sale_ok is True (would leak into /shop + ?cat= facets)")
            want = {cat_id[c] for c in p["cats"]}
            if set(r["public_categ_ids"]) != want:
                problems.append(f"{p['sku']}: categories {r['public_categ_ids']} != wanted {sorted(want)}")
        if problems:
            fail("Verify failed:\n  - " + "\n  - ".join(problems))
        print(f"  OK — {len(rows)} placeholders: website_published=True, sale_ok=False, categories exact.")

    print(f"\nDone. {created} created." + (" (dry run — nothing written)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
