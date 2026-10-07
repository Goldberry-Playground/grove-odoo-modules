# ZIP-based shipping calculator (design)

**Status:** approach ratified by Josh in brainstorm, 2026-10-07. Targets **Train #4** (up Mon 2026-11-02, promote Wed 11-04, teardown Thu 11-05; freeze Fri 10-30). GOL-2923 stays the interim zone-table fix and ships first; this replaces its state-zone table.

## Decisions (Josh, 2026-10-07), do not re-open

- **Goal:** checkout shipping = actual Pirate Ship label cost + one flat **$5.00 handling per order**, tight in BOTH directions (cheap ZIPs get cheaper, expensive ZIPs fully covered). Not "never under-quote at any price".
- **Approach A:** UPS zone chart + rate card + per-ZIP surcharge overlay, all static repo data refreshed by PR. Rejected: probing all ~41k ZIPs (B), live carrier quote at checkout (C: needs a credential and a runtime dependency).
- **Service rule:** the quote and the label use the **cheapest of UPS Ground / UPS Ground Saver** for that ZIP and weight. The label-batch tool must pre-select that service so the bought label equals the quoted one.
- **Handling:** $5.00 per ORDER (not per box), added once in the checkout total and in settlement (ruling on GOL-2923, 2026-10-06).
- **Zone_5 corner:** Mariaville ME 04605 stays a reference corner for the interim table. Potted is no longer shipped (ruled 2026-10-07); ignore potted boxes everywhere in this design.
- Still holds from the 2026-09-09 Pirate Ship design: rate data changes flow through a reviewed PR and a modules pin bump; **no live DB rate table**, no reverse-engineered Pirate Ship purchase API, no new credentials.

## Why state zones are too coarse (evidence, 2026-10-07)

13 shipped small-box labels, joined Pirate Ship export to Odoo, origin 26651:

- Portland ME 04101 quotes $12.33 and Mariaville ME 04605 quotes $16.89 (Saver) / $19.38 (Ground) for the same 7 lb small box. Pirate Ship labels both "Zone 5". The gap is a ZIP-level cost (extended area / rural), not a state or UPS-zone difference.
- Zone_2, zone_3 and zone_4 price identically for every box from Summersville.
- State zone_5 spans $9.84 (Columbia SC) to $19.38 (Mariaville ME) on the same box.
- The public calculator at pirateship.com/rates reproduced 7 of 7 checked actual label costs to the cent (UPS Ground), so it is a valid reference.
- Weight moves the price as much as geography: most small boxes pack at 6.5 lb but some at 12.5 and 14.5 lb.

## Non-goals

- Potted, bagged or pickup orders (no shipping charge changes).
- Changing the deposit rule, settlement mechanics, or the Stripe flow beyond using the new quote.
- Per-ORDER dynamic carrier quotes. All price data is static between refresh PRs.

## Architecture

Three independently shippable pieces, in this order.

### 1. ZIP rate data + generator (`scripts/rate_check/`, `grove_headless/data/`)

Static data, versioned in the repo, loaded once at import like `shipping_rates.json` is today.

- `shipping_zip_zones.json`: 3-digit ZIP prefix -> UPS Ground zone from origin 26651 (about 900 rows).
- `shipping_rate_card.json`: for each UPS zone x billable-weight tier (1 to 20 lb, whole pounds, covering the box catalog) the UPS Ground and UPS Ground Saver price. Read from the public calculator at one representative ZIP per zone.
- `shipping_zip_surcharges.json`: per-ZIP (5 digit) extra cost over the rate card for rural / extended-area ZIPs, plus the service that wins there. Sparse: only ZIPs that differ from the card by more than $0.25.
- Generator: extend `rate_check.py` with a `--zip-table` mode driven by the same `--manual-quotes` style input or by the browser-pane probe recipe (public calculator, no API, no credential). Output is a PR, never applied live.
- Discovery spike (first task): confirm how surcharge ZIPs are best found. Candidates: UPS's published delivery-area-surcharge ZIP list, sampling every 3-digit prefix at 2 to 3 ZIPs, and back-filling from actual label costs in Odoo. Pick the cheapest source that reproduces the 13 known labels to the cent. Outcome decides the size of the surcharge table.

Price lookup: `price(zip, billable_lb) = min over {Ground, Saver} of card[zone_for_zip(zip)][lb][service] + surcharge[zip][service]`, returning the winning service for the label tool.

### 2. Weight model calibration (`grove_headless/models/shipping_boxes.py`)

`actual_weight_lb(box_id, count, mode)` already models packed weight as terms (tare + per-tree weight x count, `PER_TREE_LB`). It is priced at a representative worst-case weight today. Change:

- Price each packed box at its **estimated actual billable weight** (rounded up to the carrier pound), not the full-capacity representative weight.
- Calibrate `PER_TREE_LB` and tare against the recorded `grove.label.batch.line.weight_lb` for shipped boxes (median small box 6.5 lb, large about 12 lb). The calibration is a script run before each refresh PR that reports predicted vs. packed weight per box type; it never writes live.
- Large box and any box with fewer than 5 shipped samples keep the conservative weight and are flagged `weight_basis: unverified` in the rate feed.

### 3. Checkout, feed and label wiring (`models/shipping_zones.py`, `controllers/main.py`, `models/label_batch.py`)

- `compute_order_shipping(state, items, mode)` gains a `zip` argument. With a ZIP present and a ZIP table loaded it prices every packed box from the ZIP table; otherwise it falls back to the interim state-zone table (fail-safe, never "no price" for a ZIP we cannot resolve).
- Total = sum of per-box ZIP prices (carrier cost only) + one $5.00 handling per order. Handling appears as its own line so settlement and refunds reconcile.
- The `/shipping/rates` feed and the cart/PDP estimate accept an optional ZIP and return the same number checkout will charge; with no ZIP it returns the state-level worst case (current behavior), labelled as an estimate.
- Label batch: per row, pre-select the quoted service (Ground vs Saver) in the CSV and show the quoted cost next to the purchased cost so drift is visible before the batch is bought.
- Settlement already charges actual label cost + $5 - deposit; this design removes the typical gap rather than changing that code.

## Error handling

- Unknown or non-resolvable ZIP: use the state-zone worst case (GOL-2923 table). Log, do not block checkout.
- Data older than the existing rate-table staleness limit (`scripts/rate_check/staleness.py`, extended to cover the ZIP files): the rate feed marks `rates_stale: true` and the morning rate-check opens the refresh PR; checkout keeps working on the last data.
- A drift alarm fires when the mean of (actual label - quoted carrier cost) over the last 20 labels moves outside +/- $1.00 or any single label is more than $4 under quote. It posts to the existing Paperclip rate-check issue; it never changes prices by itself.
- Estimated weight below the carrier minimum or above `MAX_SHIP_WEIGHT_LB`: existing fail-safe (no shipping line).

## Testing

- Golden tests: the 13 known labels (ZIP, box, weight, expected actual cost) reproduce to the cent through `price()`.
- Property tests: price is non-decreasing in weight; Saver/Ground choice is always the minimum; ZIP prefix lookup covers every US ZIP in the PHZM table already shipped for USDA zones.
- Fallback tests: unresolvable ZIP returns the state-zone worst case; handling is added exactly once for 1-box and 3-box carts.
- Calibration script test on a fixture of `weight_lb` samples.
- Existing `scripts/rate_check/tests` and `grove_headless/tests` keep passing; CI command is `python3 -m pytest grove_headless/tests/ scripts/rate_check/tests/ scripts/tests/`.

## Rollout (Train #4)

1. Before the Oct 30 freeze: discovery spike result + data generator PR + golden tests (data only, inert).
2. Train #4 up (11-02): checkout/feed wiring behind a config param `grove_headless.zip_shipping_enabled` default OFF; QA compares ZIP price against actual label cost for a handful of seeded orders.
3. Promote (11-04) only if QA gates pass; flip the param at promote, same pattern as Stripe Tax (ships inert, flipped on pass).
4. Rollback: unset the param; the state-zone table is untouched.

## Open questions (resolved in the discovery spike unless Josh rules first)

- Source of the surcharge ZIP list (see piece 1).
- Whether the cart/PDP ZIP field is a new input or reuses the shipping-zone ZIP the storefront already collects for USDA zone.
- Fate of the p24x10x4 / p24x10x6 cells in `shipping_rates.json` (inert after potted shipping ended).

## Issue slicing (Paperclip, one epic on the Grove board, labelled Train #4)

1. Discovery spike: surcharge source + golden dataset (13 labels) - Ada.
2. ZIP data generator + data files + staleness/drift checks - Ada.
3. Weight-model calibration script + `weight_basis` flag - Ada.
4. Checkout/feed/label-tool wiring behind the flag - Ada.
5. QA plan + seeded orders + promote gate - owner decided at Train #4 kickoff.
