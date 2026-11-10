# Planner winter beta: layers and roles, the suggested list, and the learning loop (design)

**Status:** approved in brainstorm with Josh, 2026-10-21. Target: Release Train #5 (freeze Fri 2026-11-13, QA Mon 11-16, promote Wed 11-18), with one Train #4 manifest change (section F). Builds on the shipped order builder (`2026-10-14-order-builder-design.md`: A gom#357, B gom#362 and gom#367, C grove-sites#1138, D gom#358 and GOL-3541). Every ruling in that spec stands; this one adds to it. Draft notes: `gather-at-the-grove/docs/2026-10-21-retail-planner-design.md`; the internal consulting system this is a projection of: `gather-at-the-grove/docs/2026-10-21-consulting-dashboard-design.md` (waits on Josh's rulings). Prototype used in the brainstorm: the Grove Planner Lab artifact.

## What changes and why

The order builder ships `/plan` behind `PLANNER_ENABLED`. Josh's ruling on 2026-10-21 is to release it as a standalone part of the nursery site for the whole winter, before it is wired into the shop's merchandising, so that experts and buyers train it: edit plans, add plants by hand, dispute facts, and suggest what we should carry. Three things make that possible.

1. **The syntropic framing.** Every plant carries a role (anchor or companion) and a stratum word (overstory, understory, mushroom) on top of its existing layer, plus a succession word (fast, mid, long-lived). The result card says why each plant is there. The philosophy is stated on the page: anchors are the long-lived trees the system is built around; companions feed the soil, the pollinators and the person while anchors grow, and their prunings become mulch and mushroom logs. Nothing is planted alone.
2. **The suggested list.** Below the result, everything that fits the ZIP and site and was not placed, grouped by stratum, with an Add button. A hand add is a learning signal.
3. **The learning loop.** Cart signals, evidence against facts, and community suggestions land in Odoo with a name on them. A nightly job turns patterns into proposals. A named human accepts or dismisses every proposal; nothing changes a fact or a weight on its own.

## Decisions (Josh, 2026-10-21), do not re-open

- **Standalone winter beta.** `/plan` goes public on prod at the Train #4 promote (`PLANNER_ENABLED` set on prod, section F). Build my order stays on: bareroot lines are spring pre-orders with the $10 deposit as today, potted lines ship now. The consult checkbox keeps its $500 threshold.
- **Three strata, two roles.** Overstory, understory, mushroom. Anchor, companion. These are projections of the consultant vocabularies (eight syntropic strata, four succession stages, from AgriforestryOS `farm_syntropic`), never a separate fact set. The mapping is fixed in section A.
- **Succession words** fast, mid, long-lived map to Placenta, Accumulation 1 and 2, Abundance. Retail uses them for ordering and copy only.
- **Mushroom spawn is a planner plant.** A new `grove_layer` value `fungal`. Spawn products carry a substrate line and are placed in a mushroom slot on the food forest and agroforestry tracks, never on the deer track.
- **Suggested list and hand adds.** Everything eligible and unplaced, grouped by stratum. Adds append to the plan marked as the person's own pick.
- **Suggest a plant.** Name, stratum, why. Queued for Josh; accepting creates a draft listing in the content pipeline.
- **Tester mode** is a flag, not a login: a token in the URL sets a cookie. It shows the score breakdown on every pick, a dispute menu per fact, what-if weight sliders that do not persist, and a "propose this weight" button that does.
- **Guilds come from saved plans.** A staff user approves a saved plan into a bundle template in the Guilds website category, which is what the Guilds page lists.
- **Machines propose, a named human accepts.** The content gate's rule that machine writes clear `grove_facts_reviewed` applies to every write this spec adds.

## Non-goals

- No change to the four questions, the slots and shares, the score formula, or the cart write in the shipped planner. Section C changes what the result card shows and what sits under it.
- No server-side planner and no LLM, as before. Weights move from constants to a feed object, but the engine stays a pure function.
- No accounts or logins for the public. Signals are anonymous beyond the plan URL and a browser id.
- No change to the publish gate, the deposit rule, shipping, or compliance.
- No post-season check-in email in this train. The evidence model accepts survival reports so the email can land in spring without a schema change.
- Nothing from the consulting dashboard draft: no farmOS, no PostGIS, no consortium grid.

---

## A. Roles, strata and succession on `product.template`

**Where:** `grove_headless/models/product_template.py`, `grove_headless/views/product_template_views.xml` (Planner group), `grove_headless/controllers/main.py` (`_serialize_planner_plant`), `grove_headless/__manifest__.py` (version bump), `scripts/backfill_planner_roles.py`, `grove_headless/tests/test_planner_roles.py`.

| Field | Type | Values | Used for |
|---|---|---|---|
| `grove_planner_role` | Selection | `anchor`, `companion`; blank = unset | Slot assignment and the role badge |
| `grove_succession` | Selection | `fast`, `mid`, `long`; blank = unset | Ordering within a stratum, the succession badge |
| `grove_living_mulch` | Boolean | | Marks a ground cover as living mulch; fills the ground slot |
| `grove_substrate` | Char | e.g. `oak or maple logs from your thinnings` | Shown on spawn picks; required for `fungal` to be planner-ready |
| `grove_planner_stratum` | Selection, computed, stored | `overstory`, `understory`, `mushroom` | Derived from `grove_layer`, section A.1 |
| `grove_planner_flagged` | Boolean | | Set by the nightly audit when a fact has three open contradictions (section B.4); shown on the result card as "Needs review" |

Rules:

- `grove_layer` gains `("fungal", "Fungal")`. Existing values are untouched.
- `grove_planner_role` and `grove_succession` join the `grove_planner_ready` dependencies: a plant without a role or a succession word is not recommendable. `grove_substrate` is required for ready only when `grove_layer == "fungal"`.
- All four editable fields get `tracking=True`.
- The nightly audit's planner section lists the missing field names as it does today.

### A.1 Stratum mapping (fixed)

| `grove_layer` | `grove_planner_stratum` | Consultant strata it stands for |
|---|---|---|
| `canopy` | `overstory` | Emergent, High Canopy |
| `understory`, `shrub`, `vine` | `understory` | Low Canopy, Shrub |
| `ground` | `understory` (ground slot when `grove_living_mulch`) | Herbaceous, Ground Cover, Root/Tuber |
| `fungal` | `mushroom` | Fungal (proposed addition to `farm_syntropic`) |

### A.2 Backfill (`scripts/backfill_planner_roles.py`)

XML-RPC, `DRY_RUN=1` default, QA first, prints a before and after table.

- Role: `anchor` when `grove_layer == "canopy"`, or when `grove_layer == "understory"` and `grove_years_to_fruit_min >= 3`; else `companion`. Spawn: wine cap `anchor`, others `companion`.
- Succession: `fast` when `grove_years_to_fruit_min <= 2`; `long` when `>= 4` and `grove_layer == "canopy"`; else `mid`.
- Living mulch: true for `ground` layer plants whose `grove_wildlife` or `grove_soil` text mentions mulch, cover, fix, clover, comfrey.
- Every parsed value clears `grove_facts_reviewed` on that template. Josh confirms the table before `DRY_RUN=0`.

### A.3 Feed additions

`_serialize_planner_plant` adds `role`, `succession`, `stratum`, `living_mulch` (boolean), `substrate` (string or null), `flagged` (boolean), and `nitrogen_fixer` (boolean, true when `grove_wildlife` or `grove_soil` mentions nitrogen; a proper field can follow). The feed also gains a top-level `rules` object (section B.1) and `catalog_version` (an integer bumped by any write to a planner field or a rule, so signals can say which catalog they were made against). `grove-sites/packages/odoo-client` types gain the same keys.

---

## B. The learning loop in Odoo

**Where:** `grove_headless/models/planner_rule.py`, `planner_signal.py`, `planner_evidence.py`, `planner_proposal.py`, `planner_suggestion.py`, `planner_plan.py`; `grove_headless/security/ir.model.access.csv`; `grove_headless/views/planner_views.xml` (one "Planner" menu under Nursery with six list views); `grove_headless/controllers/planner.py` (new file, public routes); `grove_headless/data/planner_rules.xml`, `planner_cron.xml`; tests per model.

### B.1 `grove.planner.rule`

| Field | Notes |
|---|---|
| `key` | Char, unique with scope: `priority_weight`, `years_weight`, `marginal_penalty`, `season_bonus`, `wildlife_bonus`, `nitrogen_bonus`, `timing_fast`, `timing_patient` |
| `scope` | Selection `retail`, `consult`; this spec seeds `retail` only |
| `value` | Float, `tracking=True` so the chatter is the changelog |
| `label`, `hint` | Char, shown to testers |

Seeded from the shipped constants (2, 1.5, 40, 4, 10, 10, 3, 2) so the first feed changes nothing. The feed serves `rules: {key: value}` for scope `retail`; the engine reads them instead of its constants, with the constants kept as the fallback when a key is missing. One test asserts the fallback equals the seed.

### B.2 `grove.planner.signal`

Written by `POST /grove/api/v1/planner/signal` (public, same CORS as the cart, 60 requests per browser id per hour, body validated, anything else is a 400):

| Field | Notes |
|---|---|
| `kind` | Selection `removed`, `swapped`, `added`, `built` (Build my order pressed), `saved` (plan named and saved) |
| `product_tmpl_id` | Many2one, required except for `built` and `saved` |
| `zone`, `zone_band` | Integer, Char `3-5`, `6-7`, `8-9` |
| `track`, `site`, `timing` | Char, from the plan URL |
| `plan_url` | Char, the shareable quiz URL |
| `browser_id` | Char, a random id the page keeps in localStorage |
| `catalog_version` | Integer |

No personal data. The list view groups by product and zone band with a count.

### B.3 `grove.planner.evidence`

| Field | Notes |
|---|---|
| `product_tmpl_id` | Many2one, required |
| `fact` | Selection of the planner facts: `zone_min`, `zone_max`, `years_to_fruit`, `tolerates_wet`, `tolerates_poor_soil`, `tolerates_shade`, `pollination`, `harvest_months`, `role`, `succession`, `stratum` |
| `contradicts` | Boolean |
| `source` | Selection `tester`, `survival`, `staff`, `log` |
| `note` | Text, the tester's reason |
| `by` | Char, tester name from the token record; staff user otherwise |
| `zone` | Integer, optional |
| `reviewed` | Boolean, set by staff with `reviewed_by`, `reviewed_at` |

Written by `POST /grove/api/v1/planner/dispute` (requires a valid tester token, section B.6) and, later, by the spring check-in email.

### B.4 Nightly job `cron_planner_learning`

Runs after `cron_audit_listing_content`, same Discord channel.

1. **Tally signals** for the last 30 days by product and zone band. Three or more `removed` of one product in one band with no open proposal creates a proposal to raise `grove_planner_priority` by 10 for that product (a global change; per-band priority is an open item). Three or more `added` creates the opposite proposal.
2. **Count contradictions** per product and fact where `reviewed` is false. Three or more sets `grove_planner_flagged` and clears `grove_facts_reviewed`, with one chatter line naming the fact and the count. Fewer than three clears the flag only if no open proposal mentions it.
3. **Post** a summary: new proposals, flagged products, queued suggestions, with links to the Odoo views.

### B.5 `grove.planner.proposal`

| Field | Notes |
|---|---|
| `kind` | Selection `priority`, `rule`, `fact` |
| `product_tmpl_id` | Optional |
| `rule_id` | Optional |
| `change` | Json: the exact write the proposal would make, e.g. `{"grove_planner_priority": 60}` or `{"value": 1.0}` |
| `reason` | Text, generated: "removed 4 times in zone band 6-7 since 2026-10-22" |
| `status` | Selection `pending`, `accepted`, `dismissed` |
| `decided_by`, `decided_at` | |

Accepting applies `change` through `write()` so the chatter on the target shows it, stamps `grove_facts_provenance` with `proposal:<id>`, and clears `grove_facts_reviewed` on a product change. Testers can submit a `rule` proposal from the what-if sliders through `POST /grove/api/v1/planner/propose` (tester token required).

### B.6 Tester tokens and `grove.planner.suggestion`

`grove.planner.tester`: `name`, `email`, `token` (random, shown once), `active`. `GET /plan?tester=<token>` sets an HttpOnly cookie for 90 days when the token is active; the BFF forwards it on dispute and propose calls and Odoo resolves the name. There is no tester UI in Odoo beyond the list view; Josh creates the ten records by hand.

`grove.planner.suggestion`: `name`, `stratum`, `note`, `by` (tester name, or `public`), `status` (`queued`, `accepted`, `declined`), `product_tmpl_id` (set on accept). Written by `POST /grove/api/v1/planner/suggest` (public, same rate limit as signals). An Odoo button "Accept as catalog candidate" creates a product template with `sale_ok` false, `website_published` false, the name and the layer from the stratum, and posts the note to its chatter, so the enrichment pipeline and the drafter pick it up like any other new listing.

### B.7 `grove.planner.plan` and guilds

Saving a plan from the result card (`POST /grove/api/v1/planner/save`, public, body: name, plan URL, lines as `{template id, qty, role, stratum}`, zone, track) creates a `grove.planner.plan` with `status = "saved"`. Staff see them in a list view with the lines rendered. The button **Approve as guild** (server action, group `grove_headless.group_nursery_staff`) creates a bundle template in the Guilds website category with the lines as a BoM, name from the plan, price as the sum of the lines, unpublished, and links it on the plan with `status = "guild"`. Publishing it is the existing publish gate's job. A plan that reaches checkout keeps the existing `grove_planner_plan` JSON on the order; the two are not linked in v1.

---

## C. Storefront changes

**Where (grove-sites):** `packages/planner` (types, `plan.ts` reads `rules`, new `suggested.ts`), `apps/nursery/app/plan/result-card.tsx`, new `suggested-list.tsx`, `suggest-form.tsx`, `tester-bench.tsx`, `apps/nursery/app/api/planner/{signal,suggest,dispute,propose,save}/route.ts` (BFF passthroughs), `copy.ts`.

### C.1 Result card

- A philosophy line above the lines, from `copy.ts`, as ruled above.
- Each line gains three badges: stratum and role ("Overstory anchor"), succession ("long-lived"), and when true "fixes N" and "Needs review" (from `flagged`). Spawn lines show "Grows on {substrate}".
- Lines are ordered by stratum (overstory, understory, mushroom) and then by role (anchor first) so the card reads in planting order; the slot shares do not change.
- Remove and Swap on each line (Swap takes the next best candidate for that slot from the engine, which gains `nextFor(slot, exclude)`), both posting a signal.
- Save this plan: a name field and a button, posting to the save route. Build my order posts a `built` signal before the cart writes.

### C.2 Suggested list (`suggested.ts`)

`suggested(input, catalog, result)` returns, per stratum, the eligible plants (same filter as the engine) not in the result, sorted by score. The component renders three groups with name, role badge, zone fit, succession, harvest months, price and Add. Add appends a line with `qty = max(1, pollination_min)`, marks it `manual`, posts an `added` signal, and scrolls to it. Manual lines survive Edit as long as they still pass the filter. A counter says how many plants were hidden for this site.

### C.3 Suggest a plant

Name, stratum select, note, Send. Posts to the suggest route; the confirmation says it goes to the nursery for review. Rate-limited and honeypot-protected at the BFF like the newsletter form.

### C.4 Tester bench

Rendered only when the tester cookie is present. Under the result card: the score breakdown per line (from the engine's `explain(line)`), a Dispute menu per line listing the plant's facts with their current values, what-if sliders for every rule key that re-run the engine locally without persisting, and Propose this weight, which posts the rule key and value. The bench never calls Odoo directly.

### C.5 Tests

`packages/planner`: rules object overrides constants and the fallback equals the seed; `suggested` excludes placed plants and respects the filter; `nextFor` skips excluded ids; ordering by stratum then role; determinism holds with rules supplied. Components: signals fire once per action; manual lines persist across Edit; tester bench hidden without the cookie. One Playwright: WV ZIP, open ground, food forest 20, mixed, remove a line, add from the suggested list, save the plan, assert the signal and save calls.

---

## D. Winter beta operations

- **Testers.** Five to ten: extension agents, agroforestry practitioners we know, respondents to the Threads chips-versus-doors post, a handful of past buyers. Josh creates the tester records and sends each a `/plan?tester=` link.
- **Cadence.** Every other Monday with the release train: review proposals and suggestions in Odoo, accept or dismiss, review flagged products, republish nothing (rules are live from the feed within five minutes of a write because of the cache header).
- **Measures** (from signals and plans): plans built, `built` signals per plan, removes and adds per plan, disputes per product, suggestions queued, and the share of testers whose third saved plan carried no removals.
- **Exit criteria for March 2027.** Every planner-ready product has `grove_facts_reviewed` set by a named reviewer after the beta started; rules unchanged for two consecutive reviews; the first guilds approved from saved plans. Then the home-page section switches on and the planner joins the shop's merchandising.

---

## E. Populating roles (ops, before Train #5 freeze)

1. After section A lands on QA, run `backfill_planner_roles.py` on QA, review the table, then prod.
2. Create the spawn products (wine cap, shiitake plug, oyster) with `grove_layer = fungal`, substrate lines, and the planner facts, or mark the existing myco products with the new layer where they already exist.
3. Josh sets `grove_facts_reviewed` in one sitting on every product the backfill touched.

Done means every planner-ready plant on prod has a role and a succession word, and at least two spawn products are planner-ready.

---

## F. Rollout and board split

| Piece | Repo | Owner | Train |
|---|---|---|---|
| Train #4 manifest: `PLANNER_ENABLED` set on prod at promote so `/plan` is the public winter beta; QA gate 12 wording changes from "404 on prod" to "renders on prod" | odoocker / manifest GOL-3303 | Terra | #4 |
| A. Roles, strata, succession, substrate, fungal layer, flagged, feed additions, backfill script | grove-odoo-modules | Ada | #5 |
| B. Rule, signal, evidence, proposal, tester, suggestion and plan models; public routes; nightly job; Approve as guild | grove-odoo-modules | Ada | #5 |
| C. Result card badges and philosophy, remove and swap, suggested list, suggest form, save plan, signal posting, BFF routes | grove-sites | Iris | #5 |
| D. Tester bench | grove-sites | Iris | #5 |
| E. Roles backfill, spawn products, review sitting | prod ops | Josh, Wesley | before #5 freeze |

Order of landing: A first (C is developed against the fixture catalog extended with the new keys, then switched to QA). B in parallel with A. C after A, D after B and C. One board issue per row, created on the board directly.

Kill switches: `PLANNER_ENABLED` as today for the whole page. The tester bench needs a valid token, so deactivating the tester records turns it off. The public routes check a system parameter `grove_headless.planner_learning_enabled` (default True) and return 503 when it is off, which the storefront treats as a silent no-op.

## Open items (not blocking)

- Per-zone-band priority (today a proposal changes the product's global priority).
- A proper `grove_nitrogen_fixer` boolean instead of the text match.
- The spring post-season check-in email, which writes `survival` evidence.
- Linking a saved plan to the sale order it became.
- Whether community suggestions show publicly on the page as "requested".
- The consulting dashboard (`2026-10-21-consulting-dashboard-design.md`) and the `consult` rule scope.
