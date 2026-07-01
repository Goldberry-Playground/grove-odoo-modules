# Grove Product Catalogs

Per-tenant CSVs consumed by `scripts/import_grove_catalog.py`.

## Files

| File | Tenant | Odoo company | URL |
|---|---|---|---|
| `goldberry.csv` | Goldberry Grove Farm | `Goldberry Grove Farm` | goldberrygrove.farm |
| `ggg.csv` | GGG Woodworking | `George George George Woodworking, LLC` | woodworkingeorge.com |
| `nursery.csv` | At The Grove Nursery | `At The Grove Nursery` | atthegrovenursery.com |
| `_template.csv` | (docs only, not imported) | — | — |

Tenant is inferred from the filename — do not rename these files.

## Photos

Store product photos in `images/<tenant>/<sku-or-slug>.webp` next to the CSV.
The `image_path` column is resolved relative to the CSV's directory, so
`images/goldberry/tree-honeycrisp.webp` in `goldberry.csv` looks up
`scripts/catalogs/images/goldberry/tree-honeycrisp.webp`.

Photos are read at import time, base64-encoded, and stored in Odoo's
`product.template.image_1920` field. Storefronts fetch them via
`${ODOO_URL}/web/image/product.template/<id>/image_1920`.

Missing photos are non-fatal (the product is created with no photo and a
placeholder tile renders on the shop page). Fill in later + re-run.

## Column reference

Required:
- **sku** — unique within the tenant; used for idempotency
- **name** — display name shown on the shop page
- **category** — must match a seeded `product.category` name
- **list_price** — decimal USD; taken from the first row per SKU

Optional:
- **size**, **container** — variant axes. Rows sharing an `sku` with
  distinct (size, container) pairs collapse into one `product.template`
  with those variants. Omit both for a no-variant product.
- **description_sale** — customer-facing paragraph, plain text.
- **image_path** — relative to the CSV; see "Photos" above.
- **grove_slug** — URL slug for `/shop/<slug>`. Auto-generated from `name`
  when omitted (`Honeycrisp Apple Tree` → `honeycrisp-apple-tree`).
- **grove_featured** — `true` / `yes` / `1` to surface on the tenant
  homepage. Anything else = false. Only the first row per SKU is
  consulted.
- **grove_seo_description** — meta description for SEO. Only the first row
  per SKU is consulted.

## Running the importer

```bash
# Local dev — dry-run first to catch typos
DRY_RUN=1 python3 ../import_grove_catalog.py .

# Actual import (one tenant)
ODOO_URL=http://localhost:8069 \
ODOO_DB=Goldberry \
ODOO_USER=josh@goldberrygrove.farm \
ODOO_PASSWORD='...' \
python3 ../import_grove_catalog.py goldberry.csv

# Or all tenants in one shot (pass the directory)
python3 ../import_grove_catalog.py .
```

Idempotency: SKUs already present in the target company are skipped.
Safe to append rows + re-run to load only the new items.

## When adding a new tenant

1. Add a new entry to `TENANT_CONFIG` in `import_grove_catalog.py` mapping
   the tenant slug (matches the filename base) to the correct
   `res.company` name + default sale taxes.
2. Create `<tenant>.csv` here with just the header row.
3. Create `images/<tenant>/` next to it.
4. Add the tenant to the reference table above.
