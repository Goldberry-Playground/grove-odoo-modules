#!/usr/bin/env python3
"""Seed representative Goldberry products into Odoo via XML-RPC.

Idempotent: re-running skips products whose `default_code` (SKU) already exists.

Usage:
    ODOO_DB=Goldberry \
    ODOO_USER=josh@goldberrygrove.farm \
    ODOO_PASSWORD=<your-admin-password> \
    python3 seed_sample_products.py
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

# Each product templates becomes one product.template row.
# `variants` is a list of (Size, Container) tuples; each tuple becomes one
# product.product row via the Size + Container attribute lines.
PRODUCTS: list[dict[str, Any]] = [
    {
        "sku": "TREE-HONEYCRISP",
        "name": "Honeycrisp Apple Tree",
        "category": "Trees",
        "list_price": 38.00,
        "description_sale": (
            "Cold-hardy semi-dwarf apple tree, prized for sweet-tart fruit and "
            "exceptional storage life. Ripens late September. Pollinator required."
        ),
        "variants": [("3 gal", "Nursery Pot"), ("5 gal", "Nursery Pot"), ("5 gal", "Burlap Ball")],
    },
    {
        "sku": "TREE-RIVER-BIRCH",
        "name": "Heritage River Birch",
        "category": "Trees",
        "list_price": 65.00,
        "description_sale": (
            "Fast-growing native shade tree with striking exfoliating cinnamon bark. "
            "Tolerates wet soils. Reaches 40-70ft at maturity."
        ),
        "variants": [("5 gal", "Nursery Pot"), ("10 gal", "Nursery Pot"), ("10 gal", "Burlap Ball")],
    },
    {
        "sku": "SHRUB-BOXWOOD-GV",
        "name": "Boxwood 'Green Velvet'",
        "category": "Shrubs",
        "list_price": 24.00,
        "description_sale": (
            "Compact evergreen shrub with dense rich-green foliage. Excellent for "
            "low hedges and formal plantings. Holds color through winter. 3ft x 3ft."
        ),
        "variants": [("1 gal", "Nursery Pot"), ("3 gal", "Nursery Pot"), ("3 gal", "Ceramic Pot")],
    },
    {
        "sku": "SHRUB-FORSYTHIA-LG",
        "name": "Forsythia 'Lynwood Gold'",
        "category": "Shrubs",
        "list_price": 18.00,
        "description_sale": (
            "Vigorous deciduous shrub blanketed in golden-yellow flowers in early spring. "
            "Reliable harbinger of the growing season. 6-8ft x 6-8ft."
        ),
        "variants": [("1 gal", "Nursery Pot"), ("3 gal", "Nursery Pot")],
    },
    {
        "sku": "MIXED-CONCORD-GRAPE",
        "name": "Concord Grape Vine",
        "category": "Mixed",
        "list_price": 22.00,
        "description_sale": (
            "Classic American slip-skin grape — the jam, juice, and jelly grape. "
            "Vigorous, cold-hardy, productive. Self-fertile. Trellis required."
        ),
        "variants": [("Bare Root", "Bare Root"), ("3 gal", "Nursery Pot")],
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


def call(
    models: xmlrpc.client.ServerProxy, uid: int, model: str, method: str, args: list, kwargs: dict | None = None
) -> Any:
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def lookup_id(models: xmlrpc.client.ServerProxy, uid: int, model: str, domain: list, label: str) -> int:
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if not ids:
        fail(f"Could not find {model} matching {domain} ({label})")
    return ids[0]


def get_attribute_value_id(models: xmlrpc.client.ServerProxy, uid: int, attr_name: str, value_name: str) -> int:
    attr_id = lookup_id(models, uid, "product.attribute", [("name", "=", attr_name)], attr_name)
    return lookup_id(
        models,
        uid,
        "product.attribute.value",
        [("attribute_id", "=", attr_id), ("name", "=", value_name)],
        f"{attr_name}/{value_name}",
    )


def build_attribute_lines(models: xmlrpc.client.ServerProxy, uid: int, variants: list[tuple[str, str]]) -> list[tuple]:
    """Convert variant tuples to product.template.attribute.line commands."""
    sizes = sorted({size for size, _ in variants})
    containers = sorted({container for _, container in variants})

    size_attr_id = lookup_id(models, uid, "product.attribute", [("name", "=", "Size")], "Size")
    container_attr_id = lookup_id(models, uid, "product.attribute", [("name", "=", "Container")], "Container")

    size_value_ids = [get_attribute_value_id(models, uid, "Size", s) for s in sizes]
    container_value_ids = [get_attribute_value_id(models, uid, "Container", c) for c in containers]

    return [
        (0, 0, {"attribute_id": size_attr_id, "value_ids": [(6, 0, size_value_ids)]}),
        (0, 0, {"attribute_id": container_attr_id, "value_ids": [(6, 0, container_value_ids)]}),
    ]


def seed_product(models: xmlrpc.client.ServerProxy, uid: int, company_id: int, product: dict[str, Any]) -> None:
    sku = product["sku"]
    existing = call(models, uid, "product.template", "search", [[("default_code", "=", sku)]], {"limit": 1})
    if existing:
        print(f"  SKIP {sku} — already exists (id={existing[0]})")
        return

    category_id = lookup_id(
        models,
        uid,
        "product.category",
        [("name", "=", product["category"])],
        product["category"],
    )

    vals = {
        "name": product["name"],
        "default_code": sku,
        "list_price": product["list_price"],
        "description_sale": product["description_sale"],
        "categ_id": category_id,
        "company_id": company_id,
        "type": "consu",
        "is_published": True,
        "sale_ok": True,
        "purchase_ok": True,
        "attribute_line_ids": build_attribute_lines(models, uid, product["variants"]),
    }

    new_id = call(models, uid, "product.template", "create", [vals])
    print(f"  CREATE {sku} → product.template id={new_id} ({product['name']})")


def main() -> None:
    models, uid = authenticate()
    company_id = lookup_id(
        models,
        uid,
        "res.company",
        [("name", "=", "Goldberry Grove Farm")],
        "Goldberry Grove Farm",
    )
    print(f"Target company_id={company_id} (Goldberry Grove Farm)\n")
    print(f"Seeding {len(PRODUCTS)} products:")
    for product in PRODUCTS:
        seed_product(models, uid, company_id, product)
    print("\nDone.")


if __name__ == "__main__":
    main()
