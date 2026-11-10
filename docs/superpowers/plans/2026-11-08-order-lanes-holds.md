# Order lanes, holds and spring zone batches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Wesley hold, move, label and ship orders from #grove-orders, and batch spring orders by zone with 5-day and day-of warnings.

**Architecture:** Odoo stays the system of record. New bearer endpoints in `grove_headless` do every state change and return a card payload, and the Discord bridge only opens modals, forwards input and renders what Odoo returns. Phase 1 (Tasks 1 to 7) ships the individual lane alone. Phase 2 (Tasks 8 to 11) adds spring zone batches.

**Tech Stack:** Odoo 19 (Python), TypeScript bridge on Node with vitest, Discord HTTP interactions.

**Spec:** `docs/superpowers/specs/2026-11-08-order-lanes-holds-design.md`

**Repos:** Tasks 1 to 5 and 7 to 10 are in `grove-odoo-modules`. Task 6 and the bridge halves of 7 and 10 are in `grove-sites/apps/discord-bridge`.

## Global Constraints

- Build on grove-odoo-modules#400 (stage gate on `action_grove_replace_label`). Merge it first.
- Spring warnings must be live before **2027-02-24**.
- Nudges and zone alerts post to the per-brand order channel (`DISCORD_ORDER_CHANNELS`, keyed on `company_id`), which is #grove-orders for the nursery. Existing #shipping alerts don't change.
- Every order action is limited to `DISCORD_ORDER_OPERATOR_IDS`, and every state change writes chatter naming the actor.
- Discord limits: a modal must be returned within 3 seconds, `custom_id` is at most 100 characters, modal titles and input labels at most 45.
- A hold never ships or charges anything by itself. No automatic customer email on a wave move.
- Do not gate `action_grove_mark_shipped` itself: the carrier poll and webhooks call it. Gate the operator paths only.
- Cron records use `noupdate="1"` (GOL-3562).
- Odoo tests: `scripts/dev/run_odoo_tests.sh grove_headless`. **Pass means the log's `odoo.tests.result` line shows 0 failed, 0 errors.** The script exits 1 on the expected `Printify push crashed` lines. Register each new test file in `tests/__init__.py` and in `conftest.py`.
- Bridge tests, from the `grove-sites` root: `pnpm exec vitest run apps/discord-bridge/lib/<file>`.
- **Rollout order for Task 3 and Task 6:** deploy Odoo first. The old button then gets a visible refusal for the few minutes until the bridge lands. Bridge first would mark orders shipped with the tracking ignored.

## Review Focus

1. **Double-click or Discord retry on Mark shipped:** one charge, one email (test in Task 3).
2. **Tracking pasted with spaces, or a typo:** spaces are stripped, and an invalid number is refused with nothing written (Task 3).
3. **Hold date typed oddly:** "Feb 30" is refused, and "Oct 15" typed on Oct 20 means next year (Task 1).
4. **Bridge down when a nudge is due:** the nudge isn't marked as sent, so the next run retries (Task 7).
5. **Spring order with no known zone:** it can't appear in any zone batch, so the daily late post names it instead of losing it (Task 10).

---

## Phase 1: Individual lane

### Task 1: Pure lane and date helpers

**Files:**
- Create: `grove_headless/models/order_lanes.py`
- Test: `grove_headless/tests/test_order_lanes.py`

**Interfaces:**
- Produces: `lane_for(fulfillment: str, is_printify: bool, ship_wave: str | bool) -> str` returning `"spring" | "individual" | "none"`; `parse_hold_date(text: str, today: date) -> date` raising `ValueError`; `suggest_hold_date(today: date, dormancy_start: date, zone_start: date | None) -> date`; `clean_tracking(text: str) -> str`; `parse_label_cost(text) -> float` raising `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lane():
    assert lane_for("ship", False, "spring") == "spring"
    assert lane_for("ship", False, "fall") == "individual"
    assert lane_for("ship", False, False) == "individual"
    assert lane_for("pickup", False, "spring") == "none"
    assert lane_for("ship", True, False) == "none"

def test_parse_hold_date():
    today = date(2026, 10, 20)
    assert parse_hold_date("Oct 25", today) == date(2026, 10, 25)
    assert parse_hold_date("10/25", today) == date(2026, 10, 25)
    assert parse_hold_date("Oct 15", today) == date(2027, 10, 15)
    assert parse_hold_date("2027-03-01", today) == date(2027, 3, 1)
    for bad in ("Feb 30", "soon", "", "2026-10-01"):
        with self.assertRaises(ValueError):
            parse_hold_date(bad, today)

def test_suggest_is_later_of_dormancy_and_zone():
    assert suggest_hold_date(date(2026, 9, 1), date(2026, 10, 15), date(2026, 11, 9)) == date(2026, 11, 9)
    assert suggest_hold_date(date(2026, 9, 1), date(2026, 10, 15), None) == date(2026, 10, 15)

def test_clean_tracking_and_cost():
    assert clean_tracking(" 1Z999 AA1 0123456784\n") == "1Z999AA10123456784"
    assert parse_label_cost("$12.40") == 12.40
    for bad in ("", "abc", "-1", "0"):
        with self.assertRaises(ValueError):
            parse_label_cost(bad)
```

- [ ] **Step 2: Run the tests and confirm they fail** on the missing module.
- [ ] **Step 3: Implement the five functions.** A dated string with a year in the past is an error. A month and day that have passed roll to next year.
- [ ] **Step 4: Run the tests and confirm they pass.**
- [ ] **Step 5: Commit:** `feat(grove_headless): pure helpers for order lanes and hold dates`

### Task 2: Holds on the order

**Files:**
- Create: `grove_headless/models/sale_order_ops.py` (`_inherit = "sale.order"`; add to `models/__init__.py`)
- Test: `grove_headless/tests/test_order_holds.py`

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces: fields `grove_lane` (stored compute: spring, individual, none), `grove_hold_until` (Date), `grove_hold_reason` (Char), `grove_hold_set_by` (Char), `grove_hold_last_nudge` (Date). Methods `action_grove_set_hold(until: date, reason: str, actor: str)`, `action_grove_release_hold(actor: str)`, `action_grove_snooze_hold(actor: str, days: int = 7)`, `_grove_suggested_hold() -> date`.

- [ ] **Step 1: Write the failing tests:** `test_set_hold_records_and_posts_chatter` (fields set, chatter contains reason and actor); `test_set_hold_requires_reason` (`UserError` on blank); `test_set_hold_refuses_past_date`; `test_set_hold_refuses_finished_order` (shipped, delivered, cancelled raise `UserError`); `test_release_clears_all_hold_fields`; `test_snooze_moves_date_seven_days_from_today_and_clears_last_nudge`; `test_suggested_hold_uses_zone_and_dormancy`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** The zone start comes from `preorder_waves(int(zone), today, merge_calendar_override(...))` for the fall wave, and the dormancy start from `dormancy_window(env)`.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit:** `feat(grove_headless): record order holds`

### Task 3: Mark shipped requires tracking

**Files:**
- Modify: `grove_headless/models/sale_order_ops.py`, `grove_headless/models/sale_order.py` (`_grove_mark_shipped_and_settle`), `grove_headless/controllers/main.py` (`order_mark_shipped`)
- Test: `grove_headless/tests/test_mark_shipped_tracking.py`; update `tests/test_mark_shipped.py` and other callers that post no tracking

**Interfaces:**
- Consumes: `clean_tracking`, `parse_label_cost`, hold fields.
- Produces: `_grove_ship_with_label(tracking: str, cost: float, actor: str, source: str = "discord") -> dict` returning `_operator_mark_shipped`'s dict. Endpoint body `{actor, tracking, cost}`. Refusals are 422 for bad input and 409 for state, each with an `error` string written for Wesley.

- [ ] **Step 1: Write the failing tests:**
  - `test_endpoint_refuses_missing_tracking` (422, stage unchanged)
  - `test_endpoint_refuses_missing_or_zero_cost` (422)
  - `test_endpoint_strips_spaces_and_refuses_invalid_number` (invalid: 422, no write)
  - `test_records_label_then_ships_and_settles_on_real_cost` (`grove_actual_shipping_cost == 12.40` at the moment settlement runs, stage `shipped`)
  - `test_fall_deposit_order_is_assigned_to_wave_then_ships`
  - `test_second_submit_is_one_charge_one_email`
  - `test_different_number_on_labelled_order_replaces_label`
  - `test_held_order_refused` (409, message names the hold date)
  - `test_spring_order_without_label_refused` (409, message names its zone)
  - `test_odoo_server_action_refuses_without_tracking` (`_grove_mark_shipped_and_settle` raises `UserError`)
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement `_grove_ship_with_label`** in this order: refuse held and spring-without-label; assign wave if `deposit_paid` with a fall wave; record the label, or replace it if the number differs; call `_operator_mark_shipped`. Run it in a savepoint so a label failure writes nothing.
- [ ] **Step 4: Run and confirm the full suite passes.**
- [ ] **Step 5: Commit:** `feat(grove_headless): Mark shipped requires a tracking number`

### Task 4: Label refunded and Move between seasons

**Files:**
- Modify: `grove_headless/models/sale_order_ops.py`, `grove_headless/models/sale_order.py` (`_GROVE_TRANSITIONS`)
- Test: `grove_headless/tests/test_order_label_refunded_move.py`

**Interfaces:**
- Produces: `action_grove_label_refunded(actor: str)`, `action_grove_move_wave(wave: str, actor: str)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_refund_clears_label_keeps_history` (tracking, carriers, services, cost, `grove_label_purchased_at`, `grove_label_batch_id` cleared; chatter holds the old number)
  - `test_refund_returns_wave_order_to_wave_assigned`
  - `test_refund_returns_other_order_to_awaiting_label`
  - `test_refund_refused_once_shipped` (`UserError`)
  - `test_refund_does_not_touch_money` (`grove_checkout_status` unchanged)
  - `test_refunded_order_can_be_labelled_again`
  - `test_move_changes_wave_and_lane` (spring lane after move, deposit unchanged)
  - `test_move_refused_with_tracking` / `_when_shipped` / `_when_cancelled` / `_without_zone`
  - `test_move_sends_no_email` (no new `mail.mail`)
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Add backward transitions: `"awaiting_label": ("label_purchased",)` and add `"label_purchased"` to `"wave_assigned"`.
- [ ] **Step 4: Run and confirm the full suite passes.**
- [ ] **Step 5: Commit:** `feat(grove_headless): Label refunded and season moves`

### Task 5: Card payload and action endpoints

**Files:**
- Create: `grove_headless/controllers/order_ops.py` (add to `controllers/__init__.py`)
- Modify: `grove_headless/models/order_alerts.py` (`build_order_card_payload`), `grove_headless/controllers/main.py` (`_post_order_alert_card` caller)
- Test: `grove_headless/tests/test_order_ops_api.py`

**Interfaces:**
- Consumes: Tasks 2 to 4.
- Produces, added to the card payload: `lane`, `zone`, `shipWave`, `suggestedHold` (ISO date), `labelCost`, `hold` (`{until, reason, setBy}` or null). Routes under `/grove/api/v1/orders/<id>/`, auth like `mark-shipped`, all taking `actor`: `hold` `{until, reason}`, `hold/release`, `hold/snooze`, `move` `{wave}`, `label-refunded`. Each returns `{"card": <payload>}`, and so does `mark-shipped` (alongside its existing keys).

- [ ] **Step 1: Write the failing tests:** `test_hold_route_accepts_typed_date`; `test_hold_route_rejects_bad_date_with_message` (422); `test_routes_are_scoped_to_the_website_company` (404 for another company's order); `test_routes_need_bearer_auth`; `test_card_payload_carries_lane_zone_and_hold`; one success test per remaining route asserting the returned `card` reflects the change.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** A `UserError` or `ValueError` becomes 422 with its message.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit:** `feat(grove_headless): order action endpoints for Discord`

### Task 6: Bridge buttons, modals and card

**Files (in `grove-sites/apps/discord-bridge`):**
- Modify: `lib/orders.ts`, `lib/order-client.ts`, `lib/interactions.ts`, `server.ts`
- Test: `lib/orders.test.ts`, `lib/order-client.test.ts`, `lib/interactions.test.ts`

**Interfaces:**
- Consumes: Task 5 routes and payload.
- Produces: `OrderAction = "shipped" | "hold" | "holddate" | "release" | "ready" | "snooze" | "spring" | "fall" | "refunded" | "markall"`. `custom_id` forms: `ord:<action>:<id>`, and `ord:hold:<id>:<yyyy-mm-dd>` carrying the suggested date. Client: `orderAction(cfg, orderId, path, body, fetchImpl)` returning `{ok, status, error?, card?}`. Followup kind `order_action`.

- [ ] **Step 1: Write the failing tests:**
  - `parseOrderAction` round-trips every action, and the suggested date survives
  - Card buttons by state: unlabelled individual shows Mark shipped, Hold, Move to spring; held shows Change date, Release hold and no Mark shipped; spring without a label shows the zone, Move to fall and no Mark shipped; spring with a label keeps Mark shipped, prefilled; labelled shows Label refunded; shipped shows none
  - Mark shipped click returns MODAL (type 9) with required Tracking and Label cost, prefilled from the card when present
  - Hold click returns a modal with Hold until prefilled from the `custom_id`, and a required Why
  - Modal submit returns a `DEFERRED_UPDATE_MESSAGE` with an `order_action` followup carrying the inputs
  - A non-operator gets the existing denial on every action
  - Client: 422 surfaces Odoo's `error`, and success returns `card`
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** `buildOrderCard` becomes the only renderer. The server edits the card from the returned `card`, and on failure posts Odoo's error as an ephemeral followup and leaves the card alone.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit:** `feat(discord-bridge): order hold, move, refund and tracking modals`

### Task 7: Hold nudges

**Files:**
- Create: `grove_headless/data/order_ops_cron.xml`; add to `__manifest__.py`
- Modify: `grove_headless/models/sale_order_ops.py`; bridge `server.ts`, `lib/orders.ts`
- Test: `grove_headless/tests/test_order_hold_nudge.py`, bridge `lib/orders.test.ts`

**Interfaces:**
- Produces: `POST /orders/notice` on the bridge, secured with `x-order-alert-secret`, body `{companyId, title, lines: string[], buttons: [{label, style, customId}]}`. Odoo helper `_grove_post_notice(company, title, lines, buttons) -> bool`, which never raises. Cron `_cron_grove_order_nudges()`, run hourly, acting once New York local time is 8am or later.

- [ ] **Step 1: Write the failing tests:** `test_due_hold_posts_once_a_day`; `test_nudge_repeats_next_day_until_answered`; `test_future_hold_is_quiet`; `test_before_8am_new_york_is_quiet`; `test_failed_post_does_not_stamp_last_nudge`; `test_finished_order_never_nudges`; on the bridge, notice rendering with Ready to ship, Snooze a week and Change date, and a 401 on a wrong secret.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Stamp `grove_hold_last_nudge` only after the post returns True.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit each repo:** `feat: daily nudges for due order holds`

## Phase 2: Spring zone batches

### Task 8: Batches by zone

**Files:**
- Modify: `grove_headless/models/label_batch.py`, `grove_headless/models/sale_order.py` (`_wave_hold_reason`, `_grove_pack_for_label`), `grove_headless/controllers/main.py` (runner endpoint near line 2163), `grove_headless/data/shipping_actions.xml`
- Create: `grove_headless/wizards/label_batch_build.py` and its view
- Test: `grove_headless/tests/test_label_batch_zones.py`; update `tests/test_label_batch.py`, `tests/test_pirateship_import.py`

**Interfaces:**
- Produces: `grove.label.batch.zone` (same selection as `grove_usda_zone`). `_open_batch(company, zone)`, `_open_batches(company)`, `build_open_batch(company, zone)`. `spring_build_window(zone, today, calendar) -> (date_open, date_close)`, opening 5 days before `ship_start`.

- [ ] **Step 1: Write the failing tests:**
  - `test_zone_batch_takes_only_that_zone_spring_orders` (individual-lane, other-zone and held orders excluded)
  - `test_build_assigns_deposit_orders_to_wave`
  - `test_build_refused_more_than_5_days_early`
  - `test_build_allowed_at_exactly_5_days`
  - `test_two_zones_open_at_once`
  - `test_second_batch_picks_up_late_orders_after_first_is_purchased`
  - `test_import_matches_rows_across_open_batches`
  - `test_runner_build_requires_zone` (400)
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** The Build menu opens a zone-picking wizard. Pass a `ignore_ship_window` flag from batch building into `_grove_pack_for_label` once inside `spring_build_window`, so the 5-day allowance applies only there.
- [ ] **Step 4: Run and confirm the full suite passes.**
- [ ] **Step 5: Commit:** `feat(grove_headless): build Pirate Ship batches per spring zone`

### Task 9: Zone warnings

**Files:**
- Create: `grove_headless/models/zone_alert.py` (`grove.zone.alert`: company, zone, year, kind; unique together; access in `security/ir.model.access.csv`)
- Modify: `grove_headless/models/order_lanes.py`, `grove_headless/data/order_ops_cron.xml` (same hourly job), `grove_headless/models/sale_order_ops.py`
- Test: `grove_headless/tests/test_zone_alerts.py`

**Interfaces:**
- Consumes: `_grove_post_notice` (Task 7).
- Produces: `zone_alert_kind(today: date, ship_start: date, ship_end: date) -> "t5" | "day_of" | None`. `_cron_grove_zone_alerts()`.

- [ ] **Step 1: Write the failing tests:**
  - `zone_alert_kind`: start minus 6 is None, minus 5 and minus 1 are t5, start and end are day_of, end plus 1 is None
  - `test_t5_text` asserts "Zone 6 spring shipping starts in 5 days: 14 orders, 3 on hold."
  - `test_day_of_text` asserts "Zone 6 spring shipping starts today: 14 orders ready to batch."
  - `test_zone_without_orders_posts_nothing`
  - `test_alert_sends_once_per_zone_and_year`
  - `test_missed_day_catches_up`
  - `test_uses_admin_calendar_override`
  - `test_failed_post_records_nothing`
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Create the `grove.zone.alert` row only after the post returns True. Count days to the start for the t5 wording, so a catch-up says the true number.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit:** `feat(grove_headless): 5-day and day-of spring zone warnings`

### Task 10: Late orders and Mark all shipped

**Files:**
- Modify: `grove_headless/models/sale_order_ops.py`, `grove_headless/models/label_batch.py`, `grove_headless/controllers/order_ops.py`; bridge `lib/orders.ts`, `lib/order-client.ts`, `lib/interactions.ts`, `server.ts`
- Test: `grove_headless/tests/test_zone_late_and_markall.py`, bridge `lib/interactions.test.ts`

**Interfaces:**
- Consumes: `_grove_post_notice` (Task 7), `orderAction` and the `order_action` followup (Task 6).
- Produces: `POST /grove/api/v1/label-batches/<id>/mark-shipped` `{actor}` returning `{shipped, already, skipped}`. `_grove_mark_batch_shipped(actor) -> dict`. Button `ord:markall:<batchId>`.

- [ ] **Step 1: Write the failing tests:**
  - `test_late_post_lists_unbatched_spring_orders` ("Zone 6: 2 spring orders still need a batch.")
  - `test_late_post_stops_after_ship_end`
  - `test_late_post_names_spring_orders_with_no_zone`
  - `test_import_posts_labels_recorded_with_markall` ("Zone 6: 14 labels recorded")
  - `test_markall_refused_before_ship_start` (409)
  - `test_markall_ships_only_unmarked_orders_and_skips_held`
  - `test_markall_does_not_charge_again` (settlement stays a no-op)
  - `test_markall_twice_is_safe`
  - Bridge: `markall` is operator-only, and it edits the notice with the counts
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Post "labels recorded" when `_reconcile_rows` moves a zone batch to `purchased`, without letting a post failure affect the import.
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit each repo:** `feat: late spring order post and Mark all shipped`

### Task 11: Odoo views, launch repost and rollout

**Files:**
- Modify: `grove_headless/views/fulfillment_views.xml` (hold fields on the order form), `grove_headless/views/grove_zone_views.xml` ("On hold" filter), `grove_headless/data/shipping_actions.xml` (Hold, Release, Move, Label refunded actions), `grove_headless/__manifest__.py` (version bump)
- Test: `grove_headless/tests/test_order_ops_views.py`

- [ ] **Step 1: Write the failing tests:** `test_on_hold_filter_finds_held_orders`; `test_repost_covers_open_ship_orders_once` (a second run posts nothing).
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement** the views, the actions and `_grove_repost_open_order_cards()`, to be run once by hand after deploy.
- [ ] **Step 4: Run and confirm the full suite passes.**
- [ ] **Step 5: Commit:** `feat(grove_headless): Odoo views and launch repost for order lanes`
- [ ] **Step 6: Sync the bridge deploy copy.** `odoocker-goldberrygrove/infra/terraform/environments/observability/compose/discord-bridge-src` holds its own copy of the bridge. Bring it up to date in an odoocker PR. Any Terraform apply must be `-target`ed, and Josh runs it.
- [ ] **Step 7: Update the Grove Order Handling SOP** (https://claude.ai/artifact/LbEkwLPZfkuttAx2ZQQCE7) for the Discord flow and zone batches.
