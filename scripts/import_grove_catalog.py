#!/usr/bin/env python3
"""Import Grove product catalogs into Odoo from per-tenant CSVs.

Generalization of the older ``import_nursery_catalog.py`` (which was
hard-coded to At The Grove Nursery). Handles all three tenants
(goldberry / ggg / nursery), uploads product photos from local paths, and
wires the ``grove_headless``-specific fields (grove_slug, grove_featured,
grove_seo_description) that the storefronts use for URL routing and
homepage tile curation.

Filename convention: tenant is inferred from the CSV basename, so a file
named ``goldberry.csv`` seeds into the Goldberry Grove company; ``ggg.csv``
into GGG Woodworking; ``nursery.csv`` into At The Grove Nursery. This
enforces the "one file per tenant" split without a redundant column that
could get typo'd.

Idempotent per (tenant, sku): re-running skips any SKU that already exists
for the target company, so appending rows to a CSV and re-running is safe.

CSV schema
----------
One row per *variant* (rows sharing an ``sku`` collapse to one
product.template with (Size, Container) variant axes).

Required columns:
    sku              unique within the tenant, e.g. TREE-HONEYCRISP
    name             product display name
    category         must match a seeded product.category name
    list_price       decimal USD (taken from the first row per SKU)

Optional columns:
    size             variant axis, e.g. "3 gal" / "Bare Root"
    container        variant axis, e.g. "Nursery Pot" / "Burlap Ball"
    description_sale customer-facing description shown on the shop page
    image_path       relative path from the CSV to a local image file;
                     read + base64-encoded into product.template.image_1920.
                     Absent = no product photo (storefront falls back to a
                     placeholder)
    grove_slug       URL slug for /shop/<slug>. Auto-generated from name if
                     omitted (lowercase, non-alphanumeric -> dash, collapsed)
    grove_featured   'true' / 'yes' / '1' to surface on the tenant homepage.
                     Anything else = false. Only the first row per SKU is
                     consulted.
    grove_seo_description  meta description for SEO. Only the first row per
                           SKU is consulted.

Usage
-----
    # Single file
    ODOO_URL=http://localhost:8069 \\
    ODOO_DB=Goldberry \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin> \\
    python3 import_grove_catalog.py scripts/catalogs/goldberry.csv

    # All CSVs in a directory (goldberry.csv + ggg.csv + nursery.csv)
    python3 import_grove_catalog.py scripts/catalogs/

Optional env:
    DRY_RUN=1        parse + validate only; report what WOULD be created
    TAXES_OVERRIDE   comma-separated sale-tax names to apply. If unset, the
                     tenant's default WV tax pair is used. Empty string
                     disables tax assignment entirely.

Exit codes:
    0  all products imported (or skipped as already-present)
    1  malformed CSV, auth failure, or missing required Odoo data
"""

from __future__ import annotations

import base64
import csv
import os
import re
import sys
import xmlrpc.client
from collections import OrderedDict
from pathlib import Path
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"
TAXES_OVERRIDE = os.getenv("TAXES_OVERRIDE")  # None = use tenant default

REQUIRED_COLUMNS = {"sku", "name", "category", "list_price"}
KNOWN_COLUMNS = REQUIRED_COLUMNS | {
    "size",
    "container",
    "description_sale",
    "image_path",
    "grove_slug",
    "grove_featured",
    "grove_seo_description",
}

# Tenant catalog config. Filename base ("goldberry" from "goldberry.csv")
# selects one entry — this is why the split-by-tenant CSV convention matters.
# When a new tenant lands, add an entry here + a corresponding CSV.
TENANT_CONFIG: dict[str, dict[str, Any]] = {
    "goldberry": {
        "company_name": "Goldberry Grove Farm",
        "default_taxes": ["WV State Sales Tax 6%", "WV Municipal Tax 1%"],
    },
    "ggg": {
        "company_name": "George George George Woodworking, LLC",
        "default_taxes": ["WV State Sales Tax 6%", "WV Municipal Tax 1%"],
    },
    "nursery": {
        "company_name": "At The Grove Nursery",
        "default_taxes": ["WV State Sales Tax 6%", "WV Municipal Tax 1%"],
    },
}

TRUTHY = {"true", "yes", "1", "t", "y"}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"  WARN: {msg}")


def slugify(text: str) -> str:
    """Deterministic URL-safe slug: lowercase, non-alnum -> dash, collapsed."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", s)


def truthy(value: str) -> bool:
    return value.strip().lower() in TRUTHY


# ── CSV parsing ──────────────────────────────────────────────────────────────


def tenant_from_filename(path: Path) -> str:
    tenant = path.stem
    if tenant not in TENANT_CONFIG:
        fail(f"{path.name}: unknown tenant '{tenant}'. Expected one of: {sorted(TENANT_CONFIG)}")
    return tenant


def parse_catalog(path: Path) -> "OrderedDict[str, dict[str, Any]]":
    """Read one CSV into an ordered {sku: product-dict} map."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                fail(f"{path} is empty (no header row)")
            header = {c.strip() for c in reader.fieldnames}
            missing = REQUIRED_COLUMNS - header
            if missing:
                fail(f"{path}: missing required columns: {sorted(missing)}")
            unknown = header - KNOWN_COLUMNS
            if unknown:
                warn(f"{path}: unknown columns will be ignored: {sorted(unknown)}")
            rows = list(reader)
    except FileNotFoundError:
        fail(f"CSV file not found: {path}")

    products: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for i, raw in enumerate(rows, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        sku = row.get("sku", "")
        if not sku:
            if not any(row.values()):
                continue  # blank trailing row
            fail(f"{path.name} row {i}: missing sku")
        if not row.get("name"):
            fail(f"{path.name} row {i} (sku={sku}): missing name")
        if not row.get("category"):
            fail(f"{path.name} row {i} (sku={sku}): missing category")
        try:
            list_price = float(row["list_price"])
        except (ValueError, KeyError):
            fail(f"{path.name} row {i} (sku={sku}): list_price '{row.get('list_price', '')}' is not a number")

        size = row.get("size", "")
        container = row.get("container", "")

        if sku not in products:
            products[sku] = {
                "sku": sku,
                "name": row["name"],
                "category": row["category"],
                "list_price": list_price,
                "description_sale": row.get("description_sale", ""),
                "image_path": row.get("image_path", ""),
                "grove_slug": row.get("grove_slug", "") or slugify(row["name"]),
                "grove_featured": truthy(row.get("grove_featured", "")),
                "grove_seo_description": row.get("grove_seo_description", ""),
                "variants": [],
                "_first_row": i,
            }
        else:
            existing = products[sku]
            if existing["name"] != row["name"]:
                warn(
                    f"{path.name} row {i} (sku={sku}): name '{row['name']}' "
                    f"differs from '{existing['name']}' at row "
                    f"{existing['_first_row']}; keeping the first."
                )

        if size or container:
            pair = (size, container)
            if pair not in products[sku]["variants"]:
                products[sku]["variants"].append(pair)

    for p in products.values():
        p.pop("_first_row", None)
    return products


def load_image_b64(csv_path: Path, image_rel: str) -> str | None:
    """Read a local image relative to the CSV, return base64 string, or None.

    Absent / unreadable images are non-fatal — the product is still created
    and a warning is printed. Missing photos are common early in a tenant
    catalog's life; killing the whole import over one broken path would be
    worse than a placeholder tile on the shop page.
    """
    if not image_rel:
        return None
    resolved = (csv_path.parent / image_rel).resolve()
    try:
        raw = resolved.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        warn(f"image not found at {resolved} — product will have no photo")
        return None
    except OSError as e:
        warn(f"could not read image {resolved}: {e} — product will have no photo")
        return None
    return base64.b64encode(raw).decode("ascii")


# ── Odoo XML-RPC plumbing ────────────────────────────────────────────────────


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
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def lookup_id(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    domain: list,
    label: str,
) -> int:
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


def build_attribute_lines(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    variants: list[tuple[str, str]],
) -> list[tuple]:
    sizes = sorted({size for size, _ in variants if size})
    containers = sorted({container for _, container in variants if container})

    lines: list[tuple] = []
    if sizes:
        size_attr_id = lookup_id(models, uid, "product.attribute", [("name", "=", "Size")], "Size")
        size_value_ids = [get_attribute_value_id(models, uid, "Size", s) for s in sizes]
        lines.append((0, 0, {"attribute_id": size_attr_id, "value_ids": [(6, 0, size_value_ids)]}))
    if containers:
        container_attr_id = lookup_id(
            models,
            uid,
            "product.attribute",
            [("name", "=", "Container")],
            "Container",
        )
        container_value_ids = [get_attribute_value_id(models, uid, "Container", c) for c in containers]
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
    models: xmlrpc.client.ServerProxy,
    uid: int,
    company_id: int,
    tax_names: list[str],
) -> list[int]:
    """Best-effort lookup. Missing/cross-company taxes are warned about,
    not fatal — tax wiring lives on its own timeline (see GOL-4) and we
    don't want an unrelated tax config bug to block a catalog import."""
    tax_ids: list[int] = []
    for name in tax_names:
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
    csv_path: Path,
    product: dict[str, Any],
) -> None:
    sku = product["sku"]
    # Scope idempotency to the company so the same SKU in a different tenant
    # is treated as a distinct row (rare but possible: e.g. shared BOM parts).
    existing = call(
        models,
        uid,
        "product.template",
        "search",
        [[("default_code", "=", sku), ("company_id", "=", company_id)]],
        {"limit": 1},
    )
    if existing:
        print(f"  SKIP {sku} — already exists for company_id={company_id} (id={existing[0]})")
        return

    category_id = lookup_id(
        models,
        uid,
        "product.category",
        [("name", "=", product["category"])],
        product["category"],
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
        "grove_slug": product["grove_slug"],
        "grove_featured": product["grove_featured"],
        "grove_seo_description": product["grove_seo_description"],
    }
    if product["variants"]:
        vals["attribute_line_ids"] = build_attribute_lines(models, uid, product["variants"])
    if tax_ids:
        vals["taxes_id"] = [(6, 0, tax_ids)]

    image_b64 = load_image_b64(csv_path, product["image_path"])
    if image_b64:
        vals["image_1920"] = image_b64

    new_id = call(models, uid, "product.template", "create", [vals])
    nvar = len(product["variants"]) or 1
    photo_note = " +photo" if image_b64 else ""
    feat_note = " ⭐" if product["grove_featured"] else ""
    print(
        f"  CREATE {sku} → id={new_id} "
        f"({product['name']}, {nvar} variant{'s' if nvar != 1 else ''}"
        f", slug={product['grove_slug']}{photo_note}{feat_note})"
    )


# ── Top-level orchestration ──────────────────────────────────────────────────


def process_csv(csv_path: Path) -> None:
    tenant = tenant_from_filename(csv_path)
    cfg = TENANT_CONFIG[tenant]

    print(f"\n=== {csv_path.name} → tenant={tenant} ({cfg['company_name']}) ===")

    products = parse_catalog(csv_path)
    if not products:
        print(f"  (no products in {csv_path.name})")
        return

    print(f"Parsed {len(products)} products:")
    for sku, p in products.items():
        nvar = len(p["variants"]) or 1
        img = " +photo" if p["image_path"] else ""
        feat = " ⭐" if p["grove_featured"] else ""
        print(f"  {sku}: {p['name']} (${p['list_price']:.2f}, {nvar} variant(s), slug={p['grove_slug']}{img}{feat})")

    if DRY_RUN:
        print("  DRY_RUN=1 — no changes written for this file.")
        return

    models, uid = authenticate()
    company_id = lookup_id(
        models,
        uid,
        "res.company",
        [("name", "=", cfg["company_name"])],
        cfg["company_name"],
    )
    print(f"Target company_id={company_id} ({cfg['company_name']})")

    tax_names = (
        cfg["default_taxes"] if TAXES_OVERRIDE is None else [t.strip() for t in TAXES_OVERRIDE.split(",") if t.strip()]
    )
    tax_ids = resolve_sale_taxes(models, uid, company_id, tax_names) if tax_names else []
    print(f"Applying sale taxes: {tax_ids or '(none)'}\n")

    for product in products.values():
        import_product(models, uid, company_id, tax_ids, csv_path, product)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: import_grove_catalog.py <catalog.csv | catalog_dir/>")

    target = Path(sys.argv[1])
    if not target.exists():
        fail(f"target not found: {target}")

    if target.is_dir():
        csvs = sorted(target.glob("*.csv"))
        csvs = [c for c in csvs if not c.name.startswith("_")]  # skip _template.csv
        if not csvs:
            fail(f"no *.csv files found in {target} (excluding _template.csv)")
        print(f"Found {len(csvs)} catalog file(s) in {target}: {[c.name for c in csvs]}")
        for csv_path in csvs:
            process_csv(csv_path)
    else:
        process_csv(target)

    print("\nDone.")


if __name__ == "__main__":
    main()
