# Shipping Zones — Live System Reference (GOL-15)

Canonical design: vault **wiki/Software/Grove Shipping** (2026-07-02).

## Rate table — `data/shipping_rates.json`

Zone rates are stored in `grove_headless/data/shipping_rates.json` and loaded at
startup by `models/shipping_zones.py`. The file ships with provisional launch-
hypothesis values; the morning rate-checker (`scripts/rate_check/rate_check.py`)
replaces them with real Shippo-derived values via automated PR on its first run.

> **Checker owns this file.** The daily rate-check rewrites `shipping_rates.json`
> wholesale from live Shippo quotes. Only the `base` key per tier is preserved
> across runs. Do **not** hand-add `per_lb` or `free_over` keys directly to this
> file while the checker is active — they will be silently dropped on the next
> rates PR. To make those keys permanent, modify the rate-checker script itself
> so it writes them as part of the generated output.

Structure:

```json
{
  "zone_N": {
    "bareroot": {"base": <float>},
    "potted":   {"base": <float>}
  }
}
```

Optional per-zone keys (see vault spec): `per_lb` (float), `free_over` (float).
Keys beginning with `_` are ignored (used for comments).

## State eligibility — `ZONE_BY_STATE` / `GREEN_STATES`

`models/shipping_zones.py` defines two complementary constants:

- **`GREEN_STATES`** (21 states) — the compliance gate: we ship only to these states.
  Any checkout address outside this set returns `None` from `compute_shipping_rate`
  and `compute_order_shipping`, which causes the checkout to add **no** shipping line
  (fail-safe — never a guessed charge).
- **`ZONE_BY_STATE`** — maps each of the 21 green states to one of four rate zones
  (`zone_1` … `zone_4`). The carrier is USPS Ground Advantage (GOL-1906), so the
  bands are the USPS zone map from origin ZIP3 266 (WV): `zone_1` = USPS zone 3,
  `zone_2` = USPS zone 4, `zone_3` = USPS zone 5, `zone_4` = USPS zone 6. Each
  state is assigned to the band of the **highest** USPS zone any of its ZIP3s
  reaches (worst case → no undercharge, GOL-1495). The green list spans USPS
  zones 3-6 (no far corner is nearer than zone 3; only MN reaches zone 6), which
  is why there are four bands, not the five UPS transit bands this map used to
  carry. Derived from the USPS Domestic Zone Chart (postcalc.usps.com) on
  2026-08-31; re-derive if USPS republishes the chart for origin 266.

Green states (alphabetical): CT, DE, IL, IN, KY, MA, MD, ME, MI, MN, NC, NH, NJ,
NY, OH, PA, RI, VA, VT, WI, WV.

## Live rate feed — `GET /grove/api/v1/shipping/rates` (GOL-952)

`rate_feed()` serves the in-memory rate table and zone map as read-only JSON so
the storefront product-page estimator prices against exactly what checkout will
charge, instead of a bundled snapshot that drifts as the checker rewrites
`shipping_rates.json`. Public, no Shippo call, no DB read.

```json
{
  "zones": { "zone_1": { "bareroot": {"base": 21.0}, "potted": {"base": 32.0} }, ... },
  "zone_by_state": { "WV": "zone_1", ... },
  "green_states": ["CT", "DE", ...]
}
```

`zones` mirrors `data/shipping_rates.json`; the frontend drops it into
`resolveRateTable()` (grove-sites `apps/nursery/lib/shipping-estimate.ts`).
`zone_by_state` is the authoritative green-list zone map — the frontend
eligibility gate must stay in lockstep with it. Because the feed is served from
the same module globals `compute_shipping_rate` prices with, it can never
disagree with the checkout engine within a running instance.

## Product tier — `grove_shipping_tier`

`product.template` carries a `grove_shipping_tier` selection field (`bareroot` or
`potted`). The checkout reads this field per order line and passes it to
`compute_order_shipping` for per-tree tiered pricing. Untagged products default to
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

- Runs daily at 07:00 ET via GitHub Actions cron
- Fetches real USPS Ground Advantage quotes from Shippo for each zone × tier parcel profile
- If any rate drifts ≥ $1.00 from the JSON file, opens a PR to update
  `data/shipping_rates.json` and posts a Discord notification
- Gated on `SHIPPO_API_KEY` (required — the run is skipped cleanly when absent).
  `DISCORD_OPS_WEBHOOK_URL` is optional — the Discord notification step is
  individually skipped when the secret is absent (safe to merge before
  credentials exist)
