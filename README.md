# Grove Odoo Modules

[![CI](https://github.com/Goldberry-Playground/grove-odoo-modules/actions/workflows/ci.yml/badge.svg)](https://github.com/Goldberry-Playground/grove-odoo-modules/actions/workflows/ci.yml)

Custom Odoo 19 modules powering the **Gather at the Grove** multi-tenant ecosystem — three businesses on a single Odoo instance.

| Tenant Slug | Business | Domain | Odoo Company |
|-------------|----------|--------|-------------|
| `goldberry` | Goldberry Grove Farm | goldberrygrove.farm | Goldberry Grove Farm |
| `ggg` | George George George Woodworking, LLC | woodworkingeorge.com | George George George Woodworking |
| `nursery` | At The Grove Nursery, LLC | atthegrovenursery.com | At The Grove Nursery |

## Table of Contents

- [Architecture](#architecture)
- [Modules](#modules)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Development](#development)
- [Testing](#testing)
- [CI Pipeline](#ci-pipeline)
- [Contributing](#contributing)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     React Frontends                         │
│  goldberrygrove.farm   woodworkingeorge.com   atthegrove…   │
└──────────┬──────────────────┬──────────────────┬────────────┘
           │ X-Grove-Tenant:  │ X-Grove-Tenant:  │ X-Grove-Tenant:
           │ goldberry        │ ggg              │ nursery
           ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────┐
│                  grove_headless API                          │
│   /grove/api/v1/*  (auth=public, orders POST=bearer)         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Products API  │  │  Cart API    │  │ Health Check │      │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘      │
│         │                 │                                  │
│         ▼                 ▼                                  │
│  ┌─────────────────────────────────┐                        │
│  │  Multi-Company DB (Odoo 19)     │                        │
│  │  company_id scopes all queries  │                        │
│  └─────────────────────────────────┘                        │
└─────────────────────────────────────────────────────────────┘
```

Each React website sends an `X-Grove-Tenant` header. The `grove_headless` module resolves that header to the correct Odoo website/company, scoping all product and cart data to the appropriate business.

## Modules

| Module | Version | Purpose | Status |
|--------|---------|---------|--------|
| `grove_headless` | 19.0.1.3.0 | REST API + multi-tenant routing + nursery potting-batch workflow | Active |

### grove_headless

**Depends on:** `base`, `account`, `website_sale`, `website`, `mrp`, `stock`

**What it does:**

- Exposes 7 JSON API endpoints under `/grove/api/v1/`: health, products list/detail, cart get/add, order create, order detail (token-gated)
- Adds custom fields to `product.template`: `grove_featured` (Boolean), `grove_seo_description` (Text, translatable), `grove_slug` (Char, indexed — used for slug-based product lookups)
- Overrides `website.get_current_website()` to resolve tenants via `X-Grove-Tenant` header
- Extends the product form view with a "Grove Headless" tab for the custom fields
- Bootstraps multi-company + multi-website records, base product categories/attributes, WV sales tax, and a sequence registry (see `data/`)
- Adds the `grove.potting.batch` workflow for nursery potting-up operations (model + views + dedicated record rules under `security/grove_security_rules.xml`)
- Defines ACLs: public/portal = read-only, internal users = read/write/create

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Odoo | 19.0 | Community or Enterprise |
| Python | 3.12+ | Odoo 19 requirement |
| PostgreSQL | 15+ | Via Odoo's database |
| Odoo modules | `base`, `account`, `website`, `website_sale`, `mrp`, `stock` | Must be installed before `grove_headless` |
| Ruff | Latest | For linting (development only) |

## Installation

### Option 1: Production via git-sync (recommended)

The [odoocker-goldberrygrove](https://github.com/Goldberry-Playground/odoocker-goldberrygrove) stack includes a `custom-modules-sync` container that automatically clones this repo to `/workspace/current`. Odoo's `addons_path` includes that directory.

```
Push to main → GitHub webhook → git-sync pulls → /workspace/current updated → Odoo reads new code
```

Polling interval: 30 seconds (webhook overrides for instant sync).

After sync, install/upgrade the module:

```bash
# Install for the first time
docker compose exec odoo odoo -d odoo --init grove_headless --stop-after-init

# Upgrade after code changes
docker compose exec odoo odoo -d odoo --update grove_headless --stop-after-init
```

### Option 2: Local development with odoocker

1. Clone this repo alongside the odoocker stack:

   ```bash
   cd ~/Documents/Dev\ Projects/gather-at-the-grove/
   git clone git@github.com:Goldberry-Playground/grove-odoo-modules.git
   ```

2. The `docker-compose.override.local.yml` bind-mounts this repo to `/workspace/current`, so code changes are reflected immediately.

3. Restart Odoo to pick up changes:

   ```bash
   cd ../odoocker
   docker compose -f docker-compose.yml -f docker-compose.override.local.yml restart odoo
   ```

4. Install the module via Odoo UI: **Settings → Apps → Update Apps List → Search "Grove Headless" → Install**

### Option 3: Standalone Odoo (manual)

1. Clone this repo:

   ```bash
   git clone git@github.com:Goldberry-Playground/grove-odoo-modules.git /opt/odoo/custom-addons/grove
   ```

2. Add to Odoo's `addons_path` in `odoo.conf`:

   ```ini
   addons_path = /opt/odoo/odoo/addons,/opt/odoo/custom-addons/grove
   ```

3. Restart Odoo and install:

   ```bash
   odoo -d your_db --init grove_headless --stop-after-init
   ```

## Configuration

### Multi-Tenant Website Setup

Each tenant slug in the code maps to an Odoo website **by exact name**. These website records must exist in your Odoo database:

| Odoo Website Name (must match exactly) | Tenant Slug | Company |
|----------------------------------------|-------------|---------|
| `Goldberry Grove Farm` | `goldberry` | Goldberry Grove Farm |
| `George George George Woodworking` | `ggg` | George George George Woodworking |
| `At The Grove Nursery` | `nursery` | At The Grove Nursery |

To create these in Odoo: **Website → Configuration → Websites → New**

Each website must be linked to its corresponding company. If the website name doesn't match the slug map exactly, tenant resolution will fail silently and fall back to host-based resolution.

### Product Configuration

- **Publishing:** Products must be marked as `is_published = True` to appear in API responses
- **Featured:** Check the `grove_featured` field in the "Grove Headless" tab on the product form to include in featured product queries
- **SEO Description:** Fill in `grove_seo_description` in the "Grove Headless" tab for frontend meta tags
- **Images:** Products need at least one image — the API serves image URLs via `/web/image/product.template/{id}/image_1920`

## API Reference

All endpoints return JSON. Tenant context is set via the `X-Grove-Tenant` header.

### Health Check

```
GET /grove/api/v1/health
Auth: none
```

**Response:**

```json
{
  "status": "ok"
}
```

### List Products

```
GET /grove/api/v1/products
Auth: public
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 40 | Max products to return (capped at 200) |
| `offset` | int | 0 | Pagination offset |
| `featured` | string | — | Set to `"1"` to filter to featured products only |
| `category_id` | int | — | Filter by `public_categ_ids` |
| `slug` | string | — | Filter to a single product by its `grove_slug` (lowercased, exact match) |

**Request Example:**

```bash
curl -s 'http://localhost:8069/grove/api/v1/products?limit=10&featured=1' \
  -H 'X-Grove-Tenant: goldberry'
```

**Response:**

```json
{
  "count": 1,
  "limit": 10,
  "offset": 0,
  "results": [
    {
      "id": 1,
      "name": "Farm Fresh Eggs",
      "list_price": 6.50,
      "description_sale": "Free-range eggs from our pasture-raised hens",
      "grove_seo_description": "Buy fresh free-range eggs from Goldberry Grove Farm",
      "grove_featured": true,
      "slug": "farm-fresh-eggs",
      "public_categ_ids": [[4, "Farm Products"]],
      "image_url": "/web/image/product.template/1/image_128",
      "is_published": true,
      "website_url": "/shop/farm-fresh-eggs-1"
    }
  ]
}
```

`count` is the unfiltered total matching the domain, not `len(results)` — use it for pagination math. `results[*].slug` is the same value as the underlying `grove_slug` field (renamed in the response for frontend ergonomics).

### Get Product Detail

```
GET /grove/api/v1/products/<product_id>
Auth: public
```

**Request Example:**

```bash
curl -s 'http://localhost:8069/grove/api/v1/products/1' \
  -H 'X-Grove-Tenant: goldberry'
```

**Response:**

```json
{
  "product": {
    "id": 1,
    "name": "Farm Fresh Eggs",
    "list_price": 6.50,
    "description_sale": "Free-range eggs",
    "grove_seo_description": "...",
    "grove_featured": true,
    "public_categ_ids": [[4, "Farm Products"]],
    "image_url": "/web/image/product.template/1/image_1920",
    "is_published": true,
    "website_url": "/shop/farm-fresh-eggs-1",
    "variants": [
      {
        "id": 1,
        "name": "Farm Fresh Eggs",
        "default_code": "EGG-001",
        "barcode": null,
        "list_price": 6.50,
        "qty_available": 24.0,
        "attribute_line_ids": []
      }
    ]
  }
}
```

**Error (product not found or wrong tenant):**

```json
{
  "error": "Product not found"
}
```

HTTP Status: 404

### Get Cart

```
GET /grove/api/v1/cart
Auth: public (session-based)
```

**Request Example:**

```bash
curl -s 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -b cookies.txt -c cookies.txt
```

**Response (cart exists):**

```json
{
  "lines": [
    {
      "id": 12,
      "product_id": [1, "Farm Fresh Eggs"],
      "product_uom_qty": 2.0,
      "price_unit": 6.50,
      "price_subtotal": 13.00
    }
  ],
  "amount_total": 13.00,
  "currency": {"id": 1, "name": "USD"}
}
```

**Response (no cart, or a session cookie originating from another tenant — cross-company carts are not rendered):**

```json
{
  "lines": [],
  "amount_total": 0,
  "currency": null
}
```

### Add to Cart

```
POST /grove/api/v1/cart
Auth: public (session-based)
Content-Type: application/json
CSRF: disabled
```

**Request Body:**

```json
{
  "product_id": 1,
  "quantity": 2
}
```

Either `variant_id` (a `product.product` id) or `product_id` (a `product.template` id) is accepted; pass at least one. When only `product_id` is given, the default variant is resolved server-side. `quantity` defaults to `1`.

**Request Example:**

```bash
curl -s -X POST 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -H 'Content-Type: application/json' \
  -b cookies.txt -c cookies.txt \
  -d '{"product_id": 1, "quantity": 2}'
```

**Response:** Same shape as `GET /grove/api/v1/cart` — `{ lines, amount_total, currency }`.

**Error (missing fields):**

```json
{
  "error": "Either variant_id or product_id is required"
}
```

HTTP Status: 400

**Error (product not found):**

```json
{
  "error": "Product not found"
}
```

HTTP Status: 404

### Create Order

```
POST /grove/api/v1/orders
Auth: bearer  (Authorization: Bearer <API_KEY>)
Content-Type: application/json
CSRF: disabled
```

Creates a draft `sale.order` from a posted cart. The response includes a server-issued `access_token` that must be passed back when fetching the order detail later — this prevents PII leakage by id enumeration on the public confirmation page.

**Why `bearer` and not `public`:** in Odoo 19 only `auth="bearer"` actually parses the `Authorization` header for an API key. `auth="user"` only honours session cookies; `auth="public"` would accept any caller. Using `bearer` keeps the public internet from creating `sale.order` + `res.partner` records (and bypassing the BFF / rate limits) against any tenant company. The Next.js BFF (see [`@grove/odoo-client`](https://github.com/Goldberry-Playground/grove-sites/tree/main/packages/odoo-client)) sets this header on every request. Cart endpoints stay `auth="public"` because they rely on `website_sale`'s session-cookie cart proxy; `GET /orders/<id>` stays `auth="public"` because its gate is the per-order `access_token`.

**Request Body:**

```json
{
  "contact":  {"name": "Jane Doe", "email": "jane@example.com", "phone": "555-1212"},
  "shipping": {"street": "1 Main St", "city": "Charleston", "state": "WV", "zip": "25301", "country": "US"},
  "billing":  null,
  "payment_method": "card",
  "items": [{"variant_id": 12, "quantity": 2}]
}
```

**Response:**

```json
{
  "id": 42,
  "name": "S00042",
  "state": "draft",
  "access_token": "abc123...",
  "amount_untaxed": 76.00,
  "amount_tax": 5.32,
  "amount_total": 81.32,
  "currency": {"id": 1, "name": "USD"},
  "line_count": 1
}
```

### Get Order Detail

```
GET /grove/api/v1/orders/<order_id>?access_token=<token>
Auth: public
```

Token-gated read. Without a matching `access_token` the endpoint returns 403 (no token) or 404 (wrong token), so order ids cannot be enumerated to scrape customer data. The token is generated by Odoo's `_portal_ensure_token()` on order creation and embedded in the React success-page URL.

## Seeder Scripts

Operational helpers in `scripts/` for bootstrapping a fresh tenant. Each script is idempotent — re-running skips records that already exist.

| Script | What it seeds |
|--------|--------------|
| `import_grove_catalog.py` | **Real per-tenant product catalogs** from `scripts/catalogs/<tenant>.csv`. Handles goldberry / ggg / nursery in one script, uploads product photos, wires `grove_headless` fields (slug, featured, SEO). See [`scripts/catalogs/README.md`](./scripts/catalogs/README.md). |
| `import_nursery_catalog.py` | *(legacy, nursery-only)* Superseded by `import_grove_catalog.py`; kept until first successful generic run. |
| `seed_sample_products.py` | *(legacy)* 5 representative Goldberry demo products. Superseded by `import_grove_catalog.py` with a real `goldberry.csv`. |
| `seed_payment_journals.py` | Cash, Card, Check, Online Payment, Invoice (Net 30) journals |
| `seed_sales_teams.py` | Farmer's Market, Direct to Nursery, Online sales teams |
| `setup_ghost_integration.py` | Bootstraps Ghost admin + creates a Custom Integration, prints `GHOST_CONTENT_KEY=...` |

Run against a live Odoo instance:

```bash
ODOO_URL=http://localhost:8069 \
ODOO_DB=Goldberry \
ODOO_USER=josh@goldberrygrove.farm \
ODOO_PASSWORD=*** \
python3 scripts/seed_sample_products.py
```

The Ghost script uses `GHOST_URL`, `GHOST_ADMIN_EMAIL`, `GHOST_ADMIN_PASSWORD` instead.

## Development

### Repository Structure

```
grove-odoo-modules/
├── grove_headless/
│   ├── __init__.py              # Root package init
│   ├── __manifest__.py          # Odoo module manifest (19.0.1.3.0)
│   ├── controllers/
│   │   ├── __init__.py
│   │   └── main.py              # All API endpoints (products, cart, orders)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── product_template.py  # Custom fields (grove_featured, grove_seo_description, grove_slug)
│   │   ├── website.py           # Tenant resolution override
│   │   └── potting_batch.py     # grove.potting.batch nursery workflow model
│   ├── data/
│   │   ├── grove_companies.xml          # 3 companies + 3 websites bootstrap
│   │   ├── grove_product_categories.xml # Trees / Shrubs / Mixed
│   │   ├── grove_product_attributes.xml # Size + Container attributes
│   │   ├── grove_taxes.xml              # WV state 6% + municipal 1%
│   │   └── grove_sequences.xml          # Sequence registry (potting batch refs, etc.)
│   ├── security/
│   │   ├── ir.model.access.csv  # ACL rules (public, portal, internal)
│   │   └── grove_security_rules.xml  # Record rules for potting batch + multi-company scoping
│   ├── views/
│   │   ├── product_template_views.xml  # "Grove Headless" tab on product form
│   │   └── potting_batch_views.xml     # Form/list/menu for grove.potting.batch
│   └── tests/                   # Odoo TransactionCase suites
│       ├── __init__.py
│       ├── test_kit_boms.py
│       ├── test_potting_batch.py
│       ├── test_product_slug.py
│       └── test_tenant_routing.py
├── scripts/                     # Idempotent operational seeders (XML-RPC / HTTP)
│   ├── seed_sample_products.py
│   ├── seed_payment_journals.py
│   ├── seed_sales_teams.py
│   ├── seed_kit_boms.py         # Kit BOMs for bundled nursery products
│   └── setup_ghost_integration.py
├── .github/
│   └── workflows/
│       └── ci.yml               # Lint + manifest + tenant + XML data + smoke install
├── .ruff.toml                   # Ruff linter config
├── requirements-ci.txt          # CI extras (pinned tooling for the smoke-install job)
├── CLAUDE.md                    # AI assistant context
├── CONTRIBUTING.md              # Contribution guidelines
└── README.md                    # This file
```

### Creating a New Module

```bash
cd grove-odoo-modules/
mkdir grove_mymodule
```

Create `grove_mymodule/__manifest__.py`:

```python
{
    "name": "Grove My Module",
    "version": "19.0.1.0.0",
    "category": "Website",
    "summary": "Description here",
    "author": "Gathering at the Grove",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [],
    "installable": True,
    "auto_install": False,
}
```

Create `grove_mymodule/__init__.py`:

```python
from . import models
from . import controllers
```

### Coding Conventions

- Module names prefixed with `grove_` to avoid conflicts with OCA/community modules
- Version format: `19.0.X.Y.Z` (Odoo major . module major . minor . patch)
- License: `LGPL-3`
- Routes use `/grove/api/v1/` prefix
- Auth: `none` (health), `public` for storefront read endpoints (products, cart get/post, order get with `access_token` gate), `bearer` for write endpoints that mutate Odoo records on behalf of an authenticated BFF (currently `POST /orders`)
- Company isolation: always scope queries by `request.website.company_id`
- Return JSON via `_json_response()` helper — not Odoo's JSON-RPC wrapper
- Define explicit field lists (`PRODUCT_LIST_FIELDS`) — never `read()` without fields
- Line length: 120 characters max

### Linting

```bash
# Install ruff
pip install ruff

# Check for lint errors
ruff check . --select E,F,I --line-length 120

# Check formatting
ruff format --check . --line-length 120

# Auto-fix lint issues
ruff check . --select E,F,I --line-length 120 --fix

# Auto-format
ruff format . --line-length 120
```

## Testing

### Manual Testing with curl

Start a session and test all endpoints:

```bash
# 1. Health check (no tenant needed)
curl -s http://localhost:8069/grove/api/v1/health | python3 -m json.tool

# 2. List all products for Goldberry
curl -s 'http://localhost:8069/grove/api/v1/products' \
  -H 'X-Grove-Tenant: goldberry' | python3 -m json.tool

# 3. List featured products only
curl -s 'http://localhost:8069/grove/api/v1/products?featured=1&limit=5' \
  -H 'X-Grove-Tenant: goldberry' | python3 -m json.tool

# 4. Get a specific product (replace 1 with a valid ID)
curl -s 'http://localhost:8069/grove/api/v1/products/1' \
  -H 'X-Grove-Tenant: goldberry' | python3 -m json.tool

# 5. Cart operations (use cookies for session persistence)
# Get cart (should be null initially)
curl -s 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -c cookies.txt -b cookies.txt | python3 -m json.tool

# Add item to cart
curl -s -X POST 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -H 'Content-Type: application/json' \
  -c cookies.txt -b cookies.txt \
  -d '{"product_id": 1, "quantity": 2}' | python3 -m json.tool

# Verify cart has item
curl -s 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -c cookies.txt -b cookies.txt | python3 -m json.tool

# 6. Test tenant isolation — same product ID should 404 on wrong tenant
curl -s 'http://localhost:8069/grove/api/v1/products/1' \
  -H 'X-Grove-Tenant: ggg' | python3 -m json.tool

# 7. Test error cases
curl -s 'http://localhost:8069/grove/api/v1/products/99999' \
  -H 'X-Grove-Tenant: goldberry' | python3 -m json.tool

curl -s -X POST 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -H 'Content-Type: application/json' \
  -d '{}' | python3 -m json.tool
```

### Replicate CI Locally

```bash
# Run the full CI check locally
pip install ruff
ruff check . --select E,F,I --line-length 120
ruff format --check . --line-length 120
python3 -c "
import ast, pathlib
for p in pathlib.Path('.').rglob('__manifest__.py'):
    m = ast.literal_eval(p.read_text())
    assert 'name' in m and 'version' in m and m.get('installable'), f'{p} failed validation'
    print(f'  ✓ {m[\"name\"]} ({m[\"version\"]})')
"
```

## CI Pipeline

Runs on every push and PR to `main`. Three jobs:

| Job | What it checks |
|-----|---------------|
| **Lint Python** | Ruff lint (E, F, I rules) and format check at 120 char line length |
| **Validate Manifests** | Parses all `__manifest__.py` files; ensures `name`, `version`, and `installable` are present |
| **Validate Tenant Config** | Parses `_TENANT_SLUGS` from `website.py`; checks all 3 slugs (`goldberry`, `ggg`, `nursery`) exist with non-empty values |

## Contributing

1. Create a feature branch from `main`
2. Add/modify modules following the `grove_` prefix convention
3. Run linting locally: `ruff check . && ruff format --check .`
4. Open a PR — all CI checks must pass before merge

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

## Related Repositories

| Repo | Purpose |
|------|---------|
| [grove-sites](https://github.com/Goldberry-Playground/grove-sites) | Next.js monorepo — React frontends that consume these API endpoints |
| [odoocker-goldberrygrove](https://github.com/Goldberry-Playground/odoocker-goldberrygrove) | Docker infrastructure — Odoo, Ghost CMS, nginx, PostgreSQL |

## License

LGPL-3.0 — see individual module manifests for details.
