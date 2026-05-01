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
│              /grove/api/v1/*  (auth=public)                  │
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
| `grove_headless` | 19.0.1.0.0 | REST API for headless React storefronts (products, cart, health) | Active |

### grove_headless

**Depends on:** `base`, `website_sale`, `website`

**What it does:**

- Exposes 5 JSON API endpoints under `/grove/api/v1/`
- Adds custom fields to `product.template`: `grove_featured` (Boolean), `grove_seo_description` (Text, translatable)
- Overrides `website.get_current_website()` to resolve tenants via `X-Grove-Tenant` header
- Extends the product form view with a "Grove Headless" tab for the custom fields
- Defines ACLs: public/portal = read-only, internal users = read/write/create

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Odoo | 19.0 | Community or Enterprise |
| Python | 3.12+ | Odoo 19 requirement |
| PostgreSQL | 15+ | Via Odoo's database |
| Odoo modules | `base`, `website`, `website_sale` | Must be installed before `grove_headless` |
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

**Request Example:**

```bash
curl -s 'http://localhost:8069/grove/api/v1/products?limit=10&featured=1' \
  -H 'X-Grove-Tenant: goldberry'
```

**Response:**

```json
{
  "products": [
    {
      "id": 1,
      "name": "Farm Fresh Eggs",
      "list_price": 6.50,
      "description_sale": "Free-range eggs from our pasture-raised hens",
      "grove_seo_description": "Buy fresh free-range eggs from Goldberry Grove Farm",
      "grove_featured": true,
      "public_categ_ids": [[4, "Farm Products"]],
      "image_url": "/web/image/product.template/1/image_128",
      "is_published": true,
      "website_url": "/shop/farm-fresh-eggs-1"
    }
  ],
  "total": 1
}
```

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
  "cart": {
    "id": 5,
    "order_line": [
      {
        "id": 12,
        "product_id": [1, "Farm Fresh Eggs"],
        "product_uom_qty": 2.0,
        "price_unit": 6.50,
        "price_subtotal": 13.00
      }
    ],
    "amount_total": 13.00,
    "partner_id": [3, "Public user"]
  }
}
```

**Response (no cart):**

```json
{
  "cart": null
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

**Request Example:**

```bash
curl -s -X POST 'http://localhost:8069/grove/api/v1/cart' \
  -H 'X-Grove-Tenant: goldberry' \
  -H 'Content-Type: application/json' \
  -b cookies.txt -c cookies.txt \
  -d '{"product_id": 1, "quantity": 2}'
```

**Response:**

```json
{
  "cart": {
    "id": 5,
    "order_line": [...],
    "amount_total": 13.00,
    "partner_id": [3, "Public user"]
  }
}
```

**Error (missing fields):**

```json
{
  "error": "product_id and quantity are required"
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

## Development

### Repository Structure

```
grove-odoo-modules/
├── grove_headless/
│   ├── __init__.py              # Root package init
│   ├── __manifest__.py          # Odoo module manifest
│   ├── controllers/
│   │   ├── __init__.py
│   │   └── main.py              # All API endpoints
│   ├── models/
│   │   ├── __init__.py
│   │   ├── product_template.py  # Custom fields (grove_featured, grove_seo_description)
│   │   └── website.py           # Tenant resolution override
│   ├── security/
│   │   └── ir.model.access.csv  # ACL rules (public, portal, internal)
│   └── views/
│       └── product_template_views.xml  # "Grove Headless" tab on product form
├── .github/
│   └── workflows/
│       └── ci.yml               # Lint + manifest + tenant validation
├── .ruff.toml                   # Ruff linter config
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
- Auth: `public` for storefront endpoints, `user` for authenticated endpoints
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
