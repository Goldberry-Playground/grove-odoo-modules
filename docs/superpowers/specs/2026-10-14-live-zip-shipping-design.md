# Live ZIP shipping quotes + nightly ZIP-area table (design)

**Status:** approved in brainstorm with Josh, 2026-10-14. Target: Release Train #4 (freeze Fri 2026-10-30, QA Mon 11-02, promote Wed 11-04; manifest GOL-3303). **Supersedes** `2026-10-07-zip-shipping-calculator-design.md` (static per-ZIP surcharge tables refreshed by PR) where they conflict; the parts that carry over are listed under "Kept from the 10-07 design".

## Decisions (Josh, 2026-10-13 / 10-14), do not re-open

- **Every shipping charge = Pirate Ship label cost + a flat $2.50 per tree order, nothing else.** No per-box packaging, no buffer, no round-up. Supersedes the $5.00 handling of GOL-2923 / gom#308 and the 10-07 design. Seed orders keep no checkout quote and no handling (GOL-3257). Shipped live as the 10-14 hotfix (prod pin `da63b78`, gom#343 for main; prod `grove_headless.shipping_handling_fee` = 2.50).
- **Service: run both UPS Ground and UPS Ground Saver on every quote and charge the cheaper**, subject to the existing dormant transit ceiling (`MAX_TRANSIT_DAYS = 7`, `scripts/rate_check/rate_check.py`); USPS Ground Advantage stays in the allowlist as today.
- **Live quote at checkout** for the customer's exact ZIP. Reverses the 10-07 rejection of live quotes, whose only blocker (a carrier credential) is gone: Pirate Ship's RatesQuery answers keyless when sent as their persisted query.
- **Static ZIP-area table** keyed by **3-digit ZIP prefix**, refreshed **nightly by Odoo into the database** (reverses the 10-07 "no live DB rate table / rate data only via reviewed PR" ruling). It powers the product-page estimator and is the checkout fallback.
- **Two numbers per area:** `typical` (median of the sampled ZIPs) for the estimator, `highest` for the checkout fallback, so the estimate is honest for most shoppers and the fallback never undercharges.
- **Alerting:** Discord when more than **20%** of live checkout quotes fall back in a rolling 1-hour window (minimum 5 quotes); a retired persisted query alerts immediately; individual fallbacks are logged per order and counted in the nightly summary. The nightly refresh aborts and alerts above 20% area failures; any area moving 25% or more keeps yesterday's value and alerts.

## The quote source

`POST https://ship.pirateship.com/api/graphql?opname=RatesQuery` with `extensions.persistedQuery.sha256Hash` = the SHA-256 of Pirate Ship's own rates query (`2692f7149bbb741a0fd343d1d59d8c02b0d4cf50abe36d3ebce54bdef2b73f85` as of 2026-10-05), taken from `taciturnaxolotl/pirateship-api` (MIT, `src/rates-query.ts`, which ships a `sync-query` drift check). Variables: `originZip` 26651, `destinationZip`, `isResidential: true`, `weight` in **ounces**, `dimensionX/Y/Z`, `mailClassKeys: ["03", "93", "GroundAdvantage"]`, `packageTypeKeys: ["Parcel"]`. Each rate returns `totalPrice` plus itemised `surcharges` (e.g. 26651: UPS "Delivery Area" $3.45; the 24" length fee on Saver/GA). Verified keyless with curl 2026-10-13; values equal the public pirateship.com/rates calculator to the cent.

It is an **unofficial, undocumented endpoint**. The design assumes it can rate-limit, block or change without notice, so every path has a fallback and nothing about checkout depends on it being up.

## Architecture

### 1. Pirate Ship client (`grove_headless/models/pirateship_rates.py`, pure Python)

- `quote(dest_zip, box_id, weight_lb) -> Quote | raises` returns per-service `{service, total, surcharges[], transit_days}` and the winner (cheapest of Ground / Saver / GA within the transit ceiling; if none fit, fastest wins, matching `rate_check`).
- 3 s total timeout. Typed errors: `QueryRetired` (persisted query unknown), `Blocked` (HTTP 403/429 or a challenge page), `Timeout`, `NoService` (no allowlisted rate for the box), `BadResponse`.
- The hash lives in one constant with a comment pointing at the upstream file. `rate_check.py` imports the same client so the daily/manual paths and checkout share one request shape.

### 2. Live quote cache

Key `(dest_zip, box_id, ceil(weight_lb))`, TTL 24 h, stored in a small model (survives worker restarts). A cache hit never calls Pirate Ship. Errors are not cached.

### 3. ZIP-area table (`grove.shipping.zip.rate`)

Fields: `prefix` (3 digits), `box_id`, `typical`, `highest`, `service` (winner at `highest`), `sample_zips`, `checked_on`, `held` (bool, set by the 25% guard), `held_value`. Prices are carrier cost only; the $2.50 is added at the order level. Visible and editable in Odoo (Inventory > Configuration > Shipping ZIP rates).

### 4. Nightly refresh (`ir.cron`, 01:00 to about 03:00 ET)

- For every prefix in the vendored PHZM ZIP list already used by `usda_zone_for_zip`, pick 2 to 3 sample ZIPs (stable choice per prefix, stored in `sample_zips`) and quote each box (`p24x10x4` at 6 lb, `p24x10x6` at 13 lb, the 10-13 typical-order weights) with a few seconds between requests.
- `typical` = median sampled price, `highest` = max.
- Guard: a new value 25% or more away from yesterday is not applied; `held` is set, Discord is told, and an Odoo button applies it.
- Abort: `QueryRetired`, or more than 20% of prefixes failing, stops the run, keeps yesterday's table and alerts.
- One Discord summary per run: prefixes updated / held / failed, plus yesterday's count of checkout fallbacks.

### 5. Checkout (`shipping_zones.compute_order_shipping`, `controllers/main.py`)

- Gains `dest_zip`. Pack the cart exactly as today (`p24x10x4` cap 10, `p24x10x6` cap 20 for bareroot; P&B 5/10). Each box is quoted at its **real packed weight** (tare + trees × lb/tree).
- Price chain per box: live quote → ZIP-area `highest` → zone table in code (the 10-14 hotfix rates). Total = sum of boxes + one $2.50 per tree order.
- Each order stores `grove_shipping_source` (`live` / `zip_area` / `zone`), the fallback reason, and per-box quoted service + price, so the label tool can show quoted vs purchased.
- Behind `grove_headless.zip_live_quotes_enabled` (default off). Off = today's zone table.

### 6. Estimator + feed

- `/shipping/rates` and the PDP estimate accept `zip`; with a ZIP they return the area's `typical` for the box the selected pack fills, labelled "Estimated shipping to {zip}". Without a ZIP they keep today's zone answer.
- Storefront (grove-sites): the estimator asks for a ZIP instead of a state and remembers it; checkout pre-fills it.

### 7. Query-drift watch

A weekly GitHub Actions job compares our hash with `taciturnaxolotl/pirateship-api` `src/rates-query.ts` on its default branch and opens an issue + Discord ping on mismatch. Updating the hash is a one-line PR.

## Error handling

| Event | Behavior |
|---|---|
| Live quote times out / errors / blocked / `NoService` for a box | That box falls back to ZIP-area `highest`, then the zone table. Logged on the order. Counts toward the 20%-per-hour alert. |
| `QueryRetired` | Live quotes disabled until the hash is updated (flag in a config param set automatically); immediate Discord alert, at most once per day. |
| Live price more than ±50% from the area's `typical` | Charge the live price, log, include in the nightly summary. |
| ZIP not resolvable | Existing state/green-list checks refuse it as today. |
| Area table empty or prefix missing | Zone table. |
| Nightly: >20% prefixes fail or `QueryRetired` | Abort, keep yesterday, alert. |

Settlement (deposit orders) is unchanged: actual label cost + $2.50 handling − deposit.

## Kept from the 10-07 design

Box catalog after #335 (`p24x10x4` / `p24x10x6`, bareroot 10/20 at 1.0 lb, P&B 5/10 at 2.0 lb); weight calibration (#329) for the label tool; label batch pre-selects the quoted service and shows quoted vs purchased; the drift alarm on (actual label − quoted) over the last 20 labels; seeds never priced at checkout.

## Dropped from the 10-07 design

The per-ZIP surcharge JSON and its generator, the surcharge-source discovery spike (gom#328 / GOL-3202), browser-pane probing as the data source, PR-gated rate data, and the $5 handling.

## Testing

- Client: parses recorded real responses (26651 with the $3.45 Delivery Area surcharge, 28401 without, a `QueryRetired` error, a 403 challenge, a timeout); cheapest-within-ceiling selection; ounces conversion.
- Checkout: $2.50 added once for 1-, 2- and 3-box carts; each fallback level reached and recorded; flag off = zone table byte-for-byte.
- Nightly job: median/max, 25% hold, 20% abort, `QueryRetired` abort, summary content.
- Alerting: the 1-hour 20% window with the 5-quote minimum; `QueryRetired` immediate and deduped daily.
- Golden: the 13 historical labels (gom#328 fixture), each live-quoted at its shipped weight, within $0.50 of the actual cost.
- QA gate (Train #4): a rural and an urban ZIP checkout both equal the pirateship.com/rates calculator + $2.50; a forced-fallback checkout (client pointed at a dead host) completes on the area table and records `zip_area`.
- CI command unchanged: `python3 -m pytest grove_headless/tests/ scripts/rate_check/tests/ scripts/tests/`.

## Rollout (Train #4)

| Step | Owner | Content |
|---|---|---|
| 1 | Ada | Client, cache, checkout fallback chain, order fields, alerts; flag default off |
| 2 | Ada | ZIP-area model, nightly cron, guard/abort, summary; seed the table on QA by running the cron |
| 3 | Iris | ZIP-based estimator, remembered ZIP, checkout pre-fill |
| 4 | Terra | Weekly hash-drift workflow, alert webhook wiring, QA gate checks |
| 5 | Josh | After promote, set `grove_headless.zip_live_quotes_enabled` and watch Discord for a day |

Rollback: unset the flag; the estimator keeps the area table, checkout returns to the zone table.
