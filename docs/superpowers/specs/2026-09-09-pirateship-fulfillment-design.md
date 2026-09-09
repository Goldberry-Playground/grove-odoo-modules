# Pirate Ship fulfillment — rates, labels, carrier events (design)

**Status:** approved in brainstorm with Josh, 2026-09-09. Supersedes the *label channel* and *tracking source* rows of `2026-09-09-fulfillment-v1-design.md` and the "Label channel: Shippo" locked decision in the vault page `Software/Grove Shipping`. Everything else in v1 (warehouse config, fulfillment menu, mark-shipped glue, Discord interplay) stands.

## Decision (Josh, 2026-09-09)

Pirate Ship is the label channel. It is always the cheapest provider for our boxes (2026-09-09 probe: every one of the 20 zone×box cells is lower than the Shippo-derived table, net −$235; zones 4–5 by ~$27 per box because Shippo quotes undiscounted UPS retail there) and the only provider that sells the label at that price. Shippo is retired from all three roles it plays today: quoting, buying, tracking.

Three sub-projects, independently shippable, in this order: **A** (rate source) → **B** (labels) and **C** (carrier events) in parallel.

Ruled during brainstorm, do not re-open:

- Rate changes keep flowing through the existing morning rate-check **PR** (human merge + modules pin bump). No live-DB rate table.
- Labels are bought on Josh's Pirate Ship account. The automated path drives the real Pirate Ship web app through a browser profile Josh signed into once; the **manual path must always exist and use the same artifacts**.
- Shipped / delivered events come from **polling UPS and USPS tracking APIs directly**, not from Shippo Tracks and not from Pirate Ship's recipient emails.

## Non-goals

- No live rate table in Odoo; the repo JSON stays the source of truth.
- No reverse-engineered Pirate Ship purchase API. The runner clicks what a human clicks.
- No change to checkout pricing, the `/shipping/rates` feed, the order-confirmation email on payment, or the fulfillment state machine.
- Wiring the storefront estimator to the feed (it still prices off a baked snapshot) is a known gap, tracked separately.

---

## A. Rate source: Pirate Ship replaces Shippo in the morning rate-checker

**Where:** `scripts/rate_check/rate_check.py`, `scripts/rate_check/tests/`, `grove_headless/data/shipping_rates.json` (schema bump), `.github/workflows/rate-check.yml`.

**What changes**

1. `quote_zone_box(zone, box_id)` posts to Pirate Ship's rate calculator (`POST https://ship.pirateship.com/api/graphql?opname=RatesQuery`, the `RatesQuery` operation, no auth) once per reference corner with the box's dimensions and its representative billable weight **in ounces**, requesting mail classes `03` (UPS Ground), `93` (UPS Ground Saver), `GroundAdvantage` (USPS Ground Advantage) and package type `Parcel`, residential destination. The UPS keys are Pirate Ship's UPS service codes (their `BatchRoutes` bundle map, verified 2026-09-09); the wrapper types only list USPS keys.
2. Carrier selection stays "cheapest allowlisted ground within the per-mode transit ceiling". The allowlist becomes `{("UPS","03"),("UPS","93"),("USPS","GroundAdvantage")}`. Transit days are derived from Pirate Ship's `deliveryDescription` estimated-delivery date minus the probe date; a rate with no parsable date is *not* excluded (unknown ≠ slow, same rule as today).
3. Per-box **max across the zone's corners**, `ceil(quote + packaging + 2.00)`, the ≥ $1 drift threshold, the monotonicity guard, the PR + Discord steps, and the `#210` draft-on-red self-gate are unchanged.
4. `shipping_rates.json` `_schema` 2 → 3: each cell becomes `{"base": 22.0, "carrier": "UPS", "service": "03", "service_title": "UPS Ground"}`. The Odoo loader (`shipping_zones._load_rates`) reads `base` only and ignores the extra keys, so the runtime is backward compatible; the feed passes `carrier`/`service_title` through for storefront copy. The winner is recorded because sub-project B needs it (which service to pick per box in Pirate Ship) and the visibility report prints it per box, so "which carrier set this rate" is never a question again.
5. The `SHIPPO_API_KEY` guard step is deleted; the workflow needs no secret to quote. `present_carriers` / the visibility report keep their shape (X/N probes per service).
6. Fixtures: one captured Pirate Ship response per box shape under `scripts/rate_check/fixtures/pirateship_rates_*.json`; the Shippo fixtures are deleted with the Shippo code path.

**Error handling:** an HTTP error or a GraphQL `errors[]` on a corner is logged and the corner skipped (as today); a box with **no** corner quoting exits non-zero with the existing "no ground rates" message rather than publishing a gap. A run whose proposed table breaks monotonicity fails as today.

**Testing:** pure-Python tests over the fixtures: winner selection incl. the transit ceiling, ounces conversion, schema-3 cell shape, drift computation unchanged, visibility report shows the winning service per box. `test_potted_coverage_is_all_or_nothing` and the rate-table invariants keep passing.

**Acceptance:** the first run opens a rate PR whose 20 cells match the 2026-09-09 local probe within $1, every cell carries `carrier`/`service`, prod pricing after merge + pin shows the new rates on `/shipping/options`.

---

## B. Labels: Odoo owns the batch, `grove-shipper` does the clicking, manual stays first-class

### B1. Odoo side (grove_headless)

**New model `grove.label.batch`** (`grove_headless/models/label_batch.py`): `name` (sequence `LB-YYYYMMDD-NN`), `state` (`open` → `exported` → `purchased` | `cancelled`), `order_ids` (M2M sale.order), `row_count`, `expected_total` (sum of committed per-box rates), `csv_export` (attachment), `tracking_import` (attachment), `purchased_at`, `purchased_total`, `notes`. One open batch at a time per company.

**Row source.** Eligible orders: `grove_fulfillment_stage in ("awaiting_label","wave_assigned")`, `grove_fulfillment != "pickup"`, no `grove_tracking_numbers`, and — for preorders — the ship wave is open (the existing dormancy gate `shippo_client`/`can_ship_bareroot` logic moves to a carrier-neutral helper; labels still fail closed outside the window). Boxes come from the same packer that priced the order (`shipping_zones.pack_for_state`, Box Engine v2), one CSV row per packed box:

| column | value |
|---|---|
| `Grove Ref` | `S01234/1`, `S01234/2` … (order name / box index) — the round-trip key |
| `Order` | order name |
| `Name`, `Email`, `Phone` | `partner_shipping_id` |
| `Address 1`, `Address 2`, `City`, `State`, `Zip`, `Country` | shipping address, `US` |
| `Weight (lb)` | box representative billable lb (or actual packed weight when v1's package weights exist) |
| `Length`, `Width`, `Height` | box dims, inches |
| `Service` | the table's `service_title` for that zone/box (e.g. `UPS Ground`) — informational for the buyer |
| `Committed Rate` | the table `base` for that zone/box |
| `Rubber Stamp 1` | order name (prints on the label) |

Pirate Ship accepts arbitrary headers, per-row weight/dims, and carries imported columns through to its tracking export, which is what makes the round trip safe.

**Endpoints** (same auth as the other `/grove/api/v1/*` operator routes):

- `POST /grove/api/v1/labels/batch` → creates or returns the open batch, builds the CSV, stores it, marks orders with `grove_label_batch_id`, state `exported`. Returns `{batch_id, name, csv_url, rows, expected_total}`. Idempotent: calling again returns the same batch unless it is `purchased`.
- `POST /grove/api/v1/labels/batch/<id>/tracking` (multipart CSV = Pirate Ship's *Export Tracking Data* file) → the reconcile:
  1. parse rows, require `Grove Ref`, tracking number, carrier/service, cost;
  2. **validate all rows before writing anything**: every ref belongs to this batch, its order is still awaiting a label, tracking numbers pass `shippo_client.is_valid_tracking` (kept as a pure helper), no ref appears twice, cost is numeric;
  3. per order: write `grove_tracking_numbers`, `grove_shipping_carriers` (`"UPS ups_ground"` / `"USPS usps_ground_advantage"` — the existing vocabulary), `grove_actual_shipping_cost`, `grove_label_urls` (blank; labels print from Pirate Ship), then `_grove_advance_state("label_purchased", source="pirateship")`, which fires the existing tracking notice;
  4. batch → `purchased`, `purchased_total`, `purchased_at`; return `{orders_advanced, skipped_already_tracked, total}`.
  Any validation failure returns 400 with the offending refs and writes nothing.

**Odoo UI (manual path):** Fulfillment menu gets "Export Pirate Ship batch" (downloads the CSV) and "Import Pirate Ship tracking" (file wizard calling the same reconcile code). A batch form shows rows, expected vs purchased totals, and the two attachments. `action_buy_shipping_labels` (Shippo) is removed; `SHIPPO_API_KEY` stops being read anywhere in grove_headless once C lands.

**Switch:** `ir.config_parameter grove_headless.pirateship_autobuy` (`"0"` seed). Read by the runner (below) via `GET /grove/api/v1/labels/config`; flipping it in Odoo Settings changes the next scheduled run from review-only to buy, no deploy.

### B2. Runner: `tools/grove-shipper/` (Node 22 + Playwright, in grove-odoo-modules)

A CLI with a persistent Chromium profile directory (`~/.grove-shipper/profile`). **First run is headed and interactive: Josh logs into Pirate Ship himself, enters the emailed 2FA code, ticks "Stay signed in".** The runner never holds the password; the profile is the credential. Odoo API key + Discord webhook come from env (`op run`).

`grove-shipper run [--buy|--dry-run] [--batch <id>]` (dry-run default):

1. **pull** — `POST /labels/batch`; exit 0 "nothing to ship" on zero rows; refuse to proceed on a batch already `purchased`.
2. **upload** — Ship → *Upload a spreadsheet* → attach CSV → apply the saved field mapping (created once, by name `Grove batch v1`; the runner asserts the mapping resolved `Grove Ref`, address, weight, dims). Assert every row imported with no address-validation flag; on any flag, stop, screenshot, report the refs.
3. **price check** — read the per-row quoted service and price; assert each row's service matches its `Service` column and `price ≤ Committed Rate + 2.00`; assert the batch total ≤ `expected_total + 2.00 × rows`. Any breach stops before purchase.
4. **buy** — only with `--buy` (or the autobuy switch on when scheduled): click *Buy labels*, wait for the confirmation page. `--dry-run` stops at the review screen and saves a screenshot to `out/`.
5. **export** — click *Export Tracking Data*, save the CSV, POST it to `/labels/batch/<id>/tracking`; download the label PDF batch to `out/<batch>/labels.pdf`.
6. **verify** — re-read the batch from Odoo: every Grove Ref has a tracking number and carrier; `purchased_total` within $0.01 of the Pirate Ship confirmation total; row count matches Pirate Ship's *Transaction History* entry for the batch. Post one Discord message to `DISCORD_ORDERS_WEBHOOK_URL`: batch name, rows, total, link to the batch, and any mismatch. A mismatch leaves affected orders in `awaiting_label` and exits non-zero.

**Failure posture.** Every step asserts the page it expects before acting; a UI change fails the run loudly *before* money moves. Re-running after a failure is safe: the batch id is the idempotency key, and the reconcile refuses to re-write tracked orders. Timeouts: 60 s per page action, whole run 15 min.

**Where it runs.** On Josh's Mac via launchd on ship days first (he is the trusted device). Moving to the ops droplet later = same code, profile seeded once through a headed session.

**Manual path (always available).** Download the batch CSV from Odoo → upload in Pirate Ship by hand → buy → *Export Tracking Data* → import in Odoo. Same file formats, same endpoints, same outcome.

**Testing:** Odoo tests for batch building (rows per packed box, eligibility, preorder wave gate, idempotent re-export) and the reconcile (all-or-nothing, bad ref, duplicate ref, already-tracked skip, state advance + one email). Runner: Playwright tests against a local HTML fixture of the Pirate Ship pages for the assertions, plus a `--dry-run` smoke against the real site with a 1-row batch on QA.

**Acceptance:** one real batch on QA Odoo bought via `--buy` on a test-mode-free Pirate Ship account with a single cheapest label (Josh's call which order), tracking lands on the order, the label-purchased email goes out, `grove_actual_shipping_cost` equals the Pirate Ship charge, Discord summary posted.

---

## C. Carrier events: Odoo polls UPS and USPS directly

**Where:** `grove_headless/models/carrier_tracking.py` (new, pure clients + status mapping), `data/carrier_tracking_cron.xml`, controller change to retire the Shippo webhook.

**Clients.** Two thin, stdlib-only `requests` clients:
- **UPS Track API** — OAuth 2.0 client credentials (`UPS_CLIENT_ID`, `UPS_CLIENT_SECRET` from a UPS developer app with the Tracking product; token cached in memory until expiry). `GET /api/track/v1/details/{tracking}`.
- **USPS Tracking v3** — OAuth 2.0 client credentials (`USPS_CLIENT_ID`, `USPS_CLIENT_SECRET` from developers.usps.com; Web Tools is retired). `GET /tracking/v3/tracking/{tracking}`.

Both credentials live in 1Password (`Grove Prod` / `Grove QA`) and reach Odoo through the same compose `${VAR:-}` passthrough as the Stripe keys.

**Status mapping** into the vocabulary `shipment_email` already consumes: UPS/USPS "in transit / accepted / departed" → `transit`; "out for delivery" → `out_for_delivery`; "delivered" → `delivered`; exceptions → `failure` (silent update, Discord ops note); unknown → no change. The existing `_apply_delivery_status` path keeps the once-only email guard and the `shipped`/`delivered` state transitions, so **the shipped, out-for-delivery and delivered emails are unchanged**.

**Cron** `grove_headless.poll_carrier_tracking`, every 2 hours: orders with tracking numbers in `label_purchased` or `shipped`; per tracking number pick the client from `grove_shipping_carriers`; apply the mapped status; stop polling an order at `delivered` or 30 days after label purchase (Discord ops note when the cap is hit). Per-call errors are logged and skipped; three consecutive auth failures for a carrier post one Discord ops alert and pause that carrier until the next run. The cron never raises.

**Retire Shippo:** `/grove/api/v1/shipping/webhook` and `GROVE_SHIPPO_WEBHOOK_TOKEN` are removed after C has run on prod for one full delivery cycle; `shippo_client.py` shrinks to the pure helpers still used (`is_valid_tracking`, tracking-URL formatting).

**Testing:** fixture-driven unit tests for both clients (token fetch, status mapping incl. out-for-delivery and exceptions), cron tests (skips terminal/aged orders, applies status once, never raises on client error). QA acceptance: a real label from B tracked to delivered with exactly one email per status.

---

## Rollout

1. **A** merges first; its first rate PR is the real proof (cells ≈ local probe). Prod picks the rates up on the next modules pin.
2. **B1 + C** ship in one modules release; **B2** runner tested `--dry-run` against QA, then one `--buy` on QA, then prod with `pirateship_autobuy=0` (review-only) for the first two ship days, then Josh flips the switch.
3. Shippo credentials revoked and the webhook deleted after the first prod delivery cycle completes on C.

## Prerequisites (Josh)

- UPS developer app (developer.ups.com) with the Tracking API enabled → client id/secret vaulted per stage.
- USPS developer app (developers.usps.com) → client id/secret vaulted per stage.
- One Pirate Ship spreadsheet upload by hand to (a) create and name the saved field mapping and (b) confirm `Grove Ref` survives into *Export Tracking Data*.
- Confirm the Pirate Ship account's postage payment method is funded for batch buys.

## Open questions (do not block A)

- Actual vs representative weight per box in the batch: use v1's real package weights when present, else the representative billable weight (proposed default).
- Whether Ground Saver should be allowed for potted/leafed (transit ceiling says yes only when its estimated date fits); the checker decides per probe, the buyer follows the `Service` column.
