# Grove Odoo Modules

Custom Odoo 19 modules for the Gather at the Grove multi-tenant ecosystem.

## Architecture

This repo is deployed to production via **git-sync** — a sidecar container in the odoocker stack that clones this repo to `/workspace/current`. Odoo includes that path in `addons_path`, so module changes take effect without rebuilding the Docker image.

## Module Conventions

- Each module is a top-level directory (e.g., `grove_headless/`)
- Module names are prefixed with `grove_` to avoid conflicts with OCA/community modules
- Version format: `19.0.X.Y.Z` (Odoo version . major . minor . patch)
- License: `LGPL-3` (standard for Odoo community modules)
- Dependencies must be listed in `__manifest__.py` `depends` key

## API Controller Patterns

- Routes use `/grove/api/v1/` prefix for all headless endpoints
- Auth: `public` for storefront (products, cart), `bearer` for authenticated (orders)
- Company isolation: always use `request.website.company_id` or `request.env.company`
- Return plain JSON via `_json_response()` helper (not Odoo's JSON-RPC wrapper)
- Field selection: define explicit field lists (e.g., `PRODUCT_LIST_FIELDS`) — never return `*`

## Linting

```bash
ruff check . --select E,F,I --line-length 120
ruff format --check . --line-length 120
```

## Related Repos

- **odoocker**: `Goldberry-Playground/odoocker-goldberrygrove` — infrastructure, Docker Compose, nginx
- **grove-sites**: `Goldberry-Playground/grove-sites` — Next.js 15 monorepo (hub + 3 tenant storefronts) that consumes these API endpoints via `packages/odoo-client`
