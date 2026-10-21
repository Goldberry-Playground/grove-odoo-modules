# Order builder: four questions, a planned order, and the $500 consult (design)

**Status:** approved in brainstorm with Josh, 2026-10-14. Target: Release Train #4 (freeze Fri 2026-10-30, QA Mon 11-02, promote Wed 11-04; manifest GOL-3303). Builds on the growing-facts model (`2026-09-21-listing-content-gate-design.md`), the compliance carve-outs (`plant_compliance.py`, GOL-2132), bundle substitution (`bundle_substitution.py`, GOL-2237) and the ZIP zone lookup (`/grove/api/v1/zone`). Odoo stays the single source of truth; the storefront planner is a pure function over one catalog feed. Brainstorm notes: `gather-at-the-grove/docs/2026-10-14-order-builder-brainstorm.md`.

## What we are cloning

Whitetail Hill Chestnuts (whitetailhillchestnuts.com) sells trees through a four-question builder: where are you planting, what is the spot like, what are you trying to do, when do you want food on the ground. "Show me my order" writes a planned mix into the cart. The whole thing is a 20 KB client-side script over a JSON table of about ten facts per tree (zone range, drop months, first-crop years, wet/poor/shade tolerance, pollination minimum, role, priority). Nothing is server-side, nothing is AI. We copy the mechanism and change the goals: food forest first, agroforestry second, deer and wildlife third.

## Decisions (Josh, 2026-10-14), do not re-open

- **Question 1 is a ZIP code**, not a state. The ZIP resolves to a USDA zone through the existing lookup, and the state falls out of it for compliance. No North/Central/South chip.
- **Three tracks in this order: food forest, agroforestry, deer and wildlife.** Deer is a third chip at the bottom of the goal question, visually lightest, never hidden.
- **Approach A: client-side rule engine over one catalog endpoint.** Odoo publishes the planner facts for every plant in one cached JSON; the storefront planner is a pure TypeScript function with unit tests. No server-side planner, no LLM in the loop.
- **No Perenual paid tier.** Species the free tier gates (apple, black walnut, white oak, any id above 3000) are filled by the drafter agent or by hand from extension-service pages, with the source URL written into `grove_facts_provenance`.
- **Consult offer: cart subtotal after discounts and before shipping at or above $500**, or the agroforestry track at any size, or a planner result that came back partial. It is a $0 service line on the order and a Google Calendar appointment link in the confirmation email. Calendar owner: Josh, Wesley as backup. Advice only: it does not use the `grove_consult_built` invoice gate.
- **Planting Kit and survival guarantee are deferred** until the planner has shipped. Not in this spec.
- **`grove_consult_built` is turned on for templates 134 and 135 now**, independent of this spec. Both already promise the consult and carry the compliance SOP; the invoice gate the SOP depends on is off.

## Non-goals

- No change to the publish gate. The planner fields are not required to publish; a plant missing them is simply never recommended, and the nightly audit says so.
- No new states. The planner uses `GREEN_STATES` as it is; the nine follow-up states (GOL-2243) are out of scope.
- No change to the deposit rule, the waves, or the shipping rate quote. The planner reads them, it does not alter them. (The checkout quote gains one boolean, section D.2.)
- No Odoo Appointments module, no Calendly, no new credential. Booking is a Google Workspace appointment-schedule URL in one system parameter.
- No per-tree kit or guarantee product.
- No automated writing of facts into `grove_*` fields from prose by an LLM at request time. Parsing happens once, in a reviewed backfill.

---

## A. Planner facts on `product.template`

**Where:** `grove_headless/models/product_template.py`, `grove_headless/views/product_template_views.xml` (new "Planner" group under Growing facts), `grove_headless/controllers/main.py` (`_serialize_facts`), `grove_headless/__manifest__.py` (version bump), `grove_headless/tests/test_planner_facts.py`.

The planner cannot score on prose such as "First nuts at 4 to 6 years; good crops around 20 years". These fields hold the scoreable subset. The existing char fields stay as the display values; nothing moves.

| Field | Type | Values | Used for |
|---|---|---|---|
| `grove_years_to_fruit_min` | Integer | 1 to 30; 0 = unset | Timing scoring; "first crop" row |
| `grove_pollination_min` | Integer | 1 = self-fertile, 2+ = needs partners; 0 = unset | Quantity floor per line |
| `grove_harvest_months` | Char | comma list of month numbers, e.g. `9,10,11` | Deer season slots; harvest calendar on the result card |
| `grove_tolerates_wet` | Boolean | | Site filter: "low, wet spot" |
| `grove_tolerates_poor_soil` | Boolean | | Site filter: "rough ground" |
| `grove_tolerates_shade` | Boolean | default computed from `grove_sun` in (`partial`, `shade`), editable | Site filter: "woods edge" |
| `grove_wildlife_value` | Selection | `high`, `medium`, `low`; blank = unset | Deer track eligibility and scoring |
| `grove_planner_priority` | Integer | default 50, lower wins | Merchandising order inside a slot |
| `grove_planner_ready` | Boolean, computed, stored | True when `grove_listing_complete` and the first four planner fields are set | The catalog feed only serves ready plants |

Rules:

- All eight editable fields get `tracking=True` like the other facts.
- `grove_planner_ready` is recomputed on the same dependencies as `grove_listing_complete` plus the planner fields. It is a separate flag from the publish gate on purpose: a plant can be published and not recommendable.
- The nightly `cron_audit_listing_content` gains a second section: published plants that are not planner-ready, with the missing field names. Same Discord channel, same format.
- Bundles (templates with a BoM, or `grove_consult_built`) are never planner-ready as a species. The planner recommends their components, then offers the bundle when the component set matches (section C.5).

### One-time parser (`scripts/backfill_planner_facts.py`)

XML-RPC, defaults to `DRY_RUN=1`, QA first, same shape as `backfill_listing_content.py`.

- `grove_years_to_fruit_min` = first integer in `grove_years_to_fruit`.
- `grove_pollination_min` = 1 if the text matches self-fertile, self-pollinating or parthenocarpic; 2 if it matches partner, second, two, 2+, cross; 3 if it matches three or 3+; else unset.
- `grove_harvest_months` from `grove_harvest_season` by a small table: Summer = 7,8; Summer–Fall = 8,9,10; Fall = 9,10,11; Late fall = 10,11,12; Spring = 4,5,6; else unset. Month numbers already present in the text win.
- `grove_tolerates_wet` true if `grove_soil` or `grove_watering == "high"` mentions wet, moist, bottomland, flood; `grove_tolerates_poor_soil` true on rocky, clay, thin, poor, sandy, drought; `grove_tolerates_shade` from `grove_sun`.
- `grove_wildlife_value` = high if `grove_wildlife` mentions deer or turkey, medium if it mentions birds or squirrels, else unset.
- Writes go through `write()` so the chatter shows them. The script prints a table of before and after values for Josh to confirm before `DRY_RUN=0`.
- Every parsed value sets `grove_facts_reviewed = False` on that template (the existing "machine writes clear the sign-off" rule), so the review pass in section E catches them.

### Serializer

`_serialize_facts` gains the eight editable fields plus `planner_ready`, with the same blank conventions (integers to `None` when 0, chars to `""`, booleans as booleans, selection to `None`). `normalizeFacts` and `GrowingFacts` in `grove-sites/packages/odoo-client` gain the camelCase twins. The PDP spec block does not change in this train.

---

## B. Catalog feed: `GET /grove/api/v1/planner/catalog`

**Where:** `grove_headless/controllers/main.py` (new route next to `/products`), `grove_headless/models/plant_compliance.py` (expose `genus_for(botanical)` and `blocked_states_for(botanical)` as public helpers; today they are private to the order gate), `grove_headless/tests/test_planner_catalog.py`.

Public, no auth, same CORS and company scoping as `/products`. One JSON document; `Cache-Control: public, max-age=300`. The storefront fetches it once per quiz run through a BFF route (`apps/nursery/app/api/planner/catalog/route.ts`), never from the browser directly.

```json
{
  "generated_at": "2026-10-14T18:00:00Z",
  "states": ["AL", "AR", "..."],
  "state_zones": {"WV": [5, 7], "GA": [7, 9], "...": [0, 0]},
  "plants": [
    {
      "id": 4,
      "slug": "american-persimmon",
      "name": "American Persimmon",
      "botanical_name": "Diospyros virginiana",
      "genus": "diospyros",
      "price": 12.0,
      "tier": "bareroot",
      "pickup_only": false,
      "in_stock": true,
      "preorder_cap_reached": false,
      "layer": "understory",
      "zone_min": 6, "zone_max": 9,
      "years_to_fruit_min": 3,
      "pollination_min": 1,
      "harvest_months": [9, 10, 11],
      "tolerates": {"wet": true, "poor_soil": true, "shade": false},
      "wildlife_value": "high",
      "priority": 50,
      "blocked_states": ["CA"],
      "substitute_id": null,
      "image": "https://..."
    }
  ],
  "bundles": [
    {"id": 132, "slug": "mountain-mama", "name": "...", "price": 50.0,
     "component_ids": [93, 93, 91, 91, 133], "consult_built": false},
    {"id": 135, "slug": "food-forest-50", "name": "...", "price": 200.0,
     "component_ids": [], "consult_built": true, "tree_count": 50}
  ]
}
```

Rules:

- `plants` = `sale_ok`, `website_published`, company 3, `grove_planner_ready`, not a bundle. The list is short (today 6 ready, about 25 once section E is done), so no paging.
- `in_stock` and `preorder_cap_reached` use the same helpers as `/products` (`_list_in_stock`, `grove_preorder_cap_reached`). A plant that is out of stock is still in the feed so the planner can show it as "notify me".
- `blocked_states` = `CARVE_OUTS` lookup on the parsed genus, empty when `grove_compliance_exempt`. Empty or unparseable botanical name: `blocked_states` = the regulated-state set, mirroring the fail-safe in `plant_compliance.py`.
- `substitute_id` = the template the bundle-substitution engine would swap in for that genus (Castanea to Shagbark Hickory, Prunus to Jujube), or null. The planner uses it to say "in Florida we send shagbark hickory instead", the same copy the PDP notice already uses.
- `bundles` = published templates in public category 6 (Food Forest Packages). `component_ids` comes from the BoM when one exists (GOL-2589 seeds them), else empty. `tree_count` is the existing bundle tree count used by the promo engine.
- `states` is `GREEN_STATES` sorted, so the storefront never carries its own copy.
- `state_zones` is a new hand-maintained constant `STATE_ZONE_BANDS` in `shipping_zones.py`, one `[cold, warm]` USDA zone pair per green state, used only when a ZIP is unknown. A test asserts every green state has an entry.
- The endpoint is read-only and idempotent; it is safe for the nightly audit to call and diff.

Tests: feed excludes non-ready and unpublished plants; blocked states match `CARVE_OUTS` for each genus including the Morus alba versus rubra split; `compliance_exempt` empties the list; unparseable botanical name yields the regulated set; cache header present; output is JSON-serialisable with no `False` where a `None` is expected.

---

## C. Storefront: the quiz, the planner, the result

**Where (grove-sites):** new package `packages/planner` (pure TypeScript, no React, no Odoo import, 100% unit-tested); `apps/nursery/app/plan/page.tsx` (full page) and a home-page section that opens with the same component; `apps/nursery/app/api/planner/catalog/route.ts` (BFF); `packages/odoo-client` types. The cart write uses the existing `POST /api/cart` (`product_id`, `quantity`), one call per line. Nothing in this section touches `packages/checkout`; only the consult checkbox (section D) does, and that path is protected and merges by admin.

### C.1 The four questions

1. **Where are you planting?** ZIP input. `GET /api/zone?zip=` gives the zone. On `unknown_zip` the question falls back to a state select over `states` from the feed, and the planner runs on the state's `[cold, warm]` band from `state_zones`: a plant is `safe` when it covers the whole band and `marginal` when it overlaps it. A state not in `states` ends the quiz with the "we cannot ship there yet" message and the pickup option.
2. **What is the spot like?** Four cards, one choice: Open ground (full sun, drains well); Low, wet spot; Rough ground (rocky, clay, thin); Woods edge (shade part of the day).
3. **What are you trying to do?** Three track chips:
   - **Grow food for my family** (food forest). Size chips: Corner, 5 trees; Homestead, 20 trees; Acre, 50 or 100 trees.
   - **Put my land to work** (agroforestry). Sub-goal chips: Alley crop rows; Silvopasture; Riparian buffer; Windbreak or hedgerow. Size chips: 50, 100, "more than 100, talk to us".
   - **Bring in deer and wildlife** (plot). Size chips: One stand, 8 trees; Daily plot, 16; Orchard, 30+.
4. **When do you want a harvest?**
   - Food forest and agroforestry: Fast (first crop within 5 years), Patient (long-lived canopy nuts), Mixed (default).
   - Deer: Early season (Sep to Oct), Late season (Dec to Feb), All season (default), All year.

Every answer is in the URL (`/plan?zip=26501&site=open&track=food&size=20&timing=mixed`) so a result is shareable and the consult call can open the same plan.

### C.2 The planner (`packages/planner/src/plan.ts`)

```
plan(input, catalog) -> Result
input:   { zone, state, site, track, subgoal?, size, timing }
Result:  { status: "ok" | "partial" | "empty",
           lines: [{ plant, qty, slot, zoneFit, note? }],
           substitutions: [{ from, to, state }],
           notify: [plant],            // out of stock, would have been picked
           bundle?: { id, saves },      // when a bundle covers the lines
           subtotal, treeCount,
           consult: boolean,
           harvestMonths: number[] }
```

**Filter.** A plant is a candidate when all hold:
- zone fit: `zone_min <= zone <= zone_max` is `safe`; one zone outside on either end is `marginal`; further out is excluded.
- site: `wet` requires `tolerates.wet`; `poor` requires `tolerates.poor_soil`; `shade` requires `tolerates.shade`; `open` requires nothing.
- state: `state` not in `blocked_states`. A blocked plant with a `substitute_id` is replaced by its substitute at filter time and a substitution is recorded; a blocked plant without one is dropped.
- deer track only: `wildlife_value` is `high` or `medium`.
- not `pickup_only` and `tier` in the shippable set (`SHIPPABLE_TIERS` = bareroot and potted; potted ships on its own box engine since GOL-2199 and is the default tier, so excluding it would drop most of the catalog). Seeds are not plants and are never in the feed.

In-stock candidates go to the pool; out-of-stock or cap-reached candidates go to `notify`.

**Slots.** A slot is a layer (or a season for deer) with a share of the total tree count.

| Track / sub-goal | Slots and shares |
|---|---|
| Food forest | canopy 20%, understory 30%, shrub 40%, support 10% (support = any layer with `harvest_months` empty: the pollinator and nurse plants such as redbud and buttonbush) |
| Agroforestry: alley crop | canopy 50%, shrub 50% |
| Agroforestry: silvopasture | canopy 70%, understory 30% |
| Agroforestry: riparian buffer | plants with `tolerates.wet` only: understory 40%, shrub 60% |
| Agroforestry: windbreak | canopy 40%, understory 30%, shrub 30% |
| Deer: all season | early 50%, late 50% |
| Deer: early | early 75%, late 25% |
| Deer: late | early 25%, late 75% |
| Deer: all year | early 40%, late 40%, offseason 20% |

Deer seasons from `harvest_months`: early = any of 8, 9, 10; late = any of 11, 12, 1, 2; offseason = any of 4 to 7 and none of the others. A plant in both early and late is "both" and may fill either slot, scored 10 lower in the early slot so a true early tree wins there (Whitetail's rule, kept).

A slot with fewer than 2 trees' share still gets 1 line if a candidate exists; Corner (5) therefore produces 3 to 4 lines, not 5.

**Score** per candidate, higher wins within a slot:

```
score = 100
      - 2 * priority
      - 1.5 * years_to_fruit_min
      - 40 if zoneFit == "marginal"
      + timing: fast    -> a further -3 * years_to_fruit_min
                patient -> +2 * years_to_fruit_min
                mixed   -> 0
      + deer: +4 per harvest month inside the chosen season
      + 10 if wildlife_value == "high" (deer track only)
      - 20 if pollination_min > slotQty + 2   // would force a big line
```

**Quantity.** `qty = round(total * share)`, then `qty = max(qty, pollination_min)` with `note = "needs N to pollinate"`. The feed carries no on-hand count, so the planner does not cap: a plant is either in the pool (`in_stock` and not `preorder_cap_reached`) or in `notify`. The existing cart and checkout checks reject an over-ask at Build my order, and the result card shows that line's error inline.

**Status.** `ok` when every slot with a non-zero share is filled; `partial` when at least one line exists; `empty` otherwise. `consult = subtotal >= 500 || track == "agro" || status == "partial"` drives the "Talk it through first" link on the result card only; whether the $0 consult line can be added is decided by the checkout quote (section D.2), never by the planner.

**Bundle match.** After planning, if a bundle in the feed has `component_ids` that is a sub-multiset of the planned lines, the result offers it ("Mountain Mama covers 5 of these for $50, saves $X") and, on accept, replaces those lines with the bundle line. Consult-built bundles (134, 135) are offered instead of individual lines whenever `track == "agro"` or `size >= 50`: the planned mix is sent along as the starting point (C.4).

**Determinism.** Same input and catalog give the same result; ties break on `id`. The package has no Date, no random, no network.

### C.3 The result card

- Eyebrow: "ZIP 26501, zone 6b, open ground" with an Edit link that reopens the questions with the URL state.
- Headline by status: "Your 20-tree food forest, first harvest in about N years" (N = min `years_to_fruit_min` across lines) / deer: "Something dropping from Sep to Jan".
- A layer strip (canopy over understory over shrub) with each line placed in its layer, so the customer sees why there are eight shrubs and two canopy trees.
- Lines: image, name, qty stepper, price, slot tag, note. Substitutions appear inline: "Florida: shagbark hickory ships in place of chestnut".
- Ship timing from `GET /grove/api/v1/shipping/options?zip=` (`ship_timing` string, already built for the PDP), called once per tier present in the plan. Bareroot lines show the dormant-window copy and, when the mode is `bareroot-preorder`, the existing $10 per order deposit sentence; potted lines show the `peat-and-bagged` ships-now copy. A plan with both tiers says so in one line ("your potted trees ship now, your bareroot trees in your spring window"). The planner does not change the cart's existing format rules (one wave per order, no mixing where the format gate forbids it): Build my order adds the lines and the cart applies its own checks; if the cart rejects a mix, the card groups the lines by tier and offers to build the potted order first and the bareroot order second.
- `notify` chips: "Fig, back in stock soon, notify me" wired to the newsletter subscribe route with a `product_id` tag.
- Primary button: **Build my order** (adds every line through `POST /api/cart`, then opens the mini cart). Secondary when `consult`: "Talk it through first" linking to the consult section.
- `empty` state: "Nothing we grow fits a wet, shaded spot in zone 3 yet" plus the drier-site retry button and the consult link.

Home page: the same component collapsed to the four questions under "Need help choosing trees? Answer four questions. We'll build your order."

### C.4 Carrying the plan onto the order

For consult-built packages the planned mix must reach Wesley. `POST /grove/api/v1/cart` accepts an optional `plan` object on a line whose template has `grove_consult_built`; it is stored on the sale order as `grove_planner_plan` (Json) and rendered read-only on the order form under the existing "Compliance check / substitutions" block, with the quiz URL. The SOP step 3 ("swap or drop, and tell the customer what changed") starts from that list. The storefront sends `{ url, lines: [{id, qty}], zone, state, site, track, subgoal, timing }`.

### C.5 Tests (grove-sites)

`packages/planner`: fixture catalog of 12 plants covering every layer and tolerance; golden tests for each track and timing; zone edge cases (safe, marginal, excluded); each carve-out genus in a blocked state with and without a substitute; pollination floor and cap notes; bundle match; determinism (two runs equal). Component tests: URL round trip, unknown ZIP fallback, empty state. One Playwright e2e: WV ZIP, open ground, food forest 20, mixed, Build my order, cart has the lines.

---

## D. The $500 consult

**Where:** `grove_headless/data/consult_product.xml` (new), `grove_headless/models/sale_order.py`, `grove_headless/data/mail_templates.xml` (order confirmation), `grove_headless/data/grove_config_params.xml`, `grove-sites/packages/checkout` (CartPage and CheckoutPage), `grove_headless/tests/test_consult_line.py`.

### D.1 The product

One template, created by data XML with a stable external id `grove_headless.product_planting_consult`:

| Field | Value |
|---|---|
| name | Planting consult, 15 minutes |
| type | service |
| list_price | 0 |
| sale_ok | True; `website_published` False; `grove_gate_exempt` True |
| company_id | 3 |
| taxes_id | none |
| description_sale | "A 15-minute call with the nursery to review your plan before it ships. You will get a booking link after checkout." |

It does not count as a tree for the deposit rule, bundle tree count or shipping boxes: every place that counts trees already filters on storable products, which a service is not. Test that explicitly.

### D.2 Eligibility and the checkout line

- `GET /grove/api/v1/checkout/quote` adds `consult_eligible: boolean` = goods subtotal after promotions and before shipping and tax at or above the system parameter `grove_headless.consult_threshold` (default `500.00`), **or** the cart contains a `grove_consult_built` template (which is how every agroforestry plan arrives, section C.2). The storefront never computes the threshold itself.
- The cart page shows a checkbox under the totals when eligible: "Add a free 15-minute planting consult". Ticking it posts the consult product with quantity 1 through the normal cart route; unticking removes it. If the cart drops below the threshold the quote returns `consult_eligible: false` and the storefront removes the line and says why.
- Server side, `_create_draft_order` rejects the consult line when the order is not eligible (same 400 pattern as the blank-phone check), so an eligible cart is the only way to get one.

### D.3 After the order

- `sale.order.grove_consult_requested` (Boolean, computed, stored): any line with the consult template. Shown on the order list and in the morning digest ("consult requested" tag next to the order).
- The order confirmation email gains a block when `grove_consult_requested`: "Book your planting consult" linking to `grove_headless.consult_booking_url` (system parameter; a Google Calendar appointment schedule owned by Josh, Wesley as backup; no credential, no API). The link carries `?order=S00412` in the query so the calendar description shows the order.
- The booking is not tracked in Odoo in v1. The calendar is the record.
- The 50 and 100-tree packages keep their 30-minute consult-built flow and invoice gate; this line is additive and advice-only.

Tests: eligibility at 499.99 versus 500.00 after a promo; consult line rejected on an ineligible order; `grove_consult_requested` computed; email block present only when requested; the service line does not change the deposit amount, the tree count or the box plan.

---

## E. Populating the facts (ops, before the train)

Prod state on 2026-10-14, 29 sale templates:

| Group | Templates |
|---|---|
| Complete and signed off | 4, 86, 87, 130, 133 |
| Facts filled, sign-off missing | 29; 9 (chill hours missing) |
| Partially seeded | 6, 10, 12, 15, 19 |
| Never enriched | 5, 8, 11, 13, 14, 16, 17, 18 |
| Perenual plan-gated after USDA | 3, 87, 130 |
| Bundles | 22, 132, 134, 135, 140, 136 to 138 |

1. Run `scripts/backfill_listing_content.py` on prod for the never-enriched and partial groups (13 templates). Dry run first, Josh's go, then live. Expect USDA to fill the natives and Perenual the fig, jujube, kaki and peach.
2. Apple, black walnut, white oak: fill the remaining gaps by hand or through the drafter with a source URL in `grove_facts_provenance`.
3. After section A ships to QA: run `scripts/backfill_planner_facts.py` on QA, review the printed table, then prod.
4. One review sitting in Odoo: list view of products with `grove_listing_missing != ""` or `grove_planner_ready = False`, Josh or Wesley sets `grove_facts_reviewed` and `grove_guide_ready`.
5. `seed_bundle_boms.py` for 22, 132, 140 so the feed can expand them (GOL-2589). 133, 134, 135 stay custom mix.

Done means the catalog feed serves at least 20 planner-ready plants on prod before Train #4 promotes, counting both tiers (potted is the default tier and the larger share of the catalog; the planner filter in C.2 admits it).

---

## F. Rollout and board split

| Piece | Repo | Owner | Train |
|---|---|---|---|
| A. Planner fields, computed ready flag, audit section, parser script, serializer | grove-odoo-modules | Ada | #4 |
| B. Catalog feed, compliance helpers, state zone table | grove-odoo-modules | Ada | #4 |
| D (Odoo side). Consult product, quote flag, order flag, email block, params | grove-odoo-modules | Ada | #4 |
| C. `packages/planner`, quiz, result card, BFF route, types | grove-sites | Iris | #4 |
| D (storefront). Cart checkbox in `packages/checkout` | grove-sites (protected path, admin merge) | Iris, Josh merges | #4 |
| E. Facts backfill and review | prod ops | Josh, Wesley | before #4 freeze |
| `grove_consult_built` on 134, 135 | prod data | Josh | now |

Order of landing: A and B first (the storefront package is developed against a fixture, so C can start the same day and switch to the live feed on QA). D last. One board issue per row; create them on the board directly, not as GitHub issues, so github-sync does not twin them.

Kill switch: the home-page section and `/plan` route are behind a `NEXT_PUBLIC_PLANNER` flag baked at image build; the Odoo endpoint stays up regardless. The consult checkbox is governed by the Odoo threshold parameter; setting it to 0 disables the offer without a deploy.

## Open items (not blocking)

- `available` quantity in the feed (reserved versus on hand) so the cap note is exact. v1 uses the preorder cap and the cart's own checks.
- Agroforestry sub-goals beyond the four listed, and windbreak species we do not yet grow.
- "Produces every year" as a boolean for masting oaks, for the PDP spec row and the deer track.
- Planting Kit and survival guarantee (deferred by decision).
- Tracking consult bookings back into Odoo.
