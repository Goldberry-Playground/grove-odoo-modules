#!/usr/bin/env python3
"""Seed species-level plant products with a Cultivar x Format variant axis.

Companion to ``import_grove_catalog.py`` for the catalog shape that the CSV
importer cannot express: ONE product.template per species (Pear, Fig, ...)
whose cultivars are values of a "Cultivar" attribute carrying ``price_extra``
deltas off the base list price, crossed with a "Format" attribute (Potted /
Bareroot) that the shipping engine bills per variant. This is the storefront
model the 2026-07-13 catalog session locked and that ``grove_headless`` catalog
API v1 (``_structure_variant``) serialises:

    "cultivar": axis.get("Cultivar", ""),   # -> product page dropdown
    "format":   axis.get("Format", ""),      # -> potted/bareroot selector
    "shipping_tier": variant.grove_effective_shipping_tier,  # Format-driven

The axis names MUST be exactly ``Cultivar`` and ``Format`` — the serializer
keys on them by name, so a "Variety"/"Size" axis would come back empty and
break the cultivar selector. Storefront type browsing rides website public
categories, which ``/grove/api/v1/products?category_id=`` (and its slug twin
``?cat=<slug>``) filter on. Josh 2026-07-21 (GOL-658) locked the storefront
cat-bar taxonomy to five *use-type* categories — Fruit Trees, Berries, Nut
Trees, Fruiting Vines, Natives & Ornamentals — which are DECOUPLED from the
internal accounting category (Trees / Shrubs / Vines). ``website_category``
carries the use-type; ``internal_category`` stays the growth-habit accounting
bucket.

Also sets what the CSV importer does not: the ``grove_*`` growing-facts block
(botanical name, USDA zone range, food-forest layer, sun, mature size,
spacing, soil) that drives the product-page spec block and the zone/tag
facets, ``product.tag`` assignments, website categories (public_categ_ids —
NOT the internal categ_id; the storefront filters on public categories only),
per-variant SKUs (``PEAR-MAG-PT``), and opening on-hand quantities.

This batch is potted stock only, so every template gets a single Format value
"Potted". Bareroot siblings are added later as a second Format VALUE on the
SAME template (a normal attribute-value add) — NOT a separate template — so
``grove_effective_shipping_tier`` resolves them to the bareroot rate.

Idempotent per SPECIES within the company: if a template of the same species
name already exists (case/whitespace-insensitive — 'Serviceberry' matches the
canonical 'Service Berry'), it is skipped entirely — including its quantities —
so re-running never clobbers stock or forks a duplicate template (the GOL-641
regression, where SKU-only matching created parallel 178-182 templates). A
prior run's own default_code is still honoured as a secondary guard. Extending
an existing species (new cultivars, merged stock) is the remediation path's
job, not the seed's. To re-seed one product, archive it in Odoo first.

Usage
-----
    # Dry run (read-only: connects, reports what exists / would be created)
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/seed_variety_products.py

    # Live
    DRY_RUN unset -> creates categories/tags/attributes/products/quants.

Exit codes: 0 ok, 1 auth/data failure (fails loudly, never half-writes a
product: each template + its extras is one create call + follow-up writes).
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
SALE_TAXES = ["WV State Sales Tax 6%", "WV Municipal Tax 1%"]
CULTIVAR_ATTR = "Cultivar"
FORMAT_ATTR = "Format"
# This is the potted batch. Bareroot becomes a second Format value later.
FORMAT_VALUE = "Potted"
FORMAT_ABBR = {"Potted": "PT", "Bareroot": "BR"}

# Website categories are the storefront browse/filter layer (the /shop cat-bar,
# GOL-658): Josh's five use-type buckets — "Fruit Trees", "Berries", "Nut
# Trees", "Fruiting Vines", "Natives & Ornamentals". slugify() maps each to the
# cat-bar slug (e.g. "Natives & Ornamentals" -> "natives-ornamentals"). Internal
# categ_id stays the accounting/valuation category (growth habit).
#
# facts: the grove_* growing-facts block (2026-07-13 catalog spec). Best-effort
# horticultural values for USDA zone 6 (Appalachian WV); pending nursery
# confirmation alongside the GOL-588 pricing gate. layer must be one of
# canopy/understory/shrub/ground/vine; sun one of full/partial/shade.
#
# code: the species prefix for per-variant SKUs (PEAR-MAG-PT); cultivar.code
# is the middle segment.
#
# AUTHORITATIVE PRICING MODEL — Josh 2026-07-21 (GOL-641 comment). The earlier
# GOL-639 numbers used bareroot figures as the potted base ("lost in
# translation"); this batch is Potted-only, so list_price/base MUST be the
# POTTED column. Potted = Bareroot + $3.00, uniformly:
#     Non-grafted 1yr:            $12 bareroot / $15 potted
#     Grafted:                    $35 bareroot / $38 potted
#     Persimmon/Pawpaw (premium): $40 bareroot / $42 potted
# Encoded as: base list_price = non-grafted potted ($15); grafted cultivar
# price_extra = +$23 (-> $38); premium (persimmon IKKJ) price_extra = +$27
# (-> $42). Pear is grafted-only (no non-grafted variant) so its base IS $38.
# Named-cultivar premiums (Kiwi Fairchild, Fig LSU/Exquisito) are NOT part of
# Josh's graft rule and remain pending his confirmation — see GOL-641.
POTTED_NON_GRAFTED = 15.00
POTTED_GRAFTED = 38.00
POTTED_PREMIUM = 42.00  # persimmon, future pawpaw
# fmt: off
PRODUCTS: list[dict[str, Any]] = [
    {
        "sku": "VINE-KIWI", "code": "KIWI", "name": "Kiwi",
        "internal_category": "Vines", "website_category": "Fruiting Vines",
        "tags": ["Food Forest", "Silvopasture"],
        "list_price": POTTED_NON_GRAFTED,  # $15 potted non-grafted (was $12 bareroot)
        "facts": {
            "botanical_name": "Actinidia arguta", "zone_min": 4, "zone_max": 8,
            "layer": "vine", "sun": "partial",
            "mature_size": "20-30 ft vine", "spacing": "10-15 ft",
            "soil": "Moist, well-drained",
        },
        "cultivars": [
            {"name": "Wild", "code": "WLD", "price_extra": 0.00, "qty": 2},
            {"name": "Fairchild (male pollinator)", "code": "FCH", "price_extra": 4.00, "qty": 1},
        ],
    },
    {
        "sku": "SHRUB-FIG", "code": "FIG", "name": "Fig",
        "internal_category": "Shrubs", "website_category": "Fruit Trees",
        "tags": ["Food Forest", "Silvopasture"],
        "list_price": POTTED_NON_GRAFTED,  # $15 potted non-grafted
        "facts": {
            "botanical_name": "Ficus carica", "zone_min": 7, "zone_max": 9,
            "layer": "shrub", "sun": "full",
            "mature_size": "10-15 ft", "spacing": "10-12 ft",
            "soil": "Well-drained",
        },
        "cultivars": [
            {"name": "Wild", "code": "WLD", "price_extra": 0.00, "qty": 3},
            {"name": "LSU Champagne", "code": "LSU", "price_extra": 15.00, "qty": 1},
            {"name": "Exquisito", "code": "EXQ", "price_extra": 15.00, "qty": 1},
        ],
    },
    {
        "sku": "TREE-PEAR", "code": "PEAR", "name": "Pear",
        "internal_category": "Trees", "website_category": "Fruit Trees",
        "tags": [],
        "list_price": POTTED_GRAFTED,  # $38 potted grafted, flat (was $35 bareroot)
        "facts": {
            "botanical_name": "Pyrus communis", "zone_min": 4, "zone_max": 8,
            "layer": "canopy", "sun": "full",
            "mature_size": "15-20 ft", "spacing": "15-20 ft",
            "soil": "Deep, well-drained loam",
        },
        "cultivars": [
            {"name": "Magness", "code": "MAG", "price_extra": 0.00, "qty": 3},
            {"name": "Warren", "code": "WRN", "price_extra": 0.00, "qty": 2},
            {"name": "Improved Kieffer", "code": "KIE", "price_extra": 0.00, "qty": 1},
        ],
    },
    {
        "sku": "TREE-PERSIMMON", "code": "PERSIMMON", "name": "Persimmon",
        "internal_category": "Trees", "website_category": "Fruit Trees",
        "tags": ["Food Forest", "Silvopasture"],
        # Josh 2026-07-21 (GOL-641): persimmon is the premium exception.
        # base = non-grafted seedling potted ($15); IKKJ (grafted) -> $42 potted.
        "list_price": POTTED_NON_GRAFTED,
        "facts": {
            "botanical_name": "Diospyros kaki", "zone_min": 6, "zone_max": 9,
            "layer": "understory", "sun": "full",
            "mature_size": "10-15 ft", "spacing": "12-15 ft",
            "soil": "Well-drained",
        },
        "cultivars": [
            {"name": "Seedling", "code": "SDL", "price_extra": 0.00, "qty": 2},
            {"name": "IKKJ", "code": "IKKJ", "price_extra": POTTED_PREMIUM - POTTED_NON_GRAFTED, "qty": 3},  # +$27 -> $42
        ],
    },
    {
        "sku": "TREE-SERVICEBERRY", "code": "SERVICEBERRY", "name": "Serviceberry",
        "internal_category": "Trees", "website_category": "Berries",
        "tags": ["Wildlife", "Native", "Food Forest"],
        # Josh 2026-07-21 (GOL-641): base = non-grafted seedling potted ($15);
        # the grafted cultivar adds +$23 -> $38 potted grafted.
        "list_price": POTTED_NON_GRAFTED,
        "facts": {
            "botanical_name": "Amelanchier laevis", "zone_min": 4, "zone_max": 8,
            "layer": "understory", "sun": "partial",
            "mature_size": "15-25 ft", "spacing": "10-15 ft",
            "soil": "Moist, well-drained",
        },
        "cultivars": [
            {"name": "Seedling", "code": "SDL", "price_extra": 0.00, "qty": 2},
            {"name": "Grafted", "code": "GRF", "price_extra": POTTED_GRAFTED - POTTED_NON_GRAFTED, "qty": 3},  # +$23 -> $38
        ],
    },
    {
        # No named cultivar yet -> only the Format axis. Adding cultivars later
        # (first named cultivar) is a normal attribute-line add.
        "sku": "SHRUB-ARONIA", "code": "ARONIA", "name": "Aronia",
        "internal_category": "Shrubs", "website_category": "Berries",
        "tags": ["Wildlife", "Native"],
        "list_price": POTTED_NON_GRAFTED,  # $15 potted non-grafted shrub
        "facts": {
            "botanical_name": "Aronia melanocarpa", "zone_min": 3, "zone_max": 8,
            "layer": "shrub", "sun": "full",
            "mature_size": "3-6 ft", "spacing": "4-6 ft",
            "soil": "Adaptable; tolerates wet",
        },
        "cultivars": [],
        "qty": 2,
    },
]
# fmt: on

ALL_TAGS = sorted({t for p in PRODUCTS for t in p["tags"]})
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
    """Normalise a species name for matching against live templates.

    The live catalog carries the canonical templates (id 148-169) with names
    like 'Service Berry' and no ``default_code``. Matching on the script's own
    SKU (``TREE-SERVICEBERRY``) never hit them, so a re-run forked a parallel
    'Serviceberry' template (grove_slug auto-suffixed) — the GOL-641 dup bug.
    Collapse case and every non-alphanumeric char so 'Serviceberry' and
    'Service Berry' both key to ``serviceberry``.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def variant_sku(product: dict, cultivar: dict | None) -> str:
    """PEAR-MAG-PT — species code, optional cultivar code, format abbr."""
    parts = [product["code"]]
    if cultivar is not None:
        parts.append(cultivar["code"])
    parts.append(FORMAT_ABBR[FORMAT_VALUE])
    return "-".join(parts)


def main() -> None:
    print(f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}")
    models, uid = authenticate()

    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    ctx = {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}

    # Branch-aware tax resolution: "At The Grove Nursery" can be a *branch* of a
    # parent company and inherits the parent's sale taxes (Odoo 19 enforces
    # tax-name uniqueness across the company branch tree, so a branch cannot own a
    # duplicate). Resolve each expected tax to the nearest company in the parent
    # chain instead of requiring an exact company_id match.
    chain = []
    _cid = company_id
    while _cid:
        chain.append(_cid)
        _p = call(models, uid, "res.company", "read", [[_cid], ["parent_id"]])[0]["parent_id"]
        _cid = _p[0] if _p else False
    tax_ids = []
    for _tname in SALE_TAXES:
        _matches = call(
            models,
            uid,
            "account.tax",
            "search_read",
            [[("name", "=", _tname), ("type_tax_use", "=", "sale"), ("company_id", "in", chain)]],
            {"fields": ["id", "company_id"]},
        )
        _by = {t["company_id"][0]: t["id"] for t in _matches if t.get("company_id")}
        _chosen = next((_by[c] for c in chain if c in _by), None)
        tax_ids.append(_chosen)
    if len(tax_ids) != len(SALE_TAXES) or any(t is None for t in tax_ids):
        fail(f"Expected sale taxes {SALE_TAXES} for company {company_id} (parent chain {chain}), found ids={tax_ids}")

    print("\n── Prerequisites ──")
    tag_ids: dict[str, int] = {}
    for tag in ALL_TAGS:
        tag_ids[tag] = find_or_create(models, uid, "product.tag", [("name", "=", tag)], {"name": tag}, tag)

    # Internal (accounting) category for Vines; Trees/Shrubs are module-seeded.
    plants = call(models, uid, "product.category", "search", [[("name", "=", "Plants")]], {"limit": 1})
    internal_cat: dict[str, int] = {}
    for name in sorted({p["internal_category"] for p in PRODUCTS}):
        vals = {"name": name}
        if plants:
            vals["parent_id"] = plants[0]
        internal_cat[name] = find_or_create(
            models, uid, "product.category", [("name", "=", name)], vals, f"internal:{name}"
        )

    website_cat: dict[str, int] = {}
    for name in sorted({p["website_category"] for p in PRODUCTS}):
        website_cat[name] = find_or_create(
            models, uid, "product.public.category", [("name", "=", name)], {"name": name}, f"website:{name}"
        )

    # Cultivar + Format axes. The serializer keys on these names exactly.
    cultivar_attr = find_or_create(
        models,
        uid,
        "product.attribute",
        [("name", "=", CULTIVAR_ATTR)],
        {"name": CULTIVAR_ATTR, "display_type": "select", "create_variant": "always"},
        CULTIVAR_ATTR,
    )
    format_attr = find_or_create(
        models,
        uid,
        "product.attribute",
        [("name", "=", FORMAT_ATTR)],
        {"name": FORMAT_ATTR, "display_type": "radio", "create_variant": "always"},
        FORMAT_ATTR,
    )
    format_value_id = find_or_create(
        models,
        uid,
        "product.attribute.value",
        [("name", "=", FORMAT_VALUE), ("attribute_id", "=", format_attr)],
        {"name": FORMAT_VALUE, "attribute_id": format_attr},
        f"{FORMAT_ATTR}:{FORMAT_VALUE}",
    )

    # Stock location: the company warehouse's main stock location.
    wh = call(
        models,
        uid,
        "stock.warehouse",
        "search_read",
        [[("company_id", "=", company_id)]],
        {"fields": ["lot_stock_id"], "limit": 1},
    )
    if not wh:
        fail(f"No warehouse for company {company_id}")
    stock_location_id = wh[0]["lot_stock_id"][0]

    # Species-name index of every live template in this company. The seed must
    # match the CANONICAL template by species (148-169) — not by its own SKU —
    # or it forks a duplicate on every re-run (GOL-641). We do not mutate a
    # canonical template here: extending an existing species (adding cultivars,
    # merging stock) is the dedicated remediation path, not the seed's job.
    live_templates = call(
        models,
        uid,
        "product.template",
        "search_read",
        [[("company_id", "in", [company_id, False])]],
        {"fields": ["id", "name", "default_code"], "context": {"active_test": False}},
    )
    existing_by_species: dict[str, int] = {}
    for t in live_templates:
        existing_by_species.setdefault(norm_species(t["name"]), t["id"])

    print("\n── Products ──")
    for product in PRODUCTS:
        sku = product["sku"]
        species = norm_species(product["name"])
        if species in existing_by_species:
            print(
                f"  SKIP {product['name']} ({sku}) — canonical template already "
                f"exists (id={existing_by_species[species]}); not forking a "
                f"duplicate. Extend it via the remediation path, not the seed."
            )
            continue
        # Belt-and-suspenders: a prior run of THIS script (matched by its own
        # SKU) — keeps the seed idempotent for species it legitimately created.
        existing = call(
            models,
            uid,
            "product.template",
            "search",
            [[("default_code", "=", sku), ("company_id", "in", [company_id, False])]],
            {"limit": 1},
        )
        if existing:
            # Idempotent for stock/price (never re-touch quantities that may have
            # moved), but DO reconcile the storefront category so a taxonomy change
            # (GOL-658: Trees/Shrubs/Vines -> Josh's use-type buckets) lands on
            # products seeded by an earlier run without archiving them. This is a
            # cheap read-then-set: only writes when the category actually differs.
            want_cat = website_cat[product["website_category"]]
            cur = call(models, uid, "product.template", "read", [[existing[0]], ["public_categ_ids"]])[0]
            if cur["public_categ_ids"] == [want_cat]:
                print(f"  SKIP {sku} — already exists (id={existing[0]}); category '{product['website_category']}' ok")
            elif DRY_RUN:
                print(f"  ~ WOULD RECATEGORIZE {sku} (id={existing[0]}) -> '{product['website_category']}'")
            else:
                call(
                    models,
                    uid,
                    "product.template",
                    "write",
                    [[existing[0]], {"public_categ_ids": [(6, 0, [want_cat])]}],
                )
                print(f"  ~ RECATEGORIZED {sku} (id={existing[0]}) -> '{product['website_category']}'")
            continue

        cultivars = product["cultivars"]
        base = product["list_price"]
        plan = (
            ", ".join(f"{c['name']} ${base + c['price_extra']:.0f}×{c['qty']}" for c in cultivars)
            or f"${base:.0f}×{product['qty']}"
        )
        if DRY_RUN:
            tags = product["tags"] or "—"
            print(f"  + WOULD CREATE {sku} ({product['name']}) [{FORMAT_VALUE}]: {plan}; tags={tags}")
            continue

        cultivar_value_ids: dict[str, int] = {}
        for c in cultivars:
            cultivar_value_ids[c["name"]] = find_or_create(
                models,
                uid,
                "product.attribute.value",
                [("name", "=", c["name"]), ("attribute_id", "=", cultivar_attr)],
                {"name": c["name"], "attribute_id": cultivar_attr},
                f"{CULTIVAR_ATTR}:{c['name']}",
            )

        # Every template carries the Format axis; the Cultivar axis only when
        # there are named cultivars.
        attribute_lines = [(0, 0, {"attribute_id": format_attr, "value_ids": [(6, 0, [format_value_id])]})]
        if cultivars:
            attribute_lines.append(
                (0, 0, {"attribute_id": cultivar_attr, "value_ids": [(6, 0, list(cultivar_value_ids.values()))]})
            )

        vals: dict[str, Any] = {
            "name": product["name"],
            "default_code": sku,
            "list_price": base,
            "categ_id": internal_cat[product["internal_category"]],
            "public_categ_ids": [(6, 0, [website_cat[product["website_category"]]])],
            "product_tag_ids": [(6, 0, [tag_ids[t] for t in product["tags"]])],
            "company_id": company_id,
            "type": "consu",
            "is_storable": True,
            "is_published": True,
            "sale_ok": True,
            "purchase_ok": True,
            "taxes_id": [(6, 0, tax_ids)],
            "attribute_line_ids": attribute_lines,
            # grove_shipping_tier stays at its default ("potted"). The Format
            # axis drives grove_effective_shipping_tier per variant, so a later
            # "Bareroot" Format value quotes the bareroot rate automatically.
            **{f"grove_{f}": product["facts"][f] for f in FACT_FIELDS},
        }
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        print(f"  CREATE {sku} → template id={tmpl_id} ({plan})")

        # price_extra lives on product.template.attribute.value (per-template).
        for c in cultivars:
            if not c["price_extra"]:
                continue
            ptav = call(
                models,
                uid,
                "product.template.attribute.value",
                "search",
                [
                    [
                        ("product_tmpl_id", "=", tmpl_id),
                        ("product_attribute_value_id", "=", cultivar_value_ids[c["name"]]),
                    ]
                ],
            )
            call(models, uid, "product.template.attribute.value", "write", [ptav, {"price_extra": c["price_extra"]}])
            print(f"    price_extra {c['name']}: +${c['price_extra']:.2f}")

        # Per-variant SKU + opening quantity: one quant per variant in the
        # company warehouse.
        variants = call(
            models,
            uid,
            "product.product",
            "search_read",
            [[("product_tmpl_id", "=", tmpl_id)]],
            {"fields": ["product_template_variant_value_ids", "display_name"]},
        )
        for variant in variants:
            if cultivars:
                ptav_names = call(
                    models,
                    uid,
                    "product.template.attribute.value",
                    "read",
                    [variant["product_template_variant_value_ids"]],
                    {"fields": ["name"]},
                )
                names = {r["name"] for r in ptav_names}
                match = next((c for c in cultivars if c["name"] in names), None)
                if match is None:
                    fail(f"{sku}: variant {variant['id']} matches no cultivar in {names}")
                qty = match["qty"]
            else:
                match = None
                qty = product["qty"]
            variant_code = variant_sku(product, match)
            call(models, uid, "product.product", "write", [[variant["id"]], {"default_code": variant_code}])
            quant_id = call(
                models,
                uid,
                "stock.quant",
                "create",
                [{"product_id": variant["id"], "location_id": stock_location_id, "inventory_quantity": qty}],
                {"context": {"inventory_mode": True, **ctx["context"]}},
            )
            # action_apply_inventory returns None, which Odoo's XML-RPC marshaller
            # rejects ("cannot marshal None unless allow_none is enabled"). Use
            # JSON-RPC for this one call: it serialises null cleanly and still
            # commits server-side.
            import json as _json
            import urllib.request as _ureq

            _payload = {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "service": "object",
                    "method": "execute_kw",
                    "args": [ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "action_apply_inventory", [[quant_id]], ctx],
                },
            }
            _r = _json.loads(
                _ureq.urlopen(
                    _ureq.Request(
                        f"{ODOO_URL}/jsonrpc",
                        data=_json.dumps(_payload).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=30,
                ).read()
            )
            if _r.get("error"):
                fail(f"action_apply_inventory jsonrpc error: {_r['error']}")
            print(f"    variant {variant_code}: {qty} @ location {stock_location_id}")

    print("\nDone." + (" (dry run — nothing written)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
