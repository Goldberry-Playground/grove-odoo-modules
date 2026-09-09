# Fulfillment v1 — ATGN pack/ship workflow in Odoo (design)

Ratified in chat by Josh 2026-09-08/09 (brainstorm) — Odoo-native backbone, no
custom phone page or barcode in v1 (both are explicit later add-ons).

## Problem

Staff have no working screen for fulfillment: orders are only visible through
generic dashboards. The needed flow is: see pickup vs ship orders, mark an
order **boxed** (recording the real box + actual weight) and **shipped**
(label on box, dropped at carrier), with the customer emails and state machine
that already exist firing off those marks. "Received" is the automatic intake
step (Stripe webhook marks paid + Discord alert) — no manual mark.

## Design

### 1. Warehouse config (data/config, no code)

- ATGN warehouse switches to **two-step outgoing**: *Pull & Pack* → *Ship*.
  (Three-step is a config toggle later if pulling becomes a separate crew's
  session; Odoo's own docs call three-step redundant for a one-person flow.)
- **Package types** seeded from the real box catalogs with tare weights:
  bareroot `small` 24x6x4 / `large` 24x9x6, potted `p24x10x4` / `p24x10x6`
  (tares from shipping_boxes.py, single source of truth — seed script reads
  the catalogs, never copies numbers).
- Pickup orders stay **one-step**; validating the transfer = *collected*.

### 2. Fulfillment menu (grove_headless view XML, no new models)

Top-level **Fulfillment** menu:

- **To Box** — Pull & Pack transfers, ready first, pickup/ship badge (exists).
- **To Ship** — Ship transfers awaiting validation.
- **Pickups** — pickup transfers (reserved → collected).
- **All Outstanding** — sale orders kanban grouped by `grove_fulfillment_stage`
  (exists, stored+indexed), pickup and ship lanes.

### 3. Glue (the only real code)

- **Boxed** = validating Pull & Pack with *Put in Pack*: box type required,
  actual `shipping_weight` entered (two taps + a number). No state-machine
  stage change (boxed is picking state, not a lifecycle stage).
- **Shipped** = validating the Ship transfer → calls the existing
  `_operator_mark_shipped` orchestration (GOL-1980): idempotent GOL-1981
  transition + ship-time settlement + ONE customer "shipped" email via
  `_apply_delivery_status`.
- **Collected** = validating a pickup transfer → `action_grove_mark_collected`.
- **Label buy prefers actual packages**: when the order's pack step recorded
  packages, `action_buy_shipping_labels` builds parcels from the real box
  dims + recorded weight instead of the theoretical `pack_for_state` plan
  (which stays as fallback). This closes the estimate-vs-actual loop.

### 4. Discord button interplay (Josh's question: "should work together?")

Yes, by construction — the Discord bridge (GOL-1975 phase 2, blocked, owned
by the bot) calls `POST /grove/api/v1/orders/<id>/mark-shipped`, and the Odoo
Ship validation calls the same `_operator_mark_shipped`. The GOL-1981
transition is idempotent and `_apply_delivery_status` emails only on a real
status change, so pressing both buttons (or Shippo's transit scan racing
either) produces exactly one transition and one email. No coordination code
needed; a test asserts button-then-endpoint double-fire is a no-op.

### 5. Explicitly out of scope for v1

Barcode/phone page (later add-on, slot preserved), material-cost accounting
beyond tare weights, batch picking, three-step delivery, actual-vs-table rate
calibration report (data starts accumulating now; report is a follow-up).

## Error handling

- Ship validation with no label bought: warn, do not block (manual/pickup-day
  edge); mark-shipped still settles + emails per GOL-1980 contract.
- Package missing weight: block Ship validation with a clear message (weight
  capture is the point).
- All state transitions stay best-effort/idempotent exactly as GOL-1980/1981
  built them — a mail or Discord failure never rolls back a validation.

## Testing

- Odoo-runner tests: picking validation drives the watermark (ship + pickup
  paths), double-fire idempotency across Odoo button + bearer endpoint,
  label-from-actual-packages parcel construction with theoretical fallback.
- Config seeds (package types, route settings) asserted by a post-init test.
- Views ride the existing install-smoke job (view XML parses + installs).

## Rollout

QA first (floats main), then prod pin bump + `-u grove_headless` (warehouse
route change needs the deliberate upgrade path). Note the standing rate-table
constraint from memory: do not ship a UPS-priced rate table; the USPS-aware
selector (PR #221) must land before the next rate re-derive.
