# Contributing to Grove Odoo Modules

## Branch Strategy

- `main` — production branch, deployed via git-sync
- Feature branches — `feature/<module-name>-<description>`
- Bug fixes — `fix/<module-name>-<description>`

## Pull Request Process

1. Create a branch from `main`
2. Make changes, ensure `ruff check .` and `ruff format --check .` pass
3. Test locally against the odoocker stack
4. Open a PR with a description of what changed and why
5. CI must pass (lint + manifest validation)
6. Merge to `main` — git-sync auto-deploys to production

## Module Naming

- Prefix all modules with `grove_`
- Use snake_case: `grove_headless`, `grove_crm_leads`, `grove_inventory_api`
- Keep names descriptive but concise

## Code Style

- Line length: 120 characters
- Linter: Ruff with E, F, I rules
- Follow Odoo coding guidelines for model/controller patterns
- Use explicit field lists in API responses (never `read()` without specifying fields)
- Always scope data by `company_id` for multi-tenant safety
