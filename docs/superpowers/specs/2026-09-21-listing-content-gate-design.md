# Plant listing content: required facts, auto-fill, drafting, audit (design)

**Status:** approved in brainstorm with Josh, 2026-09-21. Extends the growing-facts model shipped in `grove_headless` 19.0.1.14.0 and the July publish-pipeline design (`grove-sites/docs/superpowers/specs/2026-07-24-publish-pipeline-design.md`). Odoo stays the single source of truth for all product content.

## Decision (Josh, 2026-09-21)

Every plant listing on the nursery storefront must carry a complete set of growing facts, a product description and an approved care guide before it can be published. Odoo enforces that with a hard publish gate. Facts are auto-filled from USDA PLANTS and Perenual on a button press; prose is drafted by a Paperclip agent; a human ticks two sign-off boxes. A nightly audit reports anything published that is still incomplete. Phase 3 adds per-tree environmental impact from the i-Tree engine.

Ruled during brainstorm, do not re-open:

- **Hard publish gate**, not a score. Setting `website_published` on an incomplete plant fails with the list of missing items.
- **Required set is 12 facts + description + approved guide + facts reviewed.** The 8 existing facts plus mature spread, chill hours, pollination needs and years to fruit. Growth rate, bloom season, harvest season, watering and wildlife are optional.
- **Sources: USDA PLANTS first, Perenual second, agent prose.** Trefle is skipped (USDA-derived data, 12% growth coverage, no hardiness zones). i-Tree is phase 3.
- **Perenual is a runtime dependency on the free tier**, hard-capped at 100 calls per UTC day, overflow queued to the next day. Josh classifies the use as educational.
- **Approach A, Odoo-native.** Fetch button and gate live in `grove_headless`; the drafter is a Paperclip routine talking XML-RPC; scripts only for the one-time backfill.

## Non-goals

- No change to the `/shop` facet vocabulary (zone, layer, sun) or their URL contract.
- No Odoo-side HTML sanitizer; grove-sites keeps sanitizing on render.
- No automatic unpublishing. The migration adds fields and touches no data.
- No Perenual care-guide endpoint dependency (documented as a paid tier in one place and free in another; treated as a bonus if it answers).
- No i-Tree call on page view. Projections are cached per species and zone.

## Catalog audit, prod, 2026-09-21 (read-only)

19 real plant templates plus the Remembrance bundle; 50 `[in-store historical]` POS imports are unpublished and excluded.

| Field | Filled |
|---|---|
| Botanical name | 19 / 19 |
| Zone min/max, layer, sun, mature size | 6 / 19 |
| Plant spacing, soil | 0 / 19 |
| Any description | 3 / 19 (one-liners in `description_sale`) |
| Guide approved | 0 / 19 |

12 templates are published; 4 have a complete spec block; none has a description or guide. Shagbark Hickory is archived but still `is_published`.

USDA PLANTS (live probe) has full characteristics for 12 species: pawpaw, American plum, American persimmon, serviceberry, Allegheny chinquapin, Chinese chestnut, red and white mulberry, flowering dogwood, aronia, European pear, shagbark hickory. It has nothing for fig, jujube, hardy kiwi, apple, peach, kaki persimmon or beautybush; those come from Perenual.

---

## A. Data model and publish gate

**Where:** `grove_headless/models/product_template.py`, `grove_headless/views/product_template_views.xml`, `grove_headless/security/`, `grove_headless/__manifest__.py` (version bump), `grove_headless/tests/test_listing_gate.py`.

### New fields on `product.template`

Required facts (join the existing `grove_growing_facts` group):

| Field | Type | Notes |
|---|---|---|
| `grove_mature_spread` | Char | display-only, e.g. "6–8 ft" |
| `grove_chill_hours` | Char | e.g. "450–550"; enter "Not applicable" for non-fruiting plants |
| `grove_pollination` | Char | e.g. "Self-fertile" / "Needs a second variety" |
| `grove_years_to_fruit` | Char | e.g. "2–4 years"; "Not applicable" for ornamentals |

Optional facts (auto-filled):

| Field | Type | Values |
|---|---|---|
| `grove_growth_rate` | Selection | `slow` / `moderate` / `fast` |
| `grove_bloom_season` | Char | e.g. "Late spring" |
| `grove_harvest_season` | Char | e.g. "Summer–winter" |
| `grove_watering` | Selection | `low` / `moderate` / `high` |
| `grove_wildlife` | Char | e.g. "Attracts bees, birds" |

Provenance and workflow:

| Field | Type | Notes |
|---|---|---|
| `grove_facts_provenance` | Json | `{field: {"source": "usda"\|"perenual"\|"agent"\|"human", "ref": url-or-id, "at": iso}}` |
| `grove_usda_symbol` | Char | resolved PLANTS symbol, e.g. `DIVI5`; editable |
| `grove_perenual_id` | Integer | resolved Perenual species id; editable |
| `grove_facts_reviewed` | Boolean | "Facts reviewed for storefront". Cleared by any API or agent write to a fact. |
| `grove_gate_exempt` | Boolean | "Exempt from listing-content gate" for bundles, gift cards, supplies |
| `grove_draft_state` | Selection | `none` / `requested` / `drafted` |
| `grove_listing_complete` | Boolean, computed, stored | see completeness rule |
| `grove_listing_missing` | Char, computed | human-readable list, shown as a banner |

All content fields (`description_ecommerce`, `website_description`, every `grove_*` fact) get `tracking=True` so chatter shows who or what changed them.

### Description wiring

The storefront description becomes Odoo's **eCommerce Description** (`description_ecommerce`, HTML). Today the PDP renders `description_sale`, a plain-text field Odoo also prints on quotations and invoices; marketing prose does not belong there. `description_sale` reverts to its Odoo role and is not gated. The care guide stays in `website_description`, gated by `grove_guide_ready` as today.

### Completeness rule

A template is complete when all of:

1. The 12 required facts are set. Integers count as set when > 0; chars when non-blank after strip.
2. `description_ecommerce` is non-empty after stripping tags and whitespace.
3. `website_description` is non-empty after stripping, and `grove_guide_ready` is true.
4. `grove_facts_reviewed` is true.

`grove_listing_missing` names each unmet item with the field label, e.g. "Plant Spacing, Chill Hours, Care guide approval, Facts reviewed".

### Gate

`write()` and `create()` override: when `vals` sets `website_published` to true on a template that is **gated** and not complete, raise `UserError("Cannot publish <name>: missing <grove_listing_missing>")`. Gated means `type == 'consu'`, `grove_gate_exempt` false, and `categ_id` is under the Plants root category from `data/grove_product_categories.xml`. The check runs only on the publish transition, so an already-published incomplete product can be edited field by field and keeps selling; the audit (section D) covers it until complete. Unpublish → publish re-runs the check.

The existing `_check_zone_range` constraint stands.

### Form

On the Grove Headless page: a banner at the top rendering `grove_listing_missing` (green "Listing complete" when empty), buttons **Fetch facts** and **Request content draft** (section B, C), the two sign-off checkboxes side by side, and the new fields in the facts group in this order: botanical name, zones, layer, sun, mature size, mature spread, spacing, soil, growth rate, watering, bloom season, harvest season, wildlife, pollination, years to fruit, chill hours. List view gains a filter **Incomplete listings** (`website_published = True and grove_listing_complete = False and grove_gate_exempt = False`).

---

## B. Enrichment: USDA PLANTS + Perenual with a daily budget

**Where:** `grove_headless/services/plant_data/{__init__,usda,perenual,mapping}.py`, `grove_headless/models/grove_enrich_job.py`, `grove_headless/data/ir_cron.xml`, `grove_headless/tests/test_plant_data.py` with fixtures under `grove_headless/tests/fixtures/plant_data/`.

### Provider interface

```
@dataclass
class FactValue: value: Any; source: str; ref: str
@dataclass
class PlantFacts: fields: dict[str, FactValue]; hints: list[str]; candidates: list[str]
class Provider: def lookup(self, botanical_name, cached_id=None) -> PlantFacts
```

`hints` are chatter-only notes (never written to fields); `candidates` are returned when no exact match is found.

### Name resolution

The botanical binomial is the first two tokens of `grove_botanical_name` lower-cased, with cultivar quotes and authors stripped. If the name contains `spp.`, `hybrid` or `×`, no auto-match is attempted and the chatter says so. USDA: `PlantSearch?searchText=<genus species>` → exact match on `ScientificNameWithoutAuthor`; store the symbol. Perenual: `species-list?q=<genus species>` → exact match on `scientific_name[0]`; store the id. A stored symbol/id skips the search call. No exact match → the chatter lists up to five candidates with symbols/ids; the human sets the field and re-fetches.

### USDA PLANTS (free, no key)

Base `https://plantsservices.sc.egov.usda.gov/api/`. Calls per product: `PlantProfile?symbol=`, `PlantCharacteristics/{Id}`, `PlantWildlife/{Id}`. Synchronous from the Fetch button; 10 s timeout; one retry; failure → chatter note, never an exception to the user.

### Perenual (free tier, budgeted)

Base `https://perenual.com/api/v2/`, key from env `PERENUAL_API_KEY` via the existing container env passthrough (never in the repo, never in the database). Calls per product: `species-list?q=` (skipped when `grove_perenual_id` is set) and `species/details/{id}`.

**Budget:** `ir.config_parameter` `grove_headless.perenual_daily_budget` (default 100) and a counter `grove_headless.perenual_calls.<YYYY-MM-DD UTC>` incremented per HTTP call. The Fetch button never calls Perenual directly; it creates a `grove.enrich.job` (`product_tmpl_id`, `provider='perenual'`, `state` queued/running/done/failed, `attempts`, `note`). Cron `grove_headless.process_enrich_jobs` runs every 10 minutes, drains jobs oldest-first while `counter + calls_needed <= budget`, and stops otherwise; remaining jobs wait for the next UTC day. The chatter note on enqueue states the queue position and the reset time. A job failing twice is marked failed with the HTTP status in `note`. HTTP 429 counts as budget exhausted for the day.

### Mapping rules (conservative; every write is a draft)

Only **empty** fields are written. Each write records provenance and clears `grove_facts_reviewed`. One chatter message per fetch lists field → value → source.

| Target | USDA PLANTS | Perenual | Rule |
|---|---|---|---|
| zone min/max | never (hint only: "USDA minimum temperature −21 °F ≈ zone 4b") | `hardiness.min/max` | Perenual only; survival temperatures run colder than nursery practice |
| sun | Shade Tolerance Low → `full`, Medium/High → `partial` | `sunlight` list: only "full sun" → `full`; contains "part shade" → `partial`; only shade → `shade` | Perenual wins when both present; USDA never yields `shade` |
| layer | Growth Habit Tree + Height ≥ 40 ft → `canopy`; Tree < 40 → `understory`; Shrub → `shrub`; Vine → `vine`; Forb/Herb/Graminoid → `ground` | — | USDA only |
| mature size | "up to N ft" from Height, Mature (feet) | `dimensions` min–max + unit | USDA wins |
| mature spread | — | — | never auto-filled (no source) |
| spacing | never (hint: range derived from Planting Density per Acre, forestry spacing) | — | never auto-filled |
| soil | texture adaptations joined + "pH a–b" | `soil` list joined | Perenual wins |
| growth rate | Slow/Moderate/Rapid → slow/moderate/fast | Low/Moderate/High → same | USDA wins |
| bloom season | Bloom Period | `flowering_season` | USDA wins |
| harvest season | Fruit/Seed Period Begin–End | `harvest_season` | Perenual wins |
| watering | Moisture Use Low/Medium/High → low/moderate/high | `watering` Minimum/Average/Frequent → same | Perenual wins |
| wildlife | PlantWildlife Food/Cover animal groups rated ≥ Medium | `attracts` list → "Attracts bees, birds" | Perenual wins |
| chill hours, pollination, years to fruit | — | — | never auto-filled |

---

## C. Content drafting by a Paperclip agent

**Where:** Paperclip routine `grove-content-drafter` (AgenticOS repo, routine definition + prompt); `grove_headless/security/` (drafter user + group); no new Odoo endpoint.

1. **Request content draft** button sets `grove_draft_state = requested` and posts a chatter note. It requires a botanical name and at least one fetch attempt recorded in provenance, so the agent never drafts from nothing.
2. The routine polls Odoo over XML-RPC every 15 minutes as a dedicated user **Content Drafter** (group `grove_headless.group_content_drafter`: read product.template, write on the content fields, post messages; company At The Grove Nursery). Query: `[('grove_draft_state','=','requested')]`.
3. For each product it reads name, botanical name, category, tags, all facts with provenance, and the shipping tier, and writes: `description_ecommerce` (2–3 short paragraphs, storefront voice), `website_description` (care guide: site and soil, planting, first-year care, pruning, harvest), any still-empty required facts with a cited extension-service source (provenance `source: agent`, `ref: url`), and a chatter message listing every source used. House rules from the vault apply (e.g. the juglone stance: minor factor, never "poison").
4. Allowed HTML is the grove-sites sanitizer allow-list (`apps/nursery/lib/sanitize.ts`): `p, h2, h3, ul, ol, li, strong, em, a[href]`.
5. It sets `grove_draft_state = drafted` and leaves `grove_guide_ready` and `grove_facts_reviewed` false. A human reads, edits, ticks both boxes, publishes.

A failed run leaves the state at `requested` and posts the error to chatter; the next poll retries. The routine handles one product per run to keep drafts reviewable.

---

## D. Nightly audit and one-time backfill

**Where:** `grove_headless/models/product_template.py` (`cron_audit_listing_content`), `grove_headless/data/ir_cron.xml`, `scripts/backfill_listing_content.py`.

**Audit cron**, 06:00 America/New_York daily: select gated, published, incomplete templates. Post one Discord message via the same webhook mechanism `grove.order.rollup` uses ("3 published listings incomplete: Apple — Plant Spacing, Soil, Description, Care guide; …"), and schedule a `mail.activity` (To Do, due today) on each product for its responsible user, skipping products that already carry an open one. Nothing is unpublished.

**Backfill**, `scripts/backfill_listing_content.py`, `DRY_RUN=1` by default, XML-RPC against the target host: for each real plant template (excludes `[in-store historical]`, services, bundles) call `action_fetch_facts` then `action_request_draft` through `call_model_method`, so the backfill runs exactly the production code path. Runs on QA first; prod runs only with Josh's go. Perenual jobs drain within the daily budget (19 products ≈ 38 calls, one day).

---

## E. Storefront (grove-sites)

**Where:** `packages/odoo-client/src/{types,normalizers}.ts`, `apps/nursery/app/shop/[id]/{page,spec-block}.tsx`, `grove_headless/controllers/main.py` (`_serialize_facts`, `PRODUCT_DETAIL_FIELDS`).

- `_serialize_facts` adds `growth_rate, bloom_season, harvest_season, watering, wildlife, mature_spread, chill_hours, pollination, years_to_fruit` (chars → `""`, selections → `None`). Detail payload adds `description_html` = `description_ecommerce`; `description_sale` stays in the payload for now and is removed once the app has switched.
- Normalizer maps the new keys into `GrowingFacts`; `Product.description` becomes the sanitized HTML of `description_html`, falling back to `description_sale` only while the field is empty (bridging the backfill window).
- Spec block rows, Arbor Day order: USDA zones, growth rate, mature height, mature spread, sun, soil, watering, bloom, harvest, wildlife, pollination, years to fruit, chill hours, layer. Blank rows are still omitted; a complete product now shows all of them.
- Facets, zone check, guide rendering: unchanged.

---

## F. Phase 3: environmental impact via i-Tree

**Where:** `grove_headless/models/grove_tree_benefit.py`, `grove_headless/services/itree.py`, cron, `_serialize_benefits` on the detail endpoint, `apps/nursery/app/shop/[id]/impact-block.tsx`.

Prerequisites (Josh): signed API agreement with the Davey Institute, API key in env `ITREE_API_KEY` via passthrough. Nothing runs until present. Pricing is $0.02 per successful tree request, billed quarterly; attribution "Powered by i-Tree" with logo and link to itreetools.org is mandatory on every display.

- `product.template.grove_itree_species_code` Char, set by hand from Davey's species list (i-Tree Eco codes, e.g. `DIVI` style; verify per species).
- Model `grove.tree.benefit`: `species_code`, `zone` (3–9), `project_years` (20), `co2_sequestered_kg`, `co2_storage_kg_y20`, `runoff_avoided_m3`, `interception_m3`, `pollutants_removed_kg`, `value_usd`, `engine_version`, `fetched_at`. Unique on (species_code, zone, project_years).
- Cron, yearly (and on demand): for each distinct species code × zone with a published product, call `GET /v2/getGrowoutTreeBenefit/` with `Longitude/Latitude` of a representative ZIP per zone (constant table drawn from `data/zip_usda_zone.csv`), `Species`, `DBHCentimeter` from the tree-length class (16" → 0.6, 20" → 0.8, 32" → 1.0, 46" → 1.3; tune after first run), `TreeHeightMeter=-1`, `Condition=Excellent`, `CLE=5`, `MortalityRate=0.0`, `ProjectYears=20`, no building parameters. Parse the XML `YearOutput` list; sum annual hydro, pollutant and CO2 figures across the 20 years; take year-20 carbon storage. Skip and log on `Error`.
- Detail endpoint adds `benefits` for the requested zone (`?zone=` already exists on the zone endpoint; the PDP passes the visitor's zone from the ZIP check, defaulting to the storefront's home zone). Storefront impact block: "Planted in zone 6, over 20 years this tree captures about X lb of CO2, intercepts Y gallons of stormwater and removes Z lb of air pollution", plus the i-Tree badge and link.
- Cost: 19 species × 7 zones × 1 refresh/year ≈ $2.70/year.

---

## Testing

- **Providers:** recorded JSON fixtures (USDA `DIVI5` profile/characteristics/wildlife; Perenual search + details for *Ficus carica*), mapping tests per rule in the table, name-resolution tests (exact match, `spp.` skip, no match → candidates). No live network in CI.
- **Budget/queue:** counter increments per call, drains up to the budget, stops at the cap, rolls over at UTC midnight, 429 exhausts the day, second failure marks failed.
- **Gate:** publish incomplete → error naming each missing item; exempt passes; non-plant category passes; already-published incomplete product accepts a single-field edit; sign-off boxes reset on machine writes and survive human writes; unpublish→publish re-checks.
- **Audit cron:** selects the right set, one activity per product, no duplicate activities, Discord payload shape.
- **Serializer:** new keys present, blanks normalized.
- **grove-sites:** normalizer unit tests for new keys and the description fallback; PDP render test for a fully populated spec block.
- **i-Tree (phase 3):** XML fixture parse, aggregation, unique constraint, cron skips species without a code.

## Rollout

1. `grove_headless` manifest 19.0.1.41.0 → 19.0.1.42.0; new nullable columns only, no data migration.
2. Env passthrough for `PERENUAL_API_KEY` (and later `ITREE_API_KEY`) on QA and prod, same mechanism as the carrier credentials (GOL-2274).
3. Release train: QA gate → backfill on QA → review → pin bump (Josh applies) → backfill on prod with Josh's go.
4. Vault: update `Software/Odoo ERP.md` and `Gather at the Grove.md` with the gate and the source policy once merged.

## Open items

- Perenual free-tier terms say non-commercial; Josh has ruled the use educational. Recorded here so it is not re-litigated.
- Discord webhook mechanism inside Odoo: confirm `grove.order.rollup` exposes a reusable helper; otherwise add `grove_headless.discord_webhook_url` config parameter fed from env.
- i-Tree species codes for our 19 species and the whip DBH table need a first live run to validate.
- Shagbark Hickory: archived but published; unpublish during backfill review.
