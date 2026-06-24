#!/usr/bin/env python3
"""Import the real At The Grove Nursery catalog into Odoo from a CSV file.

This is the production sibling of ``seed_sample_products.py``. Where the seed
script hard-codes five demo products, this one loads whatever Josh puts in a
spreadsheet — so the nursery catalog can be populated by editing a CSV instead
of clicking through the Odoo product UI.

Idempotent: re-running skips any SKU (``default_code``) that already exists, so
it is safe to append rows to the CSV and re-run to load only the new items.

CSV format (see ``scripts/nursery_catalog_template.csv``)
--------------------------------------------------------
One row per *variant*. Rows sharing a ``sku`` are grouped into a single
``product.template`` whose variants are the (size, container) combinations:

    sku,name,category,list_price,size,container,description_sale

- ``sku``              required, unique per product (e.g. TREE-HONEYCRISP)
- ``name``             required, product display name
- ``category``         required, must be one of the seeded product categories
                       (Trees, Shrubs, Hedging, Root Stock, Mixed)
- ``list_price``       required, decimal USD (taken from the first row per SKU)
- ``size``             optional, e.g. "3 gal", "Bare Root" (variant axis)
- ``container``        optional, e.g. "Nursery Pot", "Burlap Ball" (variant axis)
- ``description_sale`` optional, customer-facing description

A product with no size/container rows is created as a single (no-variant)
product. ``name``/``list_price``/``description_sale``/``category`` are taken
from the first row seen for each SKU; later rows only contribute variants.

Usage
-----
    ODOO_URL=http://localhost:8069 \
    ODOO_DB=Goldberry \
    ODOO_USER=josh@goldberrygrove.farm \
    ODOO_PASSWORD=<admin-password> \
    python3 import_nursery_catalog.py path/to/nursery_catalog.csv

Optional env:
    ODOO_COMPANY     target res.company name (default "At The Grove Nursery")
    NURSERY_TAXES    comma-separated sale-tax names to set on each product
                     (default "WV State Sales Tax 6%,WV Municipal Tax 1%").
                     Tax assignment is best-effort: if a tax is missing or is
                     scoped to a different company, the product is still created
                     and a warning is printed. (See GOL-4 — the WV taxes are
                     currently scoped to Goldberry, not the nursery company.)
    DRY_RUN          if "1", parse + validate the CSV and report what *would*
                     be created without writing anything to Odoo.
"""

from __future__ import annotations

import csv
import os
import sys
import xmlrpc.client
from collections import OrderedDict
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
ODOO_COMPANY = os.getenv("ODOO_COMPANY", "At The Grove Nursery")
NURSERY_TAXES = [
    t.strip()
    for t in os.getenv(
        "NURSERY_TAXES", "WV State Sales Tax 6%,WV Municipal Tax 1%"
    ).split(",")
    if t.strip()
]
DRY_RUN = os.getenv("DRY_RUN") == "1"

REQUIRED_COLUMNS = {"sku", "name", "category", "list_price"}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"  WARN: {msg}")


# ── CSV parsing ──────────────────────────────────────────────────────────────


def parse_catalog(path: str) -> "OrderedDict[str, dict[str, Any]]":
    """Read the CSV into an ordered {sku: product-dict} map.

    Validation is strict and fails fast on malformed rows so a typo in the
    spreadsheet never silently creates a junk product.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                fail(f"{path} is empty (no header row)")
            header = {c.strip() for c in reader.fieldnames}
            missing = REQUIRED_COLUMNS - header
            if missing:
                fail(f"CSV is missing required columns: {sorted(missing)}")
            rows = list(reader)
    except FileNotFoundError:
        fail(f"CSV file not found: {path}")

    products: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for i, raw in enumerate(rows, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        sku = row.get("sku", "")
        if not sku:
            # Blank line / trailing comma row — skip quietly.
            if not any(row.values()):
                continue
            fail(f"row {i}: missing sku")
        name = row.get("name", "")
        category = row.get("category", "")
        price_raw = row.get("list_price", "")
        if not name:
            fail(f"row {i} (sku={sku}): missing name")
        if not category:
            fail(f"row {i} (sku={sku}): missing category")
        try:
            list_price = float(price_raw)
        except ValueError:
            fail(f"row {i} (sku={sku}): list_price '{price_raw}' is not a number")

        size = row.get("size", "")
        container = row.get("container", "")

        if sku not in products:
            products[sku] = {
                "sku": sku,
                "name": name,
                "category": category,
                "list_price": list_price,
                "description_sale": row.get("description_sale", ""),
                "variants": [],
            }
        else:
            existing = products[sku]
            if existing["name"] != name:
                warn(
                    f"row {i} (sku={sku}): name '{name}' differs from "
                    f"'{existing['name']}' seen earlier; keeping the first."
                )

        # A variant axis is only meaningful if at least one of size/container
        # is present. Pure-duplicate (size, container) pairs are de-duped.
        if size or container:
            pair = (size, container)
            if pair not in products[sku]["variants"]:
                products[sku]["variants"].append(pair)

    return products


# ── Odoo plumbing (mirrors seed_sample_products.py) ──────────────────────────


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
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    method: str,
    args: list,
    kwargs: dict | None = None,
) -> Any:
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {}
    )


def lookup_id(
    models: xmlrpc.client.ServerProxy, uid: int, model: str, domain: list, label: str
) -> int:
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if not ids:
        fail(f"Could not find {model} matching {domain} ({label})")
    return ids[0]


def get_attribute_value_id(
    models: xmlrpc.client.ServerProxy, uid: int, attr_name: str, value_name: str
) -> int:
    attr_id = lookup_id(
        models, uid, "product.attribute", [("name", "=", attr_name)], attr_name
    )
    return lookup_id(
        models,
        uid,
        "product.attribute.value",
        [("attribute_id", "=", attr_id), ("name", "=", value_name)],
        f"{attr_name}/{value_name}",
    )


def build_attribute_lines(
    models: xmlrpc.client.ServerProxy, uid: int, variants: list[tuple[str, str]]
) -> list[tuple]:
    """Convert variant tuples to product.template.attribute.line commands.

    Empty axis values are dropped so a product that only varies by Size (and
    leaves Container blank) doesn't create a bogus empty Container attribute.
    """
    sizes = sorted({size for size, _ in variants if size})
    containers = sorted({container for _, container in variants if container})

    lines: list[tuple] = []
    if sizes:
        size_attr_id = lookup_id(
            models, uid, "product.attribute", [("name", "=", "Size")], "Size"
        )
        size_value_ids = [
            get_attribute_value_id(models, uid, "Size", s) for s in sizes
        ]
        lines.append(
            (0, 0, {"attribute_id": size_attr_id, "value_ids": [(6, 0, size_value_ids)]})
        )
    if containers:
        container_attr_id = lookup_id(
            models, uid, "product.attribute", [("name", "=", "Container")], "Container"
        )
        container_value_ids = [
            get_attribute_value_id(models, uid, "Container", c) for c in containers
        ]
        lines.append(
            (
                0,
                0,
                {
                    "attribute_id": container_attr_id,
                    "value_ids": [(6, 0, container_value_ids)],
                },
            )
        )
    return lines


def resolve_sale_taxes(
    models: xmlrpc.client.ServerProxy, uid: int, company_id: int
) -> list[int]:
    """Best-effort lookup of the configured WV sale taxes for this company.

    Returns the tax ids that exist AND are usable by ``company_id``. Missing or
    cross-company taxes are warned about, not fatal — go-live shouldn't be
    blocked on the tax wiring, which is tracked separately in GOL-4.
    """
    tax_ids: list[int] = []
    for name in NURSERY_TAXES:
        ids = call(
            models,
            uid,
            "account.tax",
            "search",
            [
                [
                    ("name", "=", name),
                    ("type_tax_use", "=", "sale"),
                    "|",
                    ("company_id", "=", company_id),
                    ("company_id", "=", False),
                ]
            ],
            {"limit": 1},
        )
        if ids:
            tax_ids.append(ids[0])
        else:
            warn(
                f"sale tax '{name}' not found for company_id={company_id}. "
                f"Products will be created WITHOUT it — fix tax wiring in GOL-4."
            )
    return tax_ids


def import_product(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    company_id: int,
    tax_ids: list[int],
    product: dict[str, Any],
) -> None:
    sku = product["sku"]
    existing = call(
        models, uid, "product.template", "search", [[("default_code", "=", sku)]], {"limit": 1}
    )
    if existing:
        print(f"  SKIP {sku} — already exists (id={existing[0]})")
        return

    category_id = lookup_id(
        models, uid, "product.category", [("name", "=", product["category"])], product["category"]
    )

    vals: dict[str, Any] = {
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
    }
    if product["variants"]:
        vals["attribute_line_ids"] = build_attribute_lines(
            models, uid, product["variants"]
        )
    if tax_ids:
        vals["taxes_id"] = [(6, 0, tax_ids)]

    new_id = call(models, uid, "product.template", "create", [vals])
    nvar = len(product["variants"]) or 1
    print(
        f"  CREATE {sku} → product.template id={new_id} "
        f"({product['name']}, {nvar} variant{'s' if nvar != 1 else ''})"
    )


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: import_nursery_catalog.py <catalog.csv>")
    csv_path = sys.argv[1]

    products = parse_catalog(csv_path)
    if not products:
        fail(f"No products parsed from {csv_path}")

    print(f"Parsed {len(products)} products from {csv_path}:")
    for sku, p in products.items():
        nvar = len(p["variants"]) or 1
        print(f"  {sku}: {p['name']} (${p['list_price']:.2f}, {nvar} variant(s))")

    if DRY_RUN:
        print("\nDRY_RUN=1 — no changes written.")
        return

    models, uid = authenticate()
    company_id = lookup_id(
        models, uid, "res.company", [("name", "=", ODOO_COMPANY)], ODOO_COMPANY
    )
    print(f"Target company_id={company_id} ({ODOO_COMPANY})")
    tax_ids = resolve_sale_taxes(models, uid, company_id)
    print(f"Applying sale taxes: {tax_ids or '(none)'}\n")

    for product in products.values():
        import_product(models, uid, company_id, tax_ids, product)
    print("\nDone.")


if __name__ == "__main__":
    main()
