#!/usr/bin/env python3
"""Seed species-level products with a Variety variant axis into Odoo.

Companion to ``import_grove_catalog.py`` for the catalog shape that CSV
importer cannot express: ONE product.template per species (Pear, Fig, ...)
whose cultivars are values of a "Variety" attribute carrying ``price_extra``
deltas off the base (wild) list price. This is the storefront model the
2026-07-13 catalog session locked: the species page shows a cultivar
dropdown (grove_headless already serialises per-variant name + lst_price);
Type browsing (Trees / Shrubs / Vines) rides website categories, which the
``/grove/api/v1/products?category_id=`` filter already supports.

Also sets what the CSV importer does not: product.tag assignments
(Native / Food Forest / Wildlife / Silvopasture), website categories
(public_categ_ids — NOT the internal categ_id; the storefront filters on
public categories only), and opening on-hand quantities per variant.

Idempotent per SKU: an existing (sku, company) template is skipped
entirely — including its quantities — so re-running never clobbers stock
that has since moved. To re-seed one product, archive it in Odoo first.

Usage
-----
    # Dry run (read-only: connects, reports what exists / would be created)
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/seed_variety_products.py

    # Live
    DRY_RUN unset → creates categories/tags/attribute/products/quants.

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
VARIETY_ATTR = "Variety"
SIZE_ATTR = "Size"
SIZE_VALUE = "3 gal"

# Website categories are the storefront browse/filter layer (Layer 1: Type).
# Internal categ_id stays the accounting/valuation category.
# fmt: off
PRODUCTS: list[dict[str, Any]] = [
    {
        "sku": "VINE-KIWI", "name": "Kiwi",
        "internal_category": "Vines", "website_category": "Vines",
        "tags": ["Food Forest", "Silvopasture"],
        "list_price": 12.00,  # base = wild type
        "varieties": [
            {"name": "Wild", "price_extra": 0.00, "qty": 2},
            {"name": "Fairchild (male pollinator)", "price_extra": 4.00, "qty": 1},
        ],
    },
    {
        "sku": "SHRUB-FIG", "name": "Fig",
        "internal_category": "Shrubs", "website_category": "Shrubs",
        "tags": ["Food Forest", "Silvopasture"],
        "list_price": 15.00,  # base = wild fig
        "varieties": [
            {"name": "Wild", "price_extra": 0.00, "qty": 3},
            {"name": "LSU Champagne", "price_extra": 15.00, "qty": 1},
            {"name": "Exquisito", "price_extra": 15.00, "qty": 1},
        ],
    },
    {
        "sku": "TREE-PEAR", "name": "Pear",
        "internal_category": "Trees", "website_category": "Trees",
        "tags": [],
        "list_price": 35.00,  # all grafted cultivars, flat price
        "varieties": [
            {"name": "Magness", "price_extra": 0.00, "qty": 3},
            {"name": "Warren", "price_extra": 0.00, "qty": 2},
            {"name": "Improved Kieffer", "price_extra": 0.00, "qty": 1},
        ],
    },
    {
        "sku": "TREE-PERSIMMON", "name": "Persimmon",
        "internal_category": "Trees", "website_category": "Trees",
        "tags": ["Food Forest", "Silvopasture"],
        "list_price": 40.00,
        "varieties": [
            {"name": "IKKJ", "price_extra": 0.00, "qty": 3},
        ],
    },
    {
        "sku": "TREE-SERVICEBERRY", "name": "Serviceberry",
        "internal_category": "Trees", "website_category": "Trees",
        "tags": ["Wildlife", "Native", "Food Forest"],
        "list_price": 35.00,
        "varieties": [
            {"name": "Grafted", "price_extra": 0.00, "qty": 3},
        ],
    },
    {
        # No named cultivar yet → plain product, no Variety axis. Adding the
        # axis later (first named cultivar) is a normal attribute-line add.
        "sku": "SHRUB-ARONIA", "name": "Aronia",
        "internal_category": "Shrubs", "website_category": "Shrubs",
        "tags": ["Wildlife", "Native"],
        "list_price": 15.00,
        "varieties": [],
        "qty": 2,
    },
]
# fmt: on

ALL_TAGS = sorted({t for p in PRODUCTS for t in p["tags"]})


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


def main() -> None:
    print(f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}")
    models, uid = authenticate()

    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    ctx = {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}

    tax_ids = call(
        models,
        uid,
        "account.tax",
        "search",
        [[("name", "in", SALE_TAXES), ("type_tax_use", "=", "sale"), ("company_id", "=", company_id)]],
    )
    if len(tax_ids) != len(SALE_TAXES):
        fail(f"Expected sale taxes {SALE_TAXES} for company {company_id}, found ids={tax_ids}")

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

    variety_attr = find_or_create(
        models,
        uid,
        "product.attribute",
        [("name", "=", VARIETY_ATTR)],
        {"name": VARIETY_ATTR, "display_type": "select", "create_variant": "always"},
        VARIETY_ATTR,
    )
    size_attr_ids = call(models, uid, "product.attribute", "search", [[("name", "=", SIZE_ATTR)]], {"limit": 1})
    size_val_ids = call(
        models,
        uid,
        "product.attribute.value",
        "search",
        [[("name", "=", SIZE_VALUE), ("attribute_id", "in", size_attr_ids)]],
        {"limit": 1},
    )
    if not (size_attr_ids and size_val_ids):
        fail(f"Seeded attribute '{SIZE_ATTR}' / value '{SIZE_VALUE}' not found — is grove_headless installed?")

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

    print("\n── Products ──")
    for product in PRODUCTS:
        sku = product["sku"]
        existing = call(
            models,
            uid,
            "product.template",
            "search",
            [[("default_code", "=", sku), ("company_id", "in", [company_id, False])]],
            {"limit": 1},
        )
        if existing:
            print(f"  SKIP {sku} — already exists (id={existing[0]}); quantities untouched")
            continue

        varieties = product["varieties"]
        base = product["list_price"]
        plan = (
            ", ".join(f"{v['name']} ${base + v['price_extra']:.0f}×{v['qty']}" for v in varieties)
            or f"${base:.0f}×{product['qty']}"
        )
        if DRY_RUN:
            print(f"  + WOULD CREATE {sku} ({product['name']}): {plan}; tags={product['tags'] or '—'}")
            continue

        variety_value_ids: dict[str, int] = {}
        for v in varieties:
            variety_value_ids[v["name"]] = find_or_create(
                models,
                uid,
                "product.attribute.value",
                [("name", "=", v["name"]), ("attribute_id", "=", variety_attr)],
                {"name": v["name"], "attribute_id": variety_attr},
                f"{VARIETY_ATTR}:{v['name']}",
            )

        attribute_lines = [(0, 0, {"attribute_id": size_attr_ids[0], "value_ids": [(6, 0, size_val_ids)]})]
        if varieties:
            attribute_lines.append(
                (0, 0, {"attribute_id": variety_attr, "value_ids": [(6, 0, list(variety_value_ids.values()))]})
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
            # grove_shipping_tier stays at its default ("potted") — this whole
            # batch is potted stock; bareroot siblings become separate SKUs.
        }
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        print(f"  CREATE {sku} → template id={tmpl_id} ({plan})")

        # price_extra lives on product.template.attribute.value (per-template).
        for v in varieties:
            if not v["price_extra"]:
                continue
            ptav = call(
                models,
                uid,
                "product.template.attribute.value",
                "search",
                [
                    [
                        ("product_tmpl_id", "=", tmpl_id),
                        ("product_attribute_value_id", "=", variety_value_ids[v["name"]]),
                    ]
                ],
            )
            call(models, uid, "product.template.attribute.value", "write", [ptav, {"price_extra": v["price_extra"]}])
            print(f"    price_extra {v['name']}: +${v['price_extra']:.2f}")

        # Opening quantities: one quant per variant in the company warehouse.
        variants = call(
            models,
            uid,
            "product.product",
            "search_read",
            [[("product_tmpl_id", "=", tmpl_id)]],
            {"fields": ["product_template_variant_value_ids", "display_name"]},
        )
        for variant in variants:
            if varieties:
                ptav_names = call(
                    models,
                    uid,
                    "product.template.attribute.value",
                    "read",
                    [variant["product_template_variant_value_ids"]],
                    {"fields": ["name"]},
                )
                names = {r["name"] for r in ptav_names}
                match = next((v for v in varieties if v["name"] in names), None)
                if match is None:
                    fail(f"{sku}: variant {variant['id']} matches no variety in {names}")
                qty = match["qty"]
            else:
                qty = product["qty"]
            quant_id = call(
                models,
                uid,
                "stock.quant",
                "create",
                [{"product_id": variant["id"], "location_id": stock_location_id, "inventory_quantity": qty}],
                {"context": {"inventory_mode": True, **ctx["context"]}},
            )
            call(models, uid, "stock.quant", "action_apply_inventory", [[quant_id]], ctx)
            print(f"    stock {variant['display_name']}: {qty} @ location {stock_location_id}")

    print("\nDone." + (" (dry run — nothing written)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
