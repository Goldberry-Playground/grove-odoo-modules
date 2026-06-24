# 12-Zone Shipping Pricing (GOL-15)

Square's API can't build shipping rate profiles, so the 12-zone shipping
pricing is implemented in the Grove headless checkout (`grove_headless`)
instead. This doc is the fill-in template that turns Josh's
"full 12-zone shipping pricing doc" (Obsidian vault, 2026-06-23) into the
two tables the rate engine reads.

## Status

| Piece | State |
| --- | --- |
| Rate engine (`models/shipping_zones.py`) | ✅ done, unit-tested |
| Checkout integration (`controllers/main.py` → `_apply_shipping_line`) | ✅ done, no-op until data lands |
| Contract + coverage tests (`tests/test_shipping_zones.py`) | ✅ done |
| **12-zone rate table (the data)** | ⛔ **BLOCKED — Rick to provide** |

The engine is fail-safe: with the tables empty, checkout adds **no** shipping
line and behaves exactly as before. Filling the two tables below is the only
remaining step — no further code changes are needed.

## How to unblock (for Rick)

Paste the doc's numbers into `models/shipping_zones.py`:

1. **`ZONE_BY_STATE`** — assign every US state/territory to one of the 12 zones:

   ```python
   ZONE_BY_STATE = {
       "WV": "zone_1", "OH": "zone_1", "KY": "zone_1",   # …
       "PA": "zone_2", "VA": "zone_2",                    # …
       # … through zone_12; every state in US_STATES gets exactly one zone
   }
   ```

2. **`ZONE_RATES`** — one rule per zone:

   ```python
   ZONE_RATES = {
       "zone_1":  {"base": 8.00},
       "zone_2":  {"base": 10.00, "free_over": 75.0},
       "zone_12": {"base": 28.00, "per_lb": 0.75},
       # …
   }
   ```

   Rule fields (only `base` is required):
   - `base` — flat charge for any order shipping to the zone.
   - `per_lb` — optional surcharge per pound of order weight.
   - `free_over` — optional: shipping is free when the order subtotal ≥ this.

Once filled, `tests/test_shipping_zones.py` automatically enforces full state
coverage and that every zone has a rate.

## Open questions for Rick (confirm against the doc)

1. **Zone basis** — are the 12 zones grouped by destination **state/region**
   (what this engine assumes), or by USPS distance band, or by mileage from the
   farm? If it's distance-from-origin rather than a fixed state map, the lookup
   needs the origin ZIP and a distance-band table instead of `ZONE_BY_STATE`.
2. **Weight / size rules** — flat per-zone, or per-pound / per-tier? The engine
   supports `per_lb` today; weight *brackets* (e.g. 0–5 lb, 5–20 lb) would need
   a small extension.
3. **Free-shipping thresholds** — any `free_over` amounts, global or per-zone?
4. **Territories** — do we ship to PR/VI/GU/AS/MP? They're in `US_STATES` for
   now; drop any we don't serve.
5. **Tax on shipping** — is the shipping charge itself taxable under WV rules?
   (Currently the shipping line carries no tax; easy to change.)

## Rate table (paste the doc here for the record)

| Zone | States / region | Base | Per-lb | Free over |
| --- | --- | --- | --- | --- |
| zone_1 |  |  |  |  |
| zone_2 |  |  |  |  |
| zone_3 |  |  |  |  |
| zone_4 |  |  |  |  |
| zone_5 |  |  |  |  |
| zone_6 |  |  |  |  |
| zone_7 |  |  |  |  |
| zone_8 |  |  |  |  |
| zone_9 |  |  |  |  |
| zone_10 |  |  |  |  |
| zone_11 |  |  |  |  |
| zone_12 |  |  |  |  |
