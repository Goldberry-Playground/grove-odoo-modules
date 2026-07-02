# Shipping Zones — Live System Reference (GOL-15)

Canonical design: vault **wiki/Software/Grove Shipping** (2026-07-02).

## Rate table — `data/shipping_rates.json`

Zone rates are stored in `grove_headless/data/shipping_rates.json` and loaded at
startup by `models/shipping_zones.py`. The file ships with provisional launch-
hypothesis values; the morning rate-checker (`scripts/rate_check/rate_check.py`)
replaces them with real Shippo-derived values via automated PR on its first run.

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
- **`ZONE_BY_STATE`** — maps each of the 21 green states to one of five rate zones
  (`zone_1` … `zone_5`), keyed by UPS ground transit distance from zip 26651 (WV).

Green states (alphabetical): CT, DE, IL, IN, KY, MA, MD, ME, MI, MN, NC, NH, NJ,
NY, OH, PA, RI, VA, VT, WI, WV.

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
- Fetches real UPS Ground quotes from Shippo for each zone × tier parcel profile
- If any rate drifts ≥ $1.00 from the JSON file, opens a PR to update
  `data/shipping_rates.json` and posts a Discord notification
- No-ops until `SHIPPO_API_KEY` and `DISCORD_WEBHOOK_URL` secrets are configured
  in the repository (safe to merge before credentials exist)
