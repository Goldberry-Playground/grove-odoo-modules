# Shipping Zones — Live System Reference (GOL-15)

Canonical design: vault **wiki/Software/Grove Shipping** (2026-07-02).

## Rate table — `data/shipping_rates.json`

Zone rates are stored in `grove_headless/data/shipping_rates.json` and loaded at
startup by `models/shipping_zones.py`. Each rate is the Pirate Ship least-cost
allowlisted ground label cost for the box plus the flat $2.50 handling. The
rate-checker (`scripts/rate_check/rate_check.py`, manual dispatch) keeps them
current via PR.

> **Checker owns this file.** A rate-check run rewrites `shipping_rates.json`
> wholesale from live Pirate Ship quotes. The Odoo loader reads only the `base`
> key per box. Do **not** hand-add `per_lb` or `free_over` keys directly to this
> file while the checker is active — they will be silently dropped on the next
> rates PR. To make those keys permanent, modify the rate-checker script itself
> so it writes them as part of the generated output.

Structure:

```json
{
  "zone_N": {
    "p24x10x4": {"base": <float>, "carrier": "...", "service": "...", "service_title": "..."},
    "p24x10x6": {"base": <float>, ...}
  }
}
```

Rates are per carton (Box Engine v2, schema 3). Both shippable tiers (bareroot and
potted / peat-and-bagged) pack into the same cartons and share these rows.

Optional per-zone keys (see vault spec): `per_lb` (float), `free_over` (float).
Keys beginning with `_` are ignored (used for comments).

## State eligibility — `ZONE_BY_STATE` / `GREEN_STATES`

`models/shipping_zones.py` defines two complementary constants:

- **`GREEN_STATES`** (32 entries: 31 states plus DC) — the compliance gate: we ship only to these.
  Any checkout address outside this set returns `None` from
  `compute_order_shipping`, which causes the checkout to add **no** shipping line
  (fail-safe — never a guessed charge).
- **`ZONE_BY_STATE`** — maps each green state to one of five rate zones
  (`zone_1` … `zone_5`), keyed by ground transit distance from zip 26651 (WV). The
  bands were originally derived from UPS ground transit and are retained as-is:
  under least-cost selection (GOL-1906) UPS remains available for the parcels where
  its billing wins, so the box catalog and zone bands stay UPS-calibrated.

Green states (alphabetical): AL, AR, CT, DC, DE, FL, GA, IA, IL, IN, KY, LA, MA, MD,
ME, MI, MN, MO, MS, NC, NH, NJ, NY, OH, PA, RI, SC, TN, VA, VT, WI, WV. The code
(`GREEN_STATES`) is authoritative; genus-level carve-outs live in
`models/plant_compliance.py`.

## Live rate feed — `GET /grove/api/v1/shipping/rates` (GOL-952)

`rate_feed()` serves the in-memory rate table and zone map as read-only JSON so
the storefront product-page estimator prices against exactly what checkout will
charge, instead of a bundled snapshot that drifts as the checker rewrites
`shipping_rates.json`. Public, no external rate call, no DB read.

```json
{
  "schema": 2,
  "zones": { "zone_1": { "p24x10x4": {"base": 14.33}, "p24x10x6": {"base": 16.06} }, ... },
  "zone_by_state": { "WV": "zone_1", ... },
  "green_states": ["AL", "AR", ...],
  "packing": { ... },
  "calendar": { ... }
}
```

`zones` mirrors `data/shipping_rates.json`; the frontend drops it into
`resolveRateTable()` (grove-sites `apps/nursery/lib/shipping-estimate.ts`).
`zone_by_state` is the authoritative green-list zone map — the frontend
eligibility gate must stay in lockstep with it. Because the feed is served from
the same module globals `compute_order_shipping` prices with, it can never
disagree with the checkout engine within a running instance.

## Product tier — `grove_shipping_tier`

`product.template` carries a `grove_shipping_tier` selection field (`bareroot` or
`potted`). The checkout reads this field per order line and passes it to
`compute_order_shipping`, which packs the lines into cartons and sums the per-box zone rates. Untagged products default to
`potted` (never undercharged).

## Shipping calendar — `models/shipping_calendar.py`

USDA hardiness zone (integer 2–10) is resolved from the customer's destination ZIP
via the vendored PHZM 2023 matrix (`data/zip_usda_zone.csv`, built by
`scripts/build_zip_zone_matrix.py`). The calendar module uses the USDA zone (not
the rate zone) to determine:

- **`WAVE_SCHEDULE`** — bareroot ship windows and order-by deadlines per USDA zone
  and season (fall / spring)
- **`FREEZE_WINDOWS`** — per-zone cold-weather no-ship ranges (conservative launch
  defaults; tighten via data PR after nursery-manager feedback)
- **`NO_SHIP_MONTHS`** — global January + February floor applied before zone checks

`ship_options(zip_code, tier, today)` returns the full availability dict consumed by
`GET /grove/api/v1/shipping/options`.

## Rate-check automation

`scripts/rate_check/rate_check.py` + `.github/workflows/rate-check.yml`:

- Runs by manual `workflow_dispatch`; the daily 07:00 ET cron was removed 2026-10-14 (GOL-2605)
- Fetches real least-cost ground quotes from **Pirate Ship's public rate
  calculator** (`POST https://ship.pirateship.com/api/graphql?opname=RatesQuery`,
  no auth) — cheapest allowlisted ground among UPS Ground (`03`), UPS Ground
  Saver (`93`), and USPS Ground Advantage, transit-guarded — for each zone × box
  parcel profile
- If any rate drifts ≥ $1.00 from the JSON file, opens a PR to update
  `data/shipping_rates.json` and posts a Discord notification
- **No API key required.** Pirate Ship's rate calculator is public, so the old
  `SHIPPO_API_KEY` guard step is gone and the checker always runs. The only
  optional secret is `DISCORD_OPS_WEBHOOK_URL` — the Discord notification step is
  individually skipped when it is absent (safe to merge before credentials exist).
  No PR token is needed either: the PR opens under the default token and the
  workflow self-dispatches `ci.yml` at `chore/rate-check` so the required
  checks run (GOL-2114, see runbook)
