#!/usr/bin/env python3
"""Seed sample Kit-type Bills of Materials for bundled Grove products.

A Kit BOM (mrp.bom with type='phantom') lets the storefront sell a single
line item ("Spring Fruit Tree Starter Crate") while inventory + delivery
break it down into the component variants. Customer experience stays
simple; warehouse picking and accounting stay accurate.

Idempotent: re-running skips kits whose parent product `default_code`
already exists.

Usage:
    ODOO_PASSWORD=... python3 seed_kit_boms.py
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

# Each kit is a parent product + list of component variants identified by
# their seeded SKU (default_code). Component qty defaults to 1.
#
# Component SKUs MUST match products seeded by seed_sample_products.py.
# If a referenced SKU isn't installed yet, the seeder skips the kit with
# a clear warning rather than half-creating it.
KITS: list[dict[str, Any]] = [
    {
        "sku": "KIT-SPRING-FRUIT-CRATE",
        "name": "Spring Fruit Tree Starter Crate",
        "category": "Mixed",
        "list_price": 225.00,
        "description_sale": (
            "A curated crate to plant a small backyard orchard in one weekend. "
            "Includes one apple, one peach equivalent (river birch is a "
            "placeholder stand-in for now), and three flowering shrubs to "
            "anchor the planting. All trees ship in 3-gallon nursery pots."
        ),
        "components": [
            # Each entry: (component default_code, quantity)
            ("TREE-HONEYCRISP", 1),
            ("TREE-RIVER-BIRCH", 1),
            ("SHRUB-BOXWOOD-GV", 1),
            ("SHRUB-FORSYTHIA-LG", 1),
            ("MIXED-CONCORD-GRAPE", 1),
        ],
    },
]


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


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def lookup_id(models, uid, model, domain, label):
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if not ids:
        fail(f"Could not find {model} matching {domain} ({label})")
    return ids[0]


def find_variant_by_sku(models, uid, sku: str) -> int | None:
    """Look up a product.product (variant) by template default_code.

    Returns the product_variant_id of the matched template, or None if
    no template has that SKU.
    """
    template_ids = call(models, uid, "product.template", "search", [[("default_code", "=", sku)]], {"limit": 1})
    if not template_ids:
        return None
    template = call(models, uid, "product.template", "read", [template_ids], {"fields": ["product_variant_id"]})[0]
    variant_ref = template.get("product_variant_id")
    if not variant_ref:
        return None
    return variant_ref[0] if isinstance(variant_ref, list) else variant_ref


def seed_kit(models, uid, company_id, kit):
    sku = kit["sku"]

    # Skip if the parent kit product already exists.
    existing_parent = call(models, uid, "product.template", "search", [[("default_code", "=", sku)]], {"limit": 1})
    if existing_parent:
        print(f"  SKIP {sku} ({kit['name']}) — parent product already exists")
        return

    # Verify every component is installed before creating anything.
    component_variants: list[tuple[int, float]] = []
    missing: list[str] = []
    for component_sku, qty in kit["components"]:
        variant_id = find_variant_by_sku(models, uid, component_sku)
        if variant_id is None:
            missing.append(component_sku)
        else:
            component_variants.append((variant_id, float(qty)))
    if missing:
        print(f"  SKIP {sku} — missing component SKUs: {missing}. Run seed_sample_products.py first.")
        return

    # Create the parent product.
    category_id = lookup_id(models, uid, "product.category", [("name", "=", kit["category"])], kit["category"])
    parent_vals = {
        "name": kit["name"],
        "default_code": sku,
        "list_price": kit["list_price"],
        "description_sale": kit["description_sale"],
        "categ_id": category_id,
        "company_id": company_id,
        "type": "consu",
        "is_published": True,
        "sale_ok": True,
        "purchase_ok": False,  # we don't buy kits, we assemble them
    }
    parent_template_id = call(models, uid, "product.template", "create", [parent_vals])
    parent_variant_id = find_variant_by_sku(models, uid, sku)
    if parent_variant_id is None:
        fail(f"Created kit {sku} but couldn't resolve its variant id — data integrity issue")

    # Create the BOM with type=phantom (Kit). Customers see one line on
    # the order; Odoo expands to component lines for inventory + delivery.
    bom_vals = {
        "product_tmpl_id": parent_template_id,
        "product_id": parent_variant_id,
        "type": "phantom",
        "product_qty": 1.0,
        "company_id": company_id,
        "bom_line_ids": [
            (0, 0, {"product_id": variant_id, "product_qty": qty}) for variant_id, qty in component_variants
        ],
    }
    bom_id = call(models, uid, "mrp.bom", "create", [bom_vals])

    print(
        f"  CREATE {sku} → product.template id={parent_template_id}, "
        f"mrp.bom id={bom_id}, {len(component_variants)} components"
    )


def main():
    models, uid = authenticate()
    company_id = lookup_id(models, uid, "res.company", [("name", "=", "Goldberry Grove Farm")], "Goldberry Grove Farm")
    print(f"Target company_id={company_id} (Goldberry Grove Farm)\n")
    print(f"Seeding {len(KITS)} kit BOM(s):")
    for kit in KITS:
        seed_kit(models, uid, company_id, kit)
    print("\nDone.")


if __name__ == "__main__":
    main()
