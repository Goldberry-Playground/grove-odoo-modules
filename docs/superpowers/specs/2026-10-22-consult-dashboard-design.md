# Grove Consult: the internal syntropic design dashboard (design, phases 1 and 2)

**Status:** approved in brainstorm with Josh, 2026-10-21 and 2026-10-22 (rulings and the flow confirmed on a working prototype). Phase 1 targets Release Train #5 (freeze Fri 2026-11-13, promote Wed 11-18); phase 2 targets Train #6 (freeze Fri 11-27, promote Wed 12-02). Phases 3 to 5 are described in the draft and get their own spec once phase 2 is on QA. Draft and rulings: `gather-at-the-grove/docs/2026-10-21-consulting-dashboard-design.md`. Builds on the order builder (`2026-10-14-order-builder-design.md`) and the planner winter beta (`2026-10-21-planner-winter-beta-design.md`, gom#370, GOL-3547 to 3551) and on AgriforestryOS 4.x (`farm_syntropic`, mcp-server, sync-service, postgis-etl). Prototypes used in the brainstorm: the Grove Consult Flow and Grove Consult Wireframes artifacts.

## What this is

An internal dashboard our farms use when we consult for other landowners. It is not customer-facing: clients receive documents, and we use the stack to make them. Four jobs, in order:

1. **Qualify and track clients in Odoo.** Every lead, consult, visit, quote, plan version and invoice is on the CRM opportunity.
2. **Create comprehensive plans from our options.** Plans are built only from what we grow, what we can source, and the consortia we have designed.
3. **Collect and improve a research database.** Every planting we design, install or steward becomes a record we can study over years, including trials such as truffle inoculation on hazelnuts (phase 5).
4. **Semi-automated plan construction.** We enter the client's goals, areas and site; the tool drafts the whole plan; we edit every section before it goes out.

The stack is the one AgriforestryOS already runs for the home farm: farmOS is the record of what is in the ground (assets, geometry, logs); Odoo is the customer, the money, the nursery stock and the lots; PostGIS is a mirror reproduced from farmOS by the ETL and QGIS draws it; Claude over MCP is how we ask questions. The dashboard holds no facts of its own.

## Decisions (Josh, 2026-10-21 and 10-22), do not re-open

- **One AgriforestryOS instance for all clients.** A `property` land type carries the client; PostGIS schema per client; QGIS project per client from the home-farm template. No per-client instances.
- **Fungal is added** to the `syntropic_stratum` vocabulary, so spawn and logs are first-class.
- **Tenure value is `client`.** It answers "whose tree is this": `permanent` is our orchard, `nursery_stock` is stock in the alleys for sale, `client` is on someone else's land. The value drives map style, default exclusion from `count_trees` and inventory views, no harvest-to-Odoo receipt, the archive rule, and the sync-service mapping.
- **A real Odoo stock location per client** during the install, for lot traceability. Client records are plans, not our inventory: at handover the location is transferred out and archived, the farmOS records are archived, and the export bundle on Spaces is the backup. Nothing is deleted.
- **Service tiers, not one-off fees.** The free $500-threshold consult is the entry; each step above it is a service product on the same opportunity. Reference prices are Forest Shepherd Farm's published list; ours are Josh's to set on the product records.
- **No client portal.** Clients see nothing inside our systems. The deliverable is the export bundle: the plan PDF, the plantings, the GIS. Clients do not log; stewardship-tier logs are ours.
- **Goal modules and practices are two axes.** Modules say what the client wants (twelve, modeled on Cultiva EcoSolutions' service lines). Practices say what goes on a piece of ground, one per area: homestead food forest, alley cropping, silvopasture, forest farming, mycoforestry, riparian buffer, windbreak and hedgerow. The consortia library is keyed by practice and site class.
- **Machines propose, a named human accepts.** Every generated plan section is `generated` until a named person edits or approves it, and a plan cannot be approved before that. The content gate's rule that machine writes clear `grove_facts_reviewed` applies to every write here.
- **The retail planner is a projection of this system.** Eight strata collapse to three, four stages to three words, practices to tracks and sub-goals. Never a separate fact set.

## Non-goals (this spec)

- Phases 3 to 5: site model from DEM, SSURGO and imagery; row drawing and the consortium grid; plan generation; quote, install, handover and the bundle; stewardship calendar; trials. They are designed in the draft and specified after phase 2.
- No change to the retail planner, the publish gate, the deposit rule, shipping or compliance.
- No new external credentials. Drive-time estimates use straight-line distance with a road factor; parcel and soil pulls are phase 3 and use keyless public services.
- No LLM in the generator. Sections are templates over records.

---

## A. farmOS vocabulary and tenure (AgriforestryOS, phase 1)

**Where:** `modules/farm_syntropic/` (config install, `.install` update hooks, `Tree.php`, the `tree_planting_trees` view, a new map style), `mcp-server/` (`count_trees`, `query_trees`, `harvest_summary` filters), `sync-service/mapping.py` (`determine_tenure`), `postgis-etl/schema.sql` (tenure column already mirrored; add the land types), `docs/`.

1. **Fungal** term in `syntropic_stratum`, after Root/Tuber. Update hook `farm_syntropic_update_8003` creates the term on existing sites; fresh installs get it from config.
2. **Tenure `client`** on the Tree asset's `tenure` list field, beside `permanent` and `nursery_stock`. Update hook extends the allowed values without touching existing rows.
   - `count_trees`, `query_trees`, `list_harvests` and `harvest_summary` take an optional `tenure` filter and default to excluding `client` unless `tenure` is passed, so home-farm numbers stay home-farm numbers. Documented in `mcp-server/README.md`.
   - `harvest_sync.py` skips logs whose asset has `tenure = client`: a client harvest never becomes an Odoo production receipt.
   - `sync-service`: moves into a stock location whose name starts with the configured `CLIENT_LOCATION_PREFIX` (default `Client:`) create Trees with `tenure = client`; moves out archive them as today. `NURSERY_STOCK_CATEGORIES` and the Orchard mapping are unchanged.
   - Map style: the GeoJSON export and the QGIS base-plan template style `client` trees with their own symbol and leave them out of the home-farm layer group by default.
3. **Land types** `property` (the client's parcel polygon; fields: client name, Odoo opportunity id, Odoo stock location id, status active or archived) and `water` (pond, stream, wet spot; Point or Polygon). Plain `farm_land` bundles via config, no new PHP beyond field definitions. Areas (section C) reference a `property`.
4. Tests: kernel test asserts the three tenure values, the Fungal term and the two land bundles; smoke workflow asserts the update hooks ran; mcp-server tests cover the tenure filter default.

Land and branch: AgriforestryOS `4.x`, one PR, Josh reviews. The module is baked into the pinned production image, so the preprod deploy and the migration step in `docs/hosting/preprod-deploy.md` apply.

---

## B. Consultant facts and the consult feed (grove-odoo-modules, phase 1)

**Where:** `grove_headless/models/product_template.py`, `views/product_template_views.xml` (a "Consultant" group under the Planner group), `controllers/main.py` (`planner_catalog` with `scope=consult`), `__manifest__.py`, `grove_headless/tests/test_consult_facts.py`.

Blocked by planner beta A (GOL-3548), which adds `grove_planner_role`, `grove_succession` (fast, mid, long), `grove_living_mulch`, `grove_substrate` and the computed three-value `grove_planner_stratum`. This section adds the consultant depth and makes the retail values projections of it.

| Field | Type | Values | Notes |
|---|---|---|---|
| `grove_syntropic_stratum` | Selection | `emergent`, `high_canopy`, `low_canopy`, `shrub`, `herbaceous`, `ground_cover`, `root_tuber`, `fungal` | Matches the farm_syntropic vocabulary one to one. Required for consult-ready |
| `grove_succession_stage` | Selection | `placenta`, `accumulation_1`, `accumulation_2`, `abundance` | Required for consult-ready |
| `grove_nitrogen_fixer` | Boolean | | Replaces the text match in the retail feed; the retail feed's `nitrogen_fixer` reads this field once set |
| `grove_mature_height_m` | Float | | |
| `grove_canopy_closure_years` | Integer | | |
| `grove_coppice_tolerant` | Boolean | | |
| `grove_pruning_response` | Selection | `strong`, `moderate`, `none` | Drives the chop-and-drop and log-yield rules |
| `grove_root_type` | Selection | `tap`, `fibrous`, `runner` | |
| `grove_allelopathy` | Selection | `none`, `minor`, `notable` | Juglone stays `minor`, never worded as poison |
| `grove_log_suitability` | Selection | `oak_maple`, `poplar_soft`, `none` | For the mushroom layer |
| `grove_consult_ready` | Boolean, computed, stored | | `grove_planner_ready` and the two required fields above |

Projections: once `grove_syntropic_stratum` and `grove_succession_stage` are set on a template, `grove_planner_stratum` (emergent and high canopy to `overstory`; low canopy, shrub, herbaceous, ground cover, root and tuber to `understory`; fungal to `mushroom`) and `grove_succession` (placenta to `fast`, both accumulation stages to `mid`, abundance to `long`) are computed from them and become read-only on the form. Templates without them keep the planner beta's editable behavior, so nothing in flight breaks. A one-time `scripts/backfill_consult_facts.py` proposes the eight-value stratum from `grove_layer` plus the retail role, prints the table, and writes only on `DRY_RUN=0`, clearing `grove_facts_reviewed` as every machine write does.

Feed: `GET /grove/api/v1/planner/catalog?scope=consult` adds, per plant, `syntropic_stratum`, `succession_stage`, `nitrogen_fixer`, `mature_height_m`, `canopy_closure_years`, `coppice_tolerant`, `pruning_response`, `root_type`, `allelopathy`, `log_suitability`, `consult_ready`, and serves `rules` for scope `consult` from the planner rule table (planner beta B). Same cache header, same company scoping, public and read-only like the retail feed; the consultant facts are catalog data, not secrets. The nightly audit gains a consult-ready section.

---

## C. Consult models and seed data (grove-odoo-modules, phase 2)

**Where:** new files `grove_headless/models/consult_module.py`, `consult_practice.py`, `consult_area.py`, `consult_plan.py`, `consult_plan_section.py`; `models/crm_lead.py` (new); `data/consult_modules.xml`, `consult_practices.xml`, `consult_products.xml`; `security/ir.model.access.csv`; `views/consult_views.xml` (a "Consulting" menu under Nursery); tests per model.

### C.1 `grove.consult.module` (twelve seeded records)

`key`, `name`, `objective`, `approach` (text, the steps), `expected_results` (text), `section_title`, `section_template` (text with `{placeholders}` the generator fills in phase 3), `consortium_rules` (Json, phase 3), `active`. Seeded: lifecycle, perennial integration, soil and carbon, water resilience, microclimate buffering, habitat and pest structuring, certification readiness, monoculture transition, nutrient cycling, climate adaptation, site remediation, wildlife and deer. Text comes from the draft, section 6.6.

### C.2 `grove.consult.practice` (seven seeded records)

`key`, `name`, `strata` (Json list of the eight stratum keys the practice plants), `row_spacing_m`, `in_row_spacing_m`, `site_needs` (text), `infrastructure_defaults` (Json list of infrastructure keys with unit and placeholder rate), `retail_track`, `retail_subgoal`, `notes`. Seeded with the table in the draft, section 6.7. Silvopasture lists no shrub stratum; forest farming and mycoforestry force shade-tolerant species.

### C.3 `grove.consult.area`

`lead_id` (crm.lead), `name`, `practice_id`, `acres`, `wet`, `poor_soil`, `shade`, `aspect` (`N`, `E`, `S`, `W`, `flat`), `farmos_land_uuid` (set in phase 3 when the polygon is drawn), `sequence`.

### C.4 `grove.consult.plan` and `grove.consult.plan.section`

Plan: `lead_id`, `version`, `status` (`draft`, `approved`, `sent`, `installed`), `plan_json` (the rows and consortia, same shape as `sale.order.grove_planner_plan` extended with areas), `approved_by`, `approved_at`, `sale_order_id`. Section: `plan_id`, `module_id` (optional), `key`, `title`, `body` (text), `figures` (Json), `status` (`generated`, `edited`, `approved`), `by` (res.users), `sequence`. Constraint: a plan's status can move to `approved` only when no section is `generated`. The generator that fills them is phase 3; phase 2 ships the models, the constraint and the views so the dashboard can create a draft plan with the skeleton sections empty.

### C.5 `crm.lead` extensions

`grove_consult_stage` (Selection: `intake`, `site`, `design`, `quote`, `install`, `stewardship`, `archived`), `grove_consult_step` (Many2one to the service product in use), `grove_hands_on` (`we_install`, `they_install`, `plan_only`), `grove_goals` (text, the client's words), `grove_existing` (text), `grove_module_ids` (Many2many), `grove_area_ids` (One2many), `grove_property_uuid`, `grove_stock_location_id`, `grove_travel_estimate` (Json: km, hours, billable hours, from C.7). The CRM pipeline view gets a consult stage column and a filter.

### C.6 Service menu products (data XML, stable external ids)

| External id | Name | Type | Reference list price | Notes |
|---|---|---|---|---|
| `product_planting_consult` | Planting consult, 15 minutes | existing | 0 | the $500-threshold entry |
| `consult_strategy_hour` | Strategy session, per hour | service | 100 | 2-hour minimum in person |
| `consult_basemaps` | Basemaps for a strategy session | service | 150 | credited against a plan |
| `consult_plan` | Whole-property plan | service | 800 | fixed on the quote after the visit |
| `consult_workbook` | Implementation workbook | service | 2000 | replaces the plan fee |
| `install_labor_day` | Installation labor, per day | service | 0 | bid per project |
| `steward_visit` | Stewardship visit | service | 0 | |
| `steward_year` | Stewardship year | service | 0 | |
| `consult_hour` | Consulting, per hour | service | 100 | extras |
| `graft_topwork` | Top-working established trees | service | 0 | case by case |
| `willow_structure` | Living willow structure | service | 0 | case by case |
| `travel_mile` | Travel, per mile round trip | service | 0.76 | IRS rate, update yearly |
| `travel_hour` | Travel, outbound beyond the first hour | service | 100 | |
| `travel_night` | Overnight allowance | service | 0 | confirmed in writing |

All company 3, `sale_ok` true, `website_published` false, `grove_gate_exempt` true, no tax by default. Prices are reference values from the public list we studied; Josh edits them on the records. None count as trees for the deposit rule, box plans or compliance (same test as the consult product).

### C.7 Staff routes `/grove/api/v1/consult/*`

Authenticated with the existing headless API key the storefront BFF holds; never called from a browser. `GET /pipeline` (leads with consult fields, areas, stage), `GET /lead/<id>`, `POST /lead` (create or update the consult fields, areas, modules, step), `POST /lead/<id>/stage`, `GET /modules`, `GET /practices`, `POST /lead/<id>/plan` (create a draft plan with the skeleton sections), `GET /travel?zip=` (straight-line distance from the farm ZIP centroid to the client ZIP centroid times 1.3, hours at 45 mph, billable hours after the first, the $600 three-hour rule applied; the ZIP centroid table is the one the zone lookup already carries plus a lat and lon column). Every route is company-scoped and tested.

---

## D. Dashboard shell (grove-sites, phase 2)

**Where:** new app `apps/consult` (Next.js, same toolchain as `apps/nursery`), `packages/consult-client` (typed calls to the C.7 routes through the app's own BFF), `apps/consult/app/{pipeline,client/[id]}`.

- **Pipeline.** Leads by consult stage with practices by area, acres, zone, service step, next action and days in stage. Filters by stage. "New client" creates a lead.
- **Client workspace, Intake tab.** Client and land (ZIP with the zone lookup, acres, service step from the product list, hands-on, goals in the client's words, what they already have, travel estimate), goal modules as chips, areas table with practice, acres, wet, poor soil, shade, aspect, and the practice's site needs and infrastructure defaults shown read-only. Save and go to Site.
- **Site tab, phase 2 scope.** The schematic map from the prototype (areas sized by acreage, wet areas marked), the site facts panel from the areas, and the walk checklist stored on the lead. The real map (PostGIS, DEM, soils) is phase 3.
- **Design tab, phase 2 scope.** Shows the plan versions and their skeleton sections with the edited and approved states and the approval lock. The generator and the consortium grid are phase 3.
- The stage tabs, badges and the planting-order and gap conventions follow the Grove Consult Flow prototype.
- Auth: the app is served on `consult.gatheringatthegrove.com` behind Cloudflare Access (the same mechanism Paperclip uses), staff emails only. The BFF holds the Odoo key; the browser never sees it.
- Tests: component tests for the intake form round trip and the areas table; one Playwright flow: new client, three areas, modules, save, pipeline shows the lead at Intake.

---

## E. Infrastructure (phase 2)

**Where:** grove-sites deploy for the new app (App Platform component, same image family as the nursery app), Cloudflare Access application and policy for the consult host, a DigitalOcean Spaces prefix `consult/<opportunity>/` for the phase 4 bundles (created now so the path is settled), and in AgriforestryOS `scripts/new_client_project.py`, which copies the home-farm QGIS base plan to a per-client `.qgz` with the layer groups kept and the home-farm layers removed, named by opportunity. No new credentials: the Access policy and the Spaces prefix use the tokens already in Terraform.

---

## F. Phases 3 to 5 (summary, specified later)

3. **Site model and design.** Parcel from county GIS or upload, 3DEP elevation clip, SSURGO drainage classes, NAIP canopy, contours and aspect in PostGIS per client; rows drawn on contour or keyline; the eight-by-four consortium grid with explainable gaps; planting points from rows and spacing; the generator that drafts rows, consortia and every plan section from the modules and practices; the consortia library keyed by practice and site class.
4. **Quote, install, handover.** Plan to sale.order with stock, spawn, infrastructure and service lines; per-client stock location; sync-service multi-location; Wesley's phone install view with lots and substitutions; the export bundle (plan PDF, site brief, GeoJSON and KML, planting points, QGIS project, calendar, pruning guides, lot list) to Spaces; the archive step.
5. **Stewardship, trials and learning.** Calendar from the plan, log templates, the `trial` plan type with treatment and control groups, `trial_summary` tool, evidence and proposals shared with the retail planner, consortia performance roll-ups, the Ask pane.

---

## G. Rollout and board split

| Piece | Repo | Owner | Train |
|---|---|---|---|
| A. Fungal stratum, `client` tenure and its filters, `property` and `water` land types, sync and harvest-sync rules, map style | AgriforestryOS 4.x | Ada, Josh reviews and deploys | #5 |
| B. Consultant facts, projections, backfill script, consult feed scope | grove-odoo-modules | Ada, blocked by GOL-3548 | #5 |
| C. Consult models, seed data, service products, crm.lead fields, staff routes | grove-odoo-modules | Ada | #6 |
| D. `apps/consult` shell: pipeline, intake, site and design tabs at phase 2 scope | grove-sites | Iris, blocked by C | #6 |
| E. Consult host, Cloudflare Access, Spaces prefix, QGIS client-project script | grove-sites deploy, AgriforestryOS, Terraform | Terra | #6 |
| Prices on the service products; first client entered | prod ops | Josh | at #6 promote |

Order of landing: A and B in parallel (B waits for planner beta A); C next; D against a fixture of the C routes until QA; E any time before #6 QA. One board issue per row, created directly on the board.

Kill switch: the consult app is a separate host; removing the Access policy or the App Platform component takes it down without touching the nursery. The Odoo routes check `grove_headless.consult_enabled` (default True) and return 503 when off.

## Open items (not blocking)

- Whether `grove.consult.plan` and the retail `grove.planner.plan` (planner beta B) merge into one model once both exist.
- Per-client PostGIS schema naming and the ETL change that writes to it (phase 3).
- The parcel source per state (phase 3).
- Exact prices and the overnight allowance rule.
