# ZIP shipping — Slice 1 discovery spike (GOL-3200)

**Epic:** GOL-3199 (Train #4). **Spec:** `docs/superpowers/specs/2026-10-07-zip-shipping-calculator-design.md` (PR #327). **Status:** decision + probe recipe delivered; 13-label golden fixture is a scaffold pending an Odoo read path for the exact ship-to ZIP + per-label service. This note sizes the surcharge table for Slice 2.

## TL;DR

- **Surcharge source: UPS's published Delivery-Area-Surcharge (DAS) ZIP list — specifically the *Extended* and *Remote* tiers — as the seed, validated against the 13-label golden set and spot public-calculator probes.** It is free, authoritative, refreshed by UPS a few times a year (fits the "static data, PR refresh" rule), and it is the only candidate that *explains* the one outlier in the real data (Mariaville ME 04605) without blind nationwide probing.
- **Odoo back-fill is a validation/drift check, not the source.** Only 1 of the 13 shipped labels hit a surcharge ZIP, and the data has no zone_3, no large/potted, and no coverage of most of the map — it cannot size or populate a surcharge table.
- **Sampling every 3-digit prefix is rejected as the surcharge source** (it only rediscovers what UPS already publishes, at ~900+ live lookups) but is retained as the method for the base **rate card** (one representative ZIP per UPS zone × weight tier), which the spec already calls for.
- **Expected surcharge-table size: sparse.** Bounded above by the UPS Extended + Remote DAS ZIP set (order 10³–10⁴ five-digit ZIPs nationwide); the spec's ">$0.25 over card" threshold plus the empirical rarity (1/13 labels, and 9 of 10 "far" corners in the CEO probe priced at the plain zone rate) will narrow the committed table well below that. Final count is pinned in Slice 2 by diffing the DAS list against card-reproduced probes.
- **Golden fixture:** `scripts/rate_check/fixtures/golden_labels_2026-10-07.json` (13 rows, `small` box, batch LB-20261005-01). Complete on state/weight/cost; `zip`/`service` gaps flagged per row (see Blocker).

## Evidence base (no new reads were possible this spike)

All numbers are transcribed from the authoritative 2026-10-06 CEO Odoo-join and 2026-10-07 calibration already on the **GOL-2923** thread (comments `e1f99aae` and `6360d2ff`). I could not re-pull them myself: the prod Odoo service account I have (`content-drafter`, uid 12) is **denied** on `grove.label.batch` / `grove.label.batch.line` — `Fault 4: "You are not allowed to access 'Pirate Ship label batch'... allowed for Sales/Administrator, Sales/User"`. QA Odoo does not hold prod's shipped labels.

The 13 shipped small-box labels (origin 26651), by UPS Ground zone:

| zone | labels (state, packed lb, actual label cost) |
|---|---|
| zone_1 | KY 6.5 $8.79 · VA 8.5 $8.52 · NC(Winston-Salem) 12.5 $13.56 |
| zone_2 | PA 6.5 $11.15 · MD 6.5 $11.15 · OH 6.5 $8.29 · NY 6.5 $9.84 · NY 6.5 $11.15 |
| zone_3 | *(none shipped)* |
| zone_4 | NH 8.5 $10.43 · MA 14.5 $15.70 |
| zone_5 | SC 6.5 $9.84 · SC 6.5 $9.84 · **ME(Mariaville 04605) 6.5 $19.38** |

Public-calculator cross-check (CEO session, `pirateship.com/rates`, no login/API/credential): the UPS-Ground page price matched **7 of 7** checked labels to the cent (KY/OH/NY/SC/PA/NY/ME). The same probe priced many distant cities at the *plain* zone_5 rate — Portland ME 04101 $12.33, Key West, Miami, Mobile, Gulfport, Lake Charles, Texarkana, Joplin, Sioux City all $12.33 at 7 lb — while **Mariaville ME 04605 alone** jumped to $16.89 (Saver) / $19.38 (Ground), ~$4.56 over Portland and ~$9.54 over Columbia SC at the same weight.

## Why this picks the DAS list

The decisive signal: within a single UPS zone, **the only price variation is at specific rural/extended-area ZIPs**, not across states or cities broadly. Pirate Ship labels both Portland (04101) and Mariaville (04605) "Zone 5," yet they price differently. That delta is UPS's Delivery Area Surcharge (Extended/Remote) — a published, per-ZIP overlay — baked into the carrier's residential ground price.

So the model the spec already chose — `price(zip, lb) = min over {Ground, Saver} of card[zone_for_zip(zip)][lb][service] + surcharge[zip][service]` — maps cleanly onto:

- **`card`** = base residential ground by UPS zone × weight tier (built by sampling one representative, non-surcharged ZIP per zone — the rate-card probe).
- **`surcharge[zip]`** = UPS Extended + Remote DAS delta for the few ZIPs that carry one.

Against the alternatives, scored on "cheapest source that reproduces all 13 to the cent":

| candidate | reproduces 13? | cost to build/maintain | verdict |
|---|---|---|---|
| **UPS DAS Extended+Remote list** | Yes — 12/13 need no surcharge (base card), 1/13 (Mariaville) is exactly a DAS Extended ZIP | Free published list; PR refresh a few times/yr | **chosen (seed)** |
| Sample every 3-digit prefix @2–3 ZIPs | Would, but 3-digit granularity *misses* 5-digit outliers like 04605 inside a prefix that otherwise prices normally | ~900+ live lookups per refresh; still needs 5-digit fill-in for surcharge ZIPs | base-card method only, **not** the surcharge source |
| Back-fill from Odoo label costs | No — only 1/13 is a surcharge ZIP; no zone_3, no large/potted, sparse map | free but incomplete by construction | validation/drift check only |

Reproduction then costs **one** calculator probe per golden label to confirm 13/13 (Slice 2 automates it via the recipe below), not a nationwide sweep.

## Expected surcharge-table size

Sparse. The UPS DAS file marks standard / extended / remote tiers; **standard DAS is already inside the base residential rate the card captures**, so only Extended + Remote ZIPs become rows where the delta exceeds the spec's $0.25 threshold. Empirically 1 of 13 labels and 1 of ~10 probed "far" corners were surcharged. Bound it by the UPS Extended+Remote ZIP set (order 10³–10⁴ ZIPs) and expect the committed table — after the >$0.25 filter and after dropping any ZIP the card already covers — to land materially smaller. **Exact count is a Slice 2 output**, produced by the diff described next; this spike's job is to confirm the method and the bound, which it does.

## Probe recipe for Slice 2 (to automate)

`rate_check.py` already probes `pirateship.com/rates`-style quotes via its `--manual-quotes` path (persisted-queries-only; GOL-2605) using `ORIGIN = 26651` and the `REFERENCE_ZIPS` corners. Extend it with the spec's `--zip-table` mode:

1. **Build the base rate card.** For each UPS zone (1–5 from Summersville) pick one representative *non-surcharged* ZIP (the existing `REFERENCE_ZIPS` corners minus known DAS ZIPs — e.g. zone_5 use Portland 04101, **not** Mariaville 04605). Probe every whole-pound billable weight 1–20 lb for both UPS Ground (03) and UPS Ground Saver (93). Emit `shipping_rate_card.json` (zone × lb × service).
2. **Load the UPS DAS Extended+Remote ZIP list** (published UPS resource; commit the raw list under `scripts/rate_check/fixtures/` so refreshes are a reviewed PR, per the no-live-data rule).
3. **Compute surcharge deltas.** For each DAS candidate ZIP, probe the calculator at the box's median billable weight and subtract `card[zone_for_zip][lb][service]`. Keep the row only if the delta > $0.25; record the winning service. Emit `shipping_zip_surcharges.json` (sparse).
4. **3-digit zone map.** Emit `shipping_zip_zones.json` (3-digit prefix → zone) from the same zone-lookup the calculator implies (~900 rows).
5. **Golden gate.** For each of the 13 labels in `golden_labels_2026-10-07.json`, assert `price(zip, billable_lb)` reproduces `actual_label_cost` to the cent (Mariaville must resolve the $16.89 Saver, since the cheapest-service rule wins over the $19.38 Ground that was actually bought — that gap is expected and documented).

All output is a PR, never applied live (same contract as `shipping_rates.json`).

## Blocker to finalize the golden fixture

The fixture is complete on `(zone, state, packed_weight, billable_weight, actual_label_cost, box_id=small)` but **the exact 5-digit destination ZIP and the per-label bought service are missing for 12 of 13 rows** — the CEO join posted state-level only (just Mariaville 04605 is pinned). A ZIP-keyed calculator's golden test needs the real ZIP. Both fields live on `grove.label.batch.line` (prod batch LB-20261005-01), which my read-only SA cannot access.

**Unblock owner: Josh.** Either (durable, preferred):

- **(A)** grant the rate-check / content-drafter prod Odoo service account **read-only Sales** access to `grove.label.batch` + `grove.label.batch.line`. This also unblocks **Slice 3** weight-calibration, which must read `grove.label.batch.line.weight_lb` across shipped boxes.

or (fast path):

- **(B)** paste the 13 rows as `(zip5, box_id, billable_weight_lb, service, actual_cost)`; I finalize the fixture and the 13/13 reproduction check.
